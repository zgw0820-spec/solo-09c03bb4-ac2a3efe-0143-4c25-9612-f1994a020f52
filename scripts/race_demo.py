#!/usr/bin/env python3
"""确定性演示“两次重读交错”：新投射先生效，慢旧请求收到 409，查询不等待。

本脚本不依赖线程调度运气：在第一份投射读完、校验函数入口处用事件门把旧
请求挂住，再换卷并让第二个 POST 完成。所有操作都走真实 HTTP 栈
（FastAPI TestClient -> /admin/reload、/groups/...、/version）。

用法（先 ``pip install -r requirements.txt httpx``）：
    python scripts/race_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.store as store_module  # noqa: E402
from app.main import create_app  # noqa: E402

GROUPS_V1 = {"groups": [{"name": "db-inspection", "target_ids": ["mysql-primary"]}]}
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


def write_projection(root: Path, version: str, groups: dict, targets: dict) -> None:
    version_dir = root / version
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "inspection-groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (version_dir / "targets.json").write_text(json.dumps(targets), encoding="utf-8")
    tmp = root / f".demo-data-{version}"
    os.symlink(version, tmp)
    os.replace(tmp, root / "..data")
    for name in ("inspection-groups.json", "targets.json"):
        link_tmp = root / f".demo-link-{name}-{version}"
        os.symlink(f"..data/{name}", link_tmp)
        os.replace(link_tmp, root / name)


def show(title: str, response) -> None:
    print(f"\n-- {title}")
    print(f"   HTTP {response.status_code}")
    try:
        print("   " + json.dumps(response.json(), ensure_ascii=False))
    except Exception:
        print("   " + response.text[:300])


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="race-demo-")
    root = Path(tmp) / "config"
    root.mkdir()
    write_projection(root, "..2026-v1", GROUPS_V1, TARGETS_V1)

    app = create_app(volume_root=str(root))
    started = threading.Event()
    release_old = threading.Event()
    armed = {"block_first": True}
    original_validate = store_module._validate

    def gated_validate(groups_raw, targets_raw):
        # 只挂住“打补丁之后的第一个”重读（即慢旧请求），其它调用直接放行。
        if armed["block_first"]:
            armed["block_first"] = False
            started.set()
            print("\n[gate] 旧请求已读完 v1 并进入校验，挂起等待…")
            if not release_old.wait(timeout=10):
                raise RuntimeError("演示等待超时")
            print("[gate] 放行旧请求，它将在锁内复核 ..data 身份")
        return original_validate(groups_raw, targets_raw)

    failures: list[str] = []

    with TestClient(app) as client:
        show("启动后 /version（v1）", client.get("/version"))

        # 启动加载已经结束，此时再装门，避免门被启动重读消费。
        store_module._validate = gated_validate  # type: ignore[assignment]

        outcome: dict[str, object] = {}

        def slow_post() -> None:
            outcome["response"] = client.post("/admin/reload")

        worker = threading.Thread(target=slow_post, name="slow-old-reload")
        worker.start()
        if not started.wait(timeout=5):
            print("未能让旧请求进入挂起点")
            return 2

        # 旧请求挂住期间换卷到 v2；新的手动重读一路完成并先生效。
        write_projection(root, "..2026-v2", GROUPS_V2, TARGETS_V2)
        new_resp = client.post("/admin/reload")
        show("期间第二个 POST /admin/reload（v2，应 200）", new_resp)
        if new_resp.status_code != 200 or new_resp.json()["version"] != "..2026-v2":
            failures.append("新投射 v2 未能在旧请求结束前生效")

        # 查询完全不等旧重读：立即拿到已经生效的 v2。
        group_resp = client.get("/groups/db-inspection")
        show("旧请求仍挂着时 GET /groups/db-inspection（应立即是 v2）", group_resp)
        if group_resp.json().get("version") != "..2026-v2":
            failures.append("查询没有立即读到已生效的 v2")
        if worker.is_alive():
            print("\n[check] 旧请求线程仍在挂起中，查询没有等待它 ✓")

        release_old.set()
        worker.join(timeout=10)
        old_resp = outcome["response"]
        show("慢旧请求最终的 POST /admin/reload（应 409）", old_resp)
        if old_resp.status_code != 409:
            failures.append(f"慢旧请求应返回 409，实际 {old_resp.status_code}")

        final = client.get("/version").json()
        print("\n-- 收尾后 /version")
        print("  ", json.dumps(final, ensure_ascii=False))
        if final["version"] != "..2026-v2":
            failures.append("旧请求收尾后把新目标换回去了")

    print("\n" + "=" * 64)
    if failures:
        print("演示校验失败：")
        for item in failures:
            print(" -", item)
        return 1
    print("演示通过：新投射先生效；慢旧请求收到 409 且未覆盖；查询未等待。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
