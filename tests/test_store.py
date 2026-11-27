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


def test_stale_version_cannot_overwrite_newer(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v2", GROUPS_V2, TARGETS_V2)
    store = ConfigStore(str(tmp_path))
    store.reload()

    # 候选版本号更旧（模拟慢速旧版重读晚于新版完成）
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    with pytest.raises(ReloadError, match="旧于现行版本"):
        store.reload()
    assert store.snapshot.version == "..2026-v2"


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


def test_concurrent_reloads_only_newest_wins(tmp_path: Path) -> None:
    make_projection(tmp_path, "..2026-v1", GROUPS_V1, TARGETS_V1)
    make_projection(tmp_path, "..2026-v2", GROUPS_V2, TARGETS_V2)
    store = ConfigStore(str(tmp_path))
    store.reload()  # 先让 v1 生效

    def reload_version(version: str, barrier: threading.Barrier) -> None:
        # 手动切换 ..data 到指定版本，再和其它线程同时触发重读，
        # 模拟“两版重读并发、旧版完成得更晚”的竞争。
        barrier.wait()
        link = tmp_path / DATA_LINKNAME
        tmp_link = tmp_path / f".swap-{version}"
        os.symlink(version, tmp_link)
        os.replace(tmp_link, link)
        store.reload()

    # 两个线程在屏障处同时起跑后各自切换 ..data 并触发重读，模拟“两版重读
    # 并发、完成顺序不确定”的竞争。无论谁先完成，最终现行版本都必须是
    # 较新的 v2：v1 若先提交会被随后的 v2 覆盖，v2 若先提交则 v1 被拒。
    barrier = threading.Barrier(2)
    t_v2 = threading.Thread(target=reload_version, args=("..2026-v2", barrier))
    t_v1 = threading.Thread(target=reload_version, args=("..2026-v1", barrier))
    t_v2.start()
    t_v1.start()
    t_v2.join()
    t_v1.join()

    assert store.snapshot is not None
    assert store.snapshot.version == "..2026-v2"


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
