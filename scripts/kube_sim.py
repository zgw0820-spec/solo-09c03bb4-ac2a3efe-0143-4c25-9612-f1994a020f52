#!/usr/bin/env python3
"""模拟 kubelet 对 ConfigMap 投射卷的原子换版。

真实行为参照 Kubernetes：

    <root>/..data -> ..YYYY-MM-DDTHH_MM_SS.NNNNNNNNN-Z/..YYYY..Z   (相对符号链接)
    <root>/..YYYY..Z/                                            (真实版本目录)
    <root>/inspection-groups.json -> ..data/inspection-groups.json
    <root>/targets.json             -> ..data/targets.json

换版时先写好新的时间戳目录与全部符号链接，再用 rename 原子替换
``..data``；prune 时删除不再被 ``..data`` 引用的旧版本目录。

为了演练“引用缺失”和“两版混用”，脚本支持在发布时故意制造坏投射。

子命令：
  init          生成首个有效版本（v1 示例数据）
  publish v2    发布一个新版本并原子切换 ..data
  bad-swap      发布一份引用缺失的坏投射并切换（两文件仍同版）
  mixed-swap    制造混用：..data 指向新版本，但一个文件链接指向旧版本
  prune         删除除当前版本外的所有时间戳目录
  status        打印当前 ..data 解析结果与文件链接
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

GROUPS_FILENAME = "inspection-groups.json"
TARGETS_FILENAME = "targets.json"
DATA_LINKNAME = "..data"

V1_GROUPS = {
    "groups": [
        {"name": "db-inspection", "target_ids": ["mysql-primary", "mysql-replica"]},
        {"name": "network-inspection", "target_ids": ["router-core", "firewall-edge"]},
    ]
}
V1_TARGETS = {
    "targets": [
        {"id": "mysql-primary", "address": "10.0.1.10:3306", "timeout_seconds": 3.0},
        {"id": "mysql-replica", "address": "10.0.1.11:3306", "timeout_seconds": 3.0},
        {"id": "router-core", "address": "10.0.2.1:22", "timeout_seconds": 5.0},
        {"id": "firewall-edge", "address": "10.0.2.254:22", "timeout_seconds": 5.0},
    ]
}

V2_GROUPS = {
    "groups": [
        {"name": "db-inspection", "target_ids": ["mysql-primary", "mysql-replica"]},
        {"name": "network-inspection", "target_ids": ["router-core", "firewall-edge"]},
        {"name": "cache-inspection", "target_ids": ["redis-cluster"]},
    ]
}
V2_TARGETS = {
    "targets": [
        {"id": "mysql-primary", "address": "10.0.1.10:3306", "timeout_seconds": 2.5},
        {"id": "mysql-replica", "address": "10.0.1.11:3306", "timeout_seconds": 2.5},
        {"id": "router-core", "address": "10.0.2.1:22", "timeout_seconds": 4.0},
        {"id": "firewall-edge", "address": "10.0.2.254:22", "timeout_seconds": 4.0},
        {"id": "redis-cluster", "address": "10.0.3.8:6379", "timeout_seconds": 1.5},
    ]
}

# 故意引用 targets 里不存在的目标。
BAD_GROUPS = {
    "groups": [
        {"name": "db-inspection", "target_ids": ["mysql-primary", "ghost-target"]},
    ]
}
BAD_TARGETS = {
    "targets": [
        {"id": "mysql-primary", "address": "10.0.1.10:3306", "timeout_seconds": 3.0},
    ]
}


def kube_timestamp() -> str:
    # 纳秒精度，字典序即时间序，和 kubelet 的命名风格保持一致。
    now = datetime.now(timezone.utc)
    return now.strftime("..%Y-%m-%dT%H_%M_%S.") + f"{now.microsecond * 1000:09d}-Z"


def atomic_symlink(link_path: Path, target: str) -> None:
    """用临时链接 + rename 原子建立/替换符号链接。"""
    tmp_name = f".tmp-{link_path.name}-{os.getpid()}"
    tmp_path = link_path.parent / tmp_name
    if tmp_path.is_symlink() or tmp_path.exists():
        tmp_path.unlink()
    os.symlink(target, tmp_path)
    os.replace(tmp_path, link_path)


def write_version_dir(root: Path, version: str, files: dict[str, dict]) -> Path:
    version_dir = root / version
    version_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in files.items():
        (version_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return version_dir


def publish(
    root: Path,
    files: dict[str, dict],
    *,
    version: str | None = None,
    mixed_from_version: str | None = None,
    mixed_file: str | None = None,
) -> str:
    """写新版本目录并原子切换 ..data，返回新版本号。

    mixed_from_version / mixed_file：把根目录下指定文件的符号链接改指旧
    版本目录，制造两版混用，供服务端拒绝逻辑演练。
    """
    root.mkdir(parents=True, exist_ok=True)
    version = version or kube_timestamp()
    write_version_dir(root, version, files)

    # ..data 是指向版本目录的相对链接（与 kubelet 行为一致）。
    atomic_symlink(root / DATA_LINKNAME, version)

    for name in (GROUPS_FILENAME, TARGETS_FILENAME):
        if mixed_from_version and name == mixed_file:
            # 故意绕过 ..data，直接指向旧版本目录里的同名文件。
            target = f"{mixed_from_version}/{name}"
        else:
            target = f"{DATA_LINKNAME}/{name}"
        atomic_symlink(root / name, target)
    return version


def current_version(root: Path) -> str | None:
    link = root / DATA_LINKNAME
    if not link.is_symlink():
        return None
    return os.path.basename(os.path.realpath(link))


def prune(root: Path) -> list[str]:
    keep = current_version(root)
    if keep is None:
        raise SystemExit(f"{DATA_LINKNAME} 不存在，拒绝清理：{root}")
    removed: list[str] = []
    for entry in root.iterdir():
        if entry.is_dir() and entry.name != keep and not entry.is_symlink():
            shutil.rmtree(entry)
            removed.append(entry.name)
    return removed


def status(root: Path) -> None:
    link = root / DATA_LINKNAME
    if not link.is_symlink():
        print(f"[status] {root} 下没有 {DATA_LINKNAME}，尚未初始化")
        return
    print(f"[status] {DATA_LINKNAME} -> {os.readlink(link)}")
    print(f"[status] 真实版本目录: {os.path.realpath(link)}")
    for name in (GROUPS_FILENAME, TARGETS_FILENAME):
        entry = root / name
        if entry.is_symlink():
            print(f"[status] {name} -> {os.readlink(entry)}  => {os.path.realpath(entry)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟 K8s ConfigMap 投射卷换版")
    parser.add_argument(
        "--root",
        default=os.environ.get("SIM_VOLUME_ROOT", "volumes/config"),
        help="投射卷根目录（默认 volumes/config）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    pub = sub.add_parser("publish")
    pub.add_argument("revision", choices=["v1", "v2"])
    sub.add_parser("bad-swap")
    sub.add_parser("mixed-swap")
    sub.add_parser("prune")
    sub.add_parser("status")

    args = parser.parse_args()
    root = Path(args.root).resolve()

    if args.cmd == "init":
        if (root / DATA_LINKNAME).is_symlink():
            print(f"[init] 已存在投射，先执行 prune/清理；当前版本: {current_version(root)}")
            return 1
        version = publish(root, {GROUPS_FILENAME: V1_GROUPS, TARGETS_FILENAME: V1_TARGETS})
        print(f"[init] 首个版本已发布: {version}")
        status(root)
        return 0

    if args.cmd == "publish":
        payloads = {
            "v1": {GROUPS_FILENAME: V1_GROUPS, TARGETS_FILENAME: V1_TARGETS},
            "v2": {GROUPS_FILENAME: V2_GROUPS, TARGETS_FILENAME: V2_TARGETS},
        }[args.revision]
        old = current_version(root)
        version = publish(root, payloads)
        print(f"[publish] {old} -> {version}（{args.revision} 示例数据，..data 原子切换）")
        status(root)
        return 0

    if args.cmd == "bad-swap":
        old = current_version(root)
        version = publish(root, {GROUPS_FILENAME: BAD_GROUPS, TARGETS_FILENAME: BAD_TARGETS})
        print(f"[bad-swap] {old} -> {version}（引用缺失：ghost-target 不存在）")
        status(root)
        return 0

    if args.cmd == "mixed-swap":
        old = current_version(root)
        if old is None:
            print("[mixed-swap] 请先 init 建立一个旧版本")
            return 1
        # 新目录放 v2，但根目录的 groups 文件链接故意留在旧版本目录。
        version = publish(
            root,
            {GROUPS_FILENAME: V2_GROUPS, TARGETS_FILENAME: V2_TARGETS},
            mixed_from_version=old,
            mixed_file=GROUPS_FILENAME,
        )
        print(
            f"[mixed-swap] {old} -> {version}，但 {GROUPS_FILENAME} 链接仍指向旧版目录"
            "（模拟两版混用，服务端必须拒绝）"
        )
        status(root)
        return 0

    if args.cmd == "prune":
        removed = prune(root)
        print(f"[prune] 保留 {current_version(root)}，删除旧目录: {removed or '无'}")
        return 0

    if args.cmd == "status":
        status(root)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
