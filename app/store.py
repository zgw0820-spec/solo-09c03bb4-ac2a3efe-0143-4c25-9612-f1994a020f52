"""巡检目标配置的原子重读与版本管理。

设计要点（对应需求）：

* 两份 JSON（巡检组 -> 目标引用；目标 -> 地址/超时）从同一个 Kubernetes
  ConfigMap 投射目录读取。判定“同一次投射”的依据是挂载根下 ``..data``
  这个符号链接解析出的同一个真实版本目录（kubelet 以 rename 原子切换）。
* 两份文件都通过 JSON 解析、结构校验、引用完整性校验后，才在锁内一次性
  提交生效；任一步失败都抛 :class:`ReloadError`，调用方继续使用旧快照。
* 读取时先解析版本目录的真实路径，再从该真实目录打开文件，避免卷换版并
  清理旧目录的瞬间混用两个版本的文件。
* 并发重读由锁内的版本比较兜底：候选版本不新于现行版本时拒绝提交，较慢
  的旧版读取不会覆盖已经生效的新版。
* 查询接口只做不可变快照的原子读取，不与重读争锁，因此不用等待重读结束。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any

GROUPS_FILENAME = "inspection-groups.json"
TARGETS_FILENAME = "targets.json"
DATA_LINKNAME = "..data"


class ReloadError(Exception):
    """重读或校验失败。message 面向 API 调用方，直接说明原因。"""


@dataclass(frozen=True)
class Target:
    id: str
    address: str
    timeout_seconds: float


@dataclass(frozen=True)
class InspectionGroup:
    name: str
    target_ids: tuple[str, ...]


@dataclass(frozen=True)
class Snapshot:
    """一次成功生效的不可变配置版本。"""

    version: str
    groups: dict[str, InspectionGroup]
    targets: dict[str, Target]
    content_hash: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def resolve_group(self, name: str) -> dict[str, Any] | None:
        group = self.groups.get(name)
        if group is None:
            return None
        return {
            "group": group.name,
            "version": self.version,
            "targets": [
                {
                    "id": target_id,
                    "address": self.targets[target_id].address,
                    "timeout_seconds": self.targets[target_id].timeout_seconds,
                }
                for target_id in group.target_ids
            ],
        }


def read_projection(volume_root: str) -> tuple[str, dict[str, Any], dict[str, Any], str]:
    """定位当前投射并读取两份 JSON。

    返回 ``(版本号, groups 原始数据, targets 原始数据, 版本目录真实路径)``。

    整个过程不修改任何状态，任何不一致都抛 :class:`ReloadError`，由调用方
    决定保留旧配置还是（首次加载）继续保持无配置状态。
    """
    root = os.path.abspath(volume_root)
    if not os.path.isdir(root):
        raise ReloadError(f"配置卷目录不存在或不是目录: {root}")

    link_path = os.path.join(root, DATA_LINKNAME)
    if not os.path.islink(link_path):
        raise ReloadError(
            f"缺少 {DATA_LINKNAME} 符号链接，挂载点不像 Kubernetes ConfigMap 投射卷: {root}"
        )

    try:
        version_dir = os.path.realpath(link_path)
    except OSError as exc:  # pragma: no cover - realpath 极少抛错，防御性处理
        raise ReloadError(f"解析 {DATA_LINKNAME} 链接失败: {exc}") from exc

    if not os.path.isdir(version_dir):
        raise ReloadError(f"{DATA_LINKNAME} 指向的版本目录已不存在: {version_dir}")

    version = os.path.basename(version_dir)
    if not version or version == os.path.dirname(version_dir):
        raise ReloadError(f"无法从版本目录推导版本号: {version_dir}")

    payloads: dict[str, dict[str, Any]] = {}
    for filename in (GROUPS_FILENAME, TARGETS_FILENAME):
        file_path = os.path.join(version_dir, filename)
        link_at_root = os.path.join(root, filename)

        # K8s ConfigMap 卷里根目录的文件本身也是符号链接，且必须指向
        # ..data 解析出的同一版本目录，防止两版文件被混挂。
        if os.path.islink(link_at_root):
            real_file = os.path.realpath(link_at_root)
            if os.path.dirname(real_file) != version_dir:
                raise ReloadError(
                    f"{filename} 的符号链接指向 {real_file}，与 {DATA_LINKNAME} "
                    f"解析出的版本目录 {version_dir} 不一致，拒绝混用不同版本"
                )
            open_path = real_file
        elif os.path.exists(link_at_root):
            # 非标准挂载（例如本地直接放文件）：以 ..data 真实目录下的文件为准。
            open_path = file_path
        else:
            raise ReloadError(f"版本目录 {version_dir} 中缺少文件: {filename}")

        if not os.path.isfile(open_path):
            raise ReloadError(f"配置文件不存在或不是普通文件: {open_path}")

        try:
            with open(open_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ReloadError(f"{filename} 不是合法 JSON: {exc.msg}（第 {exc.lineno} 行）") from exc
        except OSError as exc:
            raise ReloadError(f"读取 {filename} 失败: {exc}") from exc

        if not isinstance(raw, dict):
            raise ReloadError(f"{filename} 顶层必须是 JSON 对象，实际为 {type(raw).__name__}")
        payloads[filename] = raw

    return version, payloads[GROUPS_FILENAME], payloads[TARGETS_FILENAME], version_dir


def _validate(
    groups_raw: dict[str, Any], targets_raw: dict[str, Any]
) -> tuple[dict[str, InspectionGroup], dict[str, Target], tuple[str, ...]]:
    warnings: list[str] = []

    groups_field = groups_raw.get("groups")
    if not isinstance(groups_field, list):
        raise ReloadError(f"{GROUPS_FILENAME} 必须包含数组字段 \"groups\"")

    groups: dict[str, InspectionGroup] = {}
    for index, item in enumerate(groups_field):
        if not isinstance(item, dict):
            raise ReloadError(f"groups[{index}] 必须是对象")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ReloadError(f"groups[{index}].name 必须是非空字符串")
        if name in groups:
            raise ReloadError(f"巡检组名称重复: {name}")
        target_ids = item.get("target_ids")
        if not isinstance(target_ids, list) or not target_ids:
            raise ReloadError(f"巡检组 {name} 的 target_ids 必须是非空数组")
        normalized_ids: list[str] = []
        for target_id in target_ids:
            if not isinstance(target_id, str) or not target_id.strip():
                raise ReloadError(f"巡检组 {name} 的 target_ids 中存在非法条目: {target_id!r}")
            normalized_ids.append(target_id)
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ReloadError(f"巡检组 {name} 的 target_ids 存在重复引用")
        groups[name] = InspectionGroup(name=name, target_ids=tuple(normalized_ids))

    targets_field = targets_raw.get("targets")
    if not isinstance(targets_field, list):
        raise ReloadError(f"{TARGETS_FILENAME} 必须包含数组字段 \"targets\"")

    targets: dict[str, Target] = {}
    for index, item in enumerate(targets_field):
        if not isinstance(item, dict):
            raise ReloadError(f"targets[{index}] 必须是对象")
        target_id = item.get("id")
        if not isinstance(target_id, str) or not target_id.strip():
            raise ReloadError(f"targets[{index}].id 必须是非空字符串")
        if target_id in targets:
            raise ReloadError(f"目标 id 重复: {target_id}")
        address = item.get("address")
        if not isinstance(address, str) or not address.strip():
            raise ReloadError(f"目标 {target_id} 的 address 必须是非空字符串")
        timeout = item.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ReloadError(f"目标 {target_id} 的 timeout_seconds 必须是数字")
        timeout_value = float(timeout)
        if timeout_value <= 0:
            raise ReloadError(f"目标 {target_id} 的 timeout_seconds 必须大于 0")
        targets[target_id] = Target(
            id=target_id, address=address.strip(), timeout_seconds=timeout_value
        )

    # 引用完整性：每个巡检组引用的目标必须都能在同一份投射里找到。
    missing: list[str] = []
    for group in groups.values():
        for target_id in group.target_ids:
            if target_id not in targets:
                missing.append(f"巡检组 {group.name} 引用了不存在的目标 {target_id}")
    if missing:
        raise ReloadError("引用完整性校验失败: " + "；".join(missing))

    referenced = {target_id for group in groups.values() for target_id in group.target_ids}
    unused = sorted(target_id for target_id in targets if target_id not in referenced)
    if unused:
        warnings.append("以下目标未被任何巡检组引用: " + ", ".join(unused))

    return groups, targets, tuple(warnings)


def _content_hash(groups_raw: dict[str, Any], targets_raw: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"groups": groups_raw, "targets": targets_raw},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ConfigStore:
    """线程安全的现行配置持有者。"""

    def __init__(self, volume_root: str) -> None:
        self.volume_root = volume_root
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    @property
    def snapshot(self) -> Snapshot | None:
        # 快照本身不可变，引用读取是原子的，查询路径完全不需要拿锁。
        return self._snapshot

    def reload(self) -> Snapshot:
        """重读当前投射并在校验通过后原子生效。

        失败时抛 :class:`ReloadError`，现行快照保持不变。
        """
        version, groups_raw, targets_raw, _ = read_projection(self.volume_root)
        groups, targets, warnings = _validate(groups_raw, targets_raw)
        digest = _content_hash(groups_raw, targets_raw)

        with self._lock:
            current = self._snapshot
            if current is not None and version < current.version:
                raise ReloadError(
                    f"候选版本 {version} 旧于现行版本 {current.version}，拒绝被慢速重读覆盖"
                )
            if (
                current is not None
                and version == current.version
                and digest == current.content_hash
            ):
                return current
            snapshot = Snapshot(
                version=version,
                groups=groups,
                targets=targets,
                content_hash=digest,
                warnings=warnings,
            )
            self._snapshot = snapshot
            return snapshot
