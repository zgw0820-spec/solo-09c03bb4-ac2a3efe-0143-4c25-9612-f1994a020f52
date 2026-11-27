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


def test_stale_reload_returns_409(client: TestClient, volume: Path) -> None:
    # 现行 v2，再把 ..data 切回更旧的 v1
    _write(volume, "..2026-v2", GROUPS_V2, TARGETS_V2)
    _swap(volume, "..2026-v2")
    assert client.post("/admin/reload").status_code == 200

    _swap(volume, "..2026-v1")
    resp = client.post("/admin/reload")
    assert resp.status_code == 409
    assert "旧于现行版本" in resp.json()["detail"]["detail"]
    assert client.get("/version").json()["version"] == "..2026-v2"


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
