"""API 层测试：重读接口、按组查询、错误码与无配置状态。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

GROUPS_V1 = {
    "groups": [
        {"name": "db-inspection", "target_ids": ["mysql-primary"]},
    ]
}
TARGETS_V1 = {
    "targets": [
        {"id": "mysql-primary", "address": "10.0.1.10:3306", "timeout_seconds": 3.0},
    ]
}
GROUPS_V2 = {
    "groups": [
        {"name": "db-inspection", "target_ids": ["mysql-primary", "redis-cluster"]},
    ]
}
TARGETS_V2 = {
    "targets": [
        {"id": "mysql-primary", "address": "10.0.1.10:3306", "timeout_seconds": 2.5},
        {"id": "redis-cluster", "address": "10.0.3.8:6379", "timeout_seconds": 1.5},
    ]
}
BAD_GROUPS = {"groups": [{"name": "db-inspection", "target_ids": ["ghost"]}]}


def _write(root: Path, version: str, groups: dict, targets: dict) -> None:
    version_dir = root / version
    version_dir.mkdir(parents=True)
    (version_dir / "inspection-groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (version_dir / "targets.json").write_text(json.dumps(targets), encoding="utf-8")
    data_tmp = root / f".tmp-data-{version}"
    os.symlink(version, data_tmp)
    os.replace(data_tmp, root / "..data")
    groups_tmp = root / f".tmp-groups-{version}"
    os.symlink("..data/inspection-groups.json", groups_tmp)
    os.replace(groups_tmp, root / "inspection-groups.json")
    targets_tmp = root / f".tmp-targets-{version}"
    os.symlink("..data/targets.json", targets_tmp)
    os.replace(targets_tmp, root / "targets.json")


def _swap(root: Path, version: str) -> None:
    tmp_link = root / f".swap-{version}"
    os.symlink(version, tmp_link)
    os.replace(tmp_link, root / "..data")


@pytest.fixture()
def volume(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    root.mkdir()
    _write(root, "..2026-v1", GROUPS_V1, TARGETS_V1)
    return root


@pytest.fixture()
def client(volume: Path) -> TestClient:
    app = create_app(volume_root=str(volume))
    with TestClient(app) as c:
        yield c


def test_health_and_version_after_startup(client: TestClient) -> None:
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["version"] == "..2026-v1"

    version = client.get("/version").json()
    assert version["version"] == "..2026-v1"
    assert version["group_count"] == 1
    assert version["target_count"] == 1


def test_group_query_returns_current_version(client: TestClient) -> None:
    resp = client.get("/groups/db-inspection")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "..2026-v1"
    assert body["targets"] == [
        {"id": "mysql-primary", "address": "10.0.1.10:3306", "timeout_seconds": 3.0}
    ]


def test_group_not_found_and_listing(client: TestClient) -> None:
    assert client.get("/groups").json() == ["db-inspection"]
    resp = client.get("/groups/nope")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "group_not_found"


def test_reload_picks_up_new_projection(client: TestClient, volume: Path) -> None:
    _write(volume, "..2026-v2", GROUPS_V2, TARGETS_V2)
    _swap(volume, "..2026-v2")

    resp = client.post("/admin/reload")
    assert resp.status_code == 200
    assert resp.json()["version"] == "..2026-v2"
    assert resp.json()["target_count"] == 2

    body = client.get("/groups/db-inspection").json()
    assert body["version"] == "..2026-v2"
    assert [t["id"] for t in body["targets"]] == ["mysql-primary", "redis-cluster"]
    assert body["targets"][1]["timeout_seconds"] == 1.5


def test_bad_projection_keeps_old_config(client: TestClient, volume: Path) -> None:
    _write(volume, "..2026-v3", BAD_GROUPS, TARGETS_V1)
    _swap(volume, "..2026-v3")

    resp = client.post("/admin/reload")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "reload_failed"
    assert "ghost" in detail["detail"]

    # 现行版本保持不变，查询不受影响。
    assert client.get("/version").json()["version"] == "..2026-v1"
    assert client.get("/groups/db-inspection").json()["version"] == "..2026-v1"


def test_clock_rollback_named_projection_still_reloads(client: TestClient, volume: Path) -> None:
    # 节点时钟回拨：新投射目录名按字典序排在现行 "..2026-v1" 之前，
    # 只要两份 JSON 校验通过，手动重读就应 200 并整版换成新目标。
    rolled_back_name = "..2024-clock-rollback"
    assert rolled_back_name < "..2026-v1"
    _write(volume, rolled_back_name, GROUPS_V2, TARGETS_V2)
    _swap(volume, rolled_back_name)

    resp = client.post("/admin/reload")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == rolled_back_name
    assert body["target_count"] == 2

    version = client.get("/version").json()
    assert version["version"] == rolled_back_name

    group = client.get("/groups/db-inspection").json()
    assert group["version"] == rolled_back_name
    assert [t["id"] for t in group["targets"]] == ["mysql-primary", "redis-cluster"]
    assert group["targets"][0]["timeout_seconds"] == 2.5


def test_swap_back_to_older_valid_projection_is_accepted(client: TestClient, volume: Path) -> None:
    # 新规则下“当前 ..data 指向谁”是唯一身份：显式切回旧的有效投射并
    # reload 应当成功（时钟回拨/回滚 ConfigMap 是合法操作），不再按
    # 目录名字典序返回 409。409 只保留给“读取后卷又变了”的并发竞争。
    _write(volume, "..2026-v2", GROUPS_V2, TARGETS_V2)
    _swap(volume, "..2026-v2")
    assert client.post("/admin/reload").status_code == 200

    _swap(volume, "..2026-v1")
    resp = client.post("/admin/reload")
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "..2026-v1"
    assert client.get("/version").json()["version"] == "..2026-v1"


def test_stale_reload_returns_409(
    client: TestClient, volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 新语义下 409 只针对“读取后卷又换到另一份投射”的真实竞争。
    # 确定性交错：第一个 POST 读到 v1 后在校验处暂停；期间卷换到 v2，
    # 第二个 POST 先把 v2 生效；放行第一个 POST，它必须收到 409，
    # 且不能把已经生效的 v2 新目标换回去。
    import threading

    import app.store as store_module

    original_validate = store_module._validate
    started = threading.Event()
    release_first = threading.Event()
    state = {"armed": True}

    def slow_first_validate(groups_raw, targets_raw):
        if state["armed"]:
            state["armed"] = False
            started.set()
            assert release_first.wait(timeout=5)
        return original_validate(groups_raw, targets_raw)

    monkeypatch.setattr(store_module, "_validate", slow_first_validate)

    slow_response: dict[str, object] = {}

    def post_slow() -> None:
        slow_response["resp"] = client.post("/admin/reload")

    worker = threading.Thread(target=post_slow)
    worker.start()
    assert started.wait(timeout=2)

    # 旧请求已读到 v1 并暂停；发布并生效 v2。
    _write(volume, "..2026-v2", GROUPS_V2, TARGETS_V2)
    _swap(volume, "..2026-v2")
    fast = client.post("/admin/reload")
    assert fast.status_code == 200, fast.text
    assert fast.json()["version"] == "..2026-v2"

    # 旧请求此时尚未结束，而新投射已经生效；查询无需等待它。
    assert worker.is_alive()
    immediate = client.get("/groups/db-inspection")
    assert immediate.status_code == 200
    assert immediate.json()["version"] == "..2026-v2"

    release_first.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    resp = slow_response["resp"]
    assert resp.status_code == 409, getattr(resp, "text", resp)
    detail = resp.json()["detail"]
    assert detail["error"] == "reload_failed"
    assert "投射" in detail["detail"]

    # 收尾较慢的旧请求没有把 v2 换回去。
    version = client.get("/version").json()
    assert version["version"] == "..2026-v2"
    group = client.get("/groups/db-inspection").json()
    assert [t["id"] for t in group["targets"]] == ["mysql-primary", "redis-cluster"]


def test_background_poll_records_validation_failure_and_keeps_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import time

    root = tmp_path / "poll-volume"
    root.mkdir()
    _write(root, "..2026-v1", GROUPS_V1, TARGETS_V1)

    app = create_app(volume_root=str(root), watch_interval=0.02)
    with TestClient(app) as poll_client:
        assert poll_client.get("/version").json()["version"] == "..2026-v1"

        # 换入引用缺失的坏投射，后台轮询应记下原因但保留 v1。
        _write(root, "..2026-v3-bad", BAD_GROUPS, TARGETS_V1)
        _swap(root, "..2026-v3-bad")

        deadline = time.time() + 5
        while time.time() < deadline and app.state.poll_status["failures"] == 0:
            time.sleep(0.02)

        status = app.state.poll_status
        assert status["failures"] >= 1
        assert status["last_outcome"] == "error"
        assert "ghost" in (status["last_error"] or "")
        assert poll_client.get("/version").json()["version"] == "..2026-v1"

        caplog.set_level(logging.WARNING, logger="inspection-config")
        # 再等一轮，确保日志里也写下了带原因的警告。
        before = status["runs"]
        deadline = time.time() + 5
        while time.time() < deadline and status["runs"] < before + 1:
            time.sleep(0.02)
        text = caplog.text
        assert "后台轮询" in text
        assert "ghost" in text

        # 修好投射后，同一后台轮询无需手动 reload 即可自动生效。
        _write(root, "..2026-v4-good", GROUPS_V2, TARGETS_V2)
        _swap(root, "..2026-v4-good")
        deadline = time.time() + 5
        while time.time() < deadline and status["last_success_version"] != "..2026-v4-good":
            time.sleep(0.02)
        assert status["successes"] >= 1
        assert poll_client.get("/version").json()["version"] == "..2026-v4-good"


def test_background_poll_records_conflict_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import time

    from app.store import ProjectionConflictError

    root = tmp_path / "poll-conflict"
    root.mkdir()
    _write(root, "..2026-v1", GROUPS_V1, TARGETS_V1)

    app = create_app(volume_root=str(root), watch_interval=0.02)
    with TestClient(app) as poll_client:
        store = app.state.store
        original_reload = store.reload
        armed = {"once": True}

        def fake_reload():
            if armed["once"]:
                armed["once"] = False
                raise ProjectionConflictError("测试用冲突原因：投射已切换 (HTTP 409)")
            return original_reload()

        store.reload = fake_reload  # type: ignore[method-assign]

        caplog.set_level(logging.WARNING, logger="inspection-config")
        deadline = time.time() + 5
        while time.time() < deadline and app.state.poll_status["conflicts"] == 0:
            time.sleep(0.02)

        status = app.state.poll_status
        assert status["conflicts"] >= 1
        assert status["last_outcome"] == "conflict"
        assert "测试用冲突原因" in (status["last_error"] or "")
        # 冲突不改变现行配置，也不杀死轮询任务。
        assert poll_client.get("/version").json()["version"] == "..2026-v1"
        assert "后台轮询" in caplog.text and "测试用冲突原因" in caplog.text


def test_background_poll_uses_monkeypatched_global_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 兼容最自然的测试手法：patch app.main.WATCH_INTERVAL 后再 create_app，
    # 不传任何自定义参数；后台轮询应按该间隔运行并记录失败原因。
    import time

    import app.main as main_module

    root = tmp_path / "poll-global"
    root.mkdir()
    _write(root, "..2026-v1", GROUPS_V1, TARGETS_V1)

    monkeypatch.setattr(main_module, "WATCH_INTERVAL", 0.02)
    app = main_module.create_app(volume_root=str(root))
    with TestClient(app) as poll_client:
        assert poll_client.get("/version").status_code == 200
        _write(root, "..2026-v3-bad", BAD_GROUPS, TARGETS_V1)
        _swap(root, "..2026-v3-bad")

        deadline = time.time() + 5
        while time.time() < deadline and app.state.poll_status["failures"] == 0:
            time.sleep(0.02)
        assert app.state.poll_status["failures"] >= 1
        assert "ghost" in (app.state.poll_status["last_error"] or "")
        assert poll_client.get("/version").json()["version"] == "..2026-v1"


def test_startup_without_valid_projection_serves_503(tmp_path: Path) -> None:
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    app = create_app(volume_root=str(empty_root))
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "not_loaded"
        resp = client.get("/version")
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "config_not_loaded"

        # 补上有效投射后即可恢复，无需重启进程。
        _write(empty_root, "..2026-v1", GROUPS_V1, TARGETS_V1)
        assert client.post("/admin/reload").status_code == 200
        assert client.get("/version").json()["version"] == "..2026-v1"
