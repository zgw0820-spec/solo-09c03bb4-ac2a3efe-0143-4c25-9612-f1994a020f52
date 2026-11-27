"""单元测试：原子重读、同版校验、引用完整性、并发版本保护。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from app.store import (
    DATA_LINKNAME,
    GROUPS_FILENAME,
    TARGETS_FILENAME,
    ConfigStore,
    ReloadError,
    Snapshot,
    _content_hash,
    read_projection,
)

GROUPS_V1 = {
    "groups": [
        {"name": "g-a", "target_ids": ["t1", "t2"]},
        {"name": "g-b", "target_ids": ["t2"]},
    ]
}
TARGETS_V1 = {
    "targets": [
        {"id": "t1", "address": "10.0.0.1:80", "timeout_seconds": 1.5},
        {"id": "t2", "address": "10.0.0.2:80", "timeout_seconds": 2},
    ]
}
GROUPS_V2 = {
    "groups": [{"name": "g-a", "target_ids": ["t1", "t3"]}]
}
TARGETS_V2 = {
    "targets": [
        {"id": "t1", "address": "10.0.0.1:8080", "timeout_seconds": 3.0},
        {"id": "t3", "address": "10.0.0.3:80", "timeout_seconds": 4.0},
    ]
}


def make_projection(
    root: Path,
    version: str,
    groups: dict,
    targets: dict,
    *,
    groups_link_version: str | None = None,
    skip_data_link: bool = False,
) -> Path:
    """在 tmp_path 下搭一个 K8s 风格投射目录。"""
    version_dir = root / version
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / GROUPS_FILENAME).write_text(json.dumps(groups), encoding="utf-8")
    (version_dir / TARGETS_FILENAME).write_text(json.dumps(targets), encoding="utf-8")

    if not skip_data_link:
        tmp_link = root / f".tmp-data-{version}"
        os.symlink(version, tmp_link)
        os.replace(tmp_link, root / DATA_LINKNAME)

    groups_target_version = groups_link_version or version
    groups_tmp = root / f".tmp-groups-{version}"
    os.symlink(f"{groups_target_version}/{GROUPS_FILENAME}", groups_tmp)
    os.replace(groups_tmp, root / GROUPS_FILENAME)
    targets_tmp = root / f".tmp-targets-{version}"
    os.symlink(f"{version}/{TARGETS_FILENAME}", targets_tmp)
    os.replace(targets_tmp, root / TARGETS_FILENAME)
    return version_dir


def test_first_reload_succeeds_and_query_resolves_group(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    store = ConfigStore(str(tmp_path))

    snapshot = store.reload()
    assert snapshot.version == "..2026-v1"
    assert set(snapshot.groups) == {"g-a", "g-b"}
    assert snapshot.targets["t1"].address == "10.0.0.1:80"

    result = snapshot.resolve_group("g-a")
    assert result is not None
    assert result["version"] == "..2026-v1"
    assert [t["id"] for t in result["targets"]] == ["t1", "t2"]
    assert snapshot.resolve_group("missing") is None


def test_missing_reference_rejected_and_old_config_kept(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    store = ConfigStore(str(tmp_path))
    store.reload()

    bad_groups = {"groups": [{"name": "g-x", "target_ids": ["t1", "ghost"]}]}
    make_projection(tmp_path, "..2026-v2", bad_groups, TARGETS_V1)
    os.replace(tmp_path / DATA_LINKNAME, tmp_path / ".old-link")
    os.symlink("..2026-v2", tmp_path / DATA_LINKNAME)
    os.replace(tmp_path / GROUPS_FILENAME, tmp_path / ".old-groups")
    os.symlink(f"..2026-v2/{GROUPS_FILENAME}", tmp_path / GROUPS_FILENAME)

    with pytest.raises(ReloadError, match="ghost"):
        store.reload()

    current = store.snapshot
    assert current is not None
    assert current.version == "..2026-v1"
    assert "g-x" not in current.groups


def test_malformed_json_rejected(tmp_path: Path) -> None:
    version_dir = tmp_path / "..2026-v1"
    version_dir.mkdir()
    (version_dir / GROUPS_FILENAME).write_text("{not json", encoding="utf-8")
    (version_dir / TARGETS_FILENAME).write_text(json.dumps(TARGETS_V1), encoding="utf-8")
    os.symlink("..2026-v1", tmp_path / DATA_LINKNAME)
    os.symlink(f"..2026-v1/{GROUPS_FILENAME}", tmp_path / GROUPS_FILENAME)
    os.symlink(f"..2026-v1/{TARGETS_FILENAME}", tmp_path / TARGETS_FILENAME)

    store = ConfigStore(str(tmp_path))
    with pytest.raises(ReloadError, match="不是合法 JSON"):
        store.reload()
    assert store.snapshot is None


def test_validation_rules(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    store = ConfigStore(str(tmp_path))

    # 重复组名
    dup_groups = {
        "groups": [
            {"name": "g", "target_ids": ["t1"]},
            {"name": "g", "target_ids": ["t2"]},
        ]
    }
    with pytest.raises(ReloadError, match="巡检组名称重复"):
        from app.store import _validate

        _validate(dup_groups, TARGETS_V1)

    # 超时必须为正数（且布尔值不被当作数字）
    bad_targets = {
        "targets": [
            {"id": "t1", "address": "a", "timeout_seconds": True},
        ]
    }
    with pytest.raises(ReloadError, match="timeout_seconds"):
        _validate(GROUPS_V1, bad_targets)

    # target_ids 不能为空
    with pytest.raises(ReloadError, match="target_ids"):
        _validate({"groups": [{"name": "g", "target_ids": []}]}, TARGETS_V1)

    # 未引用目标只告警不报错，且不影响生效
    groups, targets, warnings = _validate(GROUPS_V1, {
        "targets": TARGETS_V1["targets"]
        + [{"id": "t9", "address": "x", "timeout_seconds": 1.0}]
    })
    assert len(targets) == 3
    assert any("t9" in w for w in warnings)
    assert store.snapshot is None  # 校验函数本身不改状态


def test_mixed_versions_rejected(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    # v2 目录存在，但根目录 groups 链接仍指向 v1 目录 -> 两版混用
    make_projection(
        tmp_path,
        "..2026-v2",
        GROUPS_V2,
        TARGETS_V2,
        groups_link_version="..2026-v1",
    )
    os.replace(tmp_path / DATA_LINKNAME, tmp_path / ".old-link")
    os.symlink("..2026-v2", tmp_path / DATA_LINKNAME)

    with pytest.raises(ReloadError, match="不一致"):
        read_projection(str(tmp_path))


def test_missing_data_link_rejected(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1, skip_data_link=True)
    with pytest.raises(ReloadError, match=r"\.\.data"):
        read_projection(str(tmp_path))


def test_clock_rollback_lexically_older_projection_still_activates(tmp_path: Path) -> None:
    # 节点时钟回拨后，新投射的时间戳目录名按字典序可能排在旧版本之前。
    future_name = "..2026-11-27T05_00_00.000000000-Z"
    past_name = "..2025-01-01T00_00_00.000000000-Z"
    assert past_name < future_name  # 前置条件：字典序确实更旧

    make_projection(tmp_path, future_name, GROUPS_V2, TARGETS_V2)
    store = ConfigStore(str(tmp_path))
    store.reload()
    assert store.snapshot.version == future_name

    # 时钟回拨后 kubelet 发布的“新”投射：目录名字典序更旧，但内容是新的。
    rolled_name = "..2024-06-01T00_00_00.000000000-Z"
    make_projection(tmp_path, rolled_name, GROUPS_V2, TARGETS_V2)
    snapshot = store.reload()

    assert snapshot.version == rolled_name
    assert store.snapshot is snapshot
    # 两份 JSON 整版生效：V2 才有的 t3 必须可解析。
    assert "t3" in snapshot.targets
    assert snapshot.resolve_group("g-a")["targets"][1]["id"] == "t3"

    # 直接指回一个字典序更旧但有效的历史目录同样应当生效：当前投射是谁，
    # 就以谁为准，而不是按目录名排新旧。
    make_projection(tmp_path, past_name, GROUPS_V1, TARGETS_V1)
    snapshot = store.reload()
    assert snapshot.version == past_name
    assert set(snapshot.groups) == {"g-a", "g-b"}


def test_stale_version_cannot_overwrite_newer(tmp_path: Path) -> None:
    # 新语义下“旧”不再按目录名字典序判定，而是按投射身份：请求 A 读到 v1
    # 后被拖慢；期间 ..data 换到 v2。A 准备生效时复核失败，必须抛冲突并
    # 保留现行（v1，或另一个请求已提交的 v2）配置，不能覆盖当前投射。
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    store = ConfigStore(str(tmp_path))
    store.reload()

    started = threading.Event()
    release = threading.Event()
    armed = {"block": True}
    import app.store as store_module

    original_validate = store_module._validate

    def slow_once(groups_raw, targets_raw):
        if armed["block"]:
            armed["block"] = False
            started.set()
            assert release.wait(timeout=5)
        return original_validate(groups_raw, targets_raw)

    result: dict[str, object] = {}

    def slow_reload() -> None:
        try:
            result["snapshot"] = store.reload()
        except BaseException as exc:  # noqa: BLE001 - 测试需要跨线程带回异常
            result["error"] = exc

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(store_module, "_validate", slow_once)
        worker = threading.Thread(target=slow_reload)
        worker.start()
        assert started.wait(timeout=2)

        # 读取之后、提交之前，卷换到了另一份投射。
        make_projection(tmp_path, "..2026-v2", GROUPS_V2, TARGETS_V2)

        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert "error" in result
    assert isinstance(result["error"], ReloadError)
    assert getattr(result["error"], "status_code", None) == 409
    # 没有新的成功提交，现行配置原样保留为 v1。
    assert store.snapshot.version == "..2026-v1"


def test_concurrent_reloads_only_newest_wins(tmp_path: Path) -> None:
    # 精确复现需求中的交错：旧请求 A 已读到旧投射并卡住；新请求 B 完成 v2
    # 生效（此时 A 尚未结束）；随后 A 收尾时必须失败且不能把 v1 换回去。
    # 新规则里“最新”指卷当前指向的投射，而不是目录名字典序。
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    store = ConfigStore(str(tmp_path))
    store.reload()

    started = threading.Event()
    release_old = threading.Event()
    armed = {"block": True}
    import app.store as store_module

    original_validate = store_module._validate

    def slow_old_validate(groups_raw, targets_raw):
        if armed["block"]:
            armed["block"] = False
            started.set()
            assert release_old.wait(timeout=5)
        return original_validate(groups_raw, targets_raw)

    old_result: dict[str, object] = {}

    def old_reload() -> None:
        try:
            old_result["snapshot"] = store.reload()
        except BaseException as exc:  # noqa: BLE001
            old_result["error"] = exc

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(store_module, "_validate", slow_old_validate)

        old_thread = threading.Thread(target=old_reload)
        old_thread.start()
        assert started.wait(timeout=2)

        # 卷换到 v2，新请求一路畅通地完成并先生效。
        make_projection(tmp_path, "..2026-v2", GROUPS_V2, TARGETS_V2)
        new_snapshot = store.reload()
        assert new_snapshot.version == "..2026-v2"
        assert store.snapshot.version == "..2026-v2"
        # 旧请求此刻仍未结束。
        assert old_thread.is_alive()

        release_old.set()
        old_thread.join(timeout=5)

    assert not old_thread.is_alive()
    assert isinstance(old_result.get("error"), ReloadError)
    assert getattr(old_result["error"], "status_code", None) == 409
    # 旧请求收尾后，新目标绝不能被换回去。
    assert store.snapshot.version == "..2026-v2"
    assert "t3" in store.snapshot.targets


def test_concurrent_reloads_commit_to_current_projection(tmp_path: Path) -> None:
    # 两个重读并发、..data 最终稳定指向 v2：无论线程如何交错，最终快照都
    # 必须是当前投射 v2；失败的候选只能是冲突错误，不能污染现行配置。
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    make_projection(tmp_path, "..2026-v2", GROUPS_V2, TARGETS_V2)
    # 让 ..data 稳定指向 v2（make_projection 最后一次已切到 v2）。
    store = ConfigStore(str(tmp_path))
    store.reload()
    assert store.snapshot.version == "..2026-v2"

    errors: list[BaseException] = []
    snapshots: list[Snapshot] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            snapshots.append(store.reload())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    # 投射没有再变化，所有重读都应成功（同版同内容幂等），不应有冲突。
    assert errors == []
    assert store.snapshot.version == "..2026-v2"
    assert all(s.version == "..2026-v2" for s in snapshots)


def test_same_version_same_content_is_noop(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    store = ConfigStore(str(tmp_path))
    first = store.reload()
    second = store.reload()
    assert first is second


def test_content_hash_detects_change(tmp_path: Path) -> None:
    h1 = _content_hash(GROUPS_V1, TARGETS_V1)
    changed = json.loads(json.dumps(TARGETS_V1))
    changed["targets"][0]["timeout_seconds"] = 9.9
    assert _content_hash(GROUPS_V1, changed) != h1


def test_queries_do_not_block_on_reload_lock(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    store = ConfigStore(str(tmp_path))
    store.reload()

    started = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with store._lock:
            started.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert started.wait(timeout=2)
    try:
        # 重读锁被占住时，快照查询仍立即返回现行版本。
        assert store.snapshot.version == "..2026-v1"
        assert store.snapshot.resolve_group("g-b") is not None
    finally:
        release.set()
        holder.join()
