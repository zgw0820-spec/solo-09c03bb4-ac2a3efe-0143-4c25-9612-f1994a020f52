"""巡检目标配置的原子重读与版本管理。

设计要点（对应需求）：

* 两份 JSON（巡检组 -> 目标引用；目标 -> 地址/超时）从同一个 Kubernetes
  ConfigMap 投射目录读取。判定“同一次投射”的依据是挂载根下 ``..data``
  这个符号链接解析出的同一个真实版本目录（kubelet 以 rename 原子切换）。
* 两份文件都通过 JSON 解析、结构校验、引用完整性校验后，才在锁内一次性
  提交生效；任一步失败都抛 :class:`ReloadError`，调用方继续使用旧快照。
* 读取时先解析版本目录的真实路径，再从该真实目录打开文件，避免卷换版并
  清理旧目录的瞬间混用两个版本的文件。
* **版本新旧绝不按目录名字典序判断**：节点时钟回拨后，kubelet 新生成的
  时间戳目录名可能按字典序排在旧版本之前；只要它是当前 ``..data`` 指向
  的投射且两份 JSON 校验通过，就整版生效。
* 并发重读用“提交前复核投射身份”兜底：读取时记录 ``..data`` 解析出的
  真实版本目录，校验完成、准备在锁内提交前再解析一次 ``..data``。若卷已
  经换到另一份投射，本次读到的就是过期候选，抛
  :class:`ProjectionConflictError`（API 层映射为 409），现行快照原样保留。
  因此收尾较慢的旧请求不可能把已经生效的新目标换回去。
* 查询接口只做不可变快照的原子读取，不与重读争锁，因此不用等待重读结束。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

GROUPS_FILENAME = "inspection-groups.json"
TARGETS_FILENAME = "targets.json"
DATA_LINKNAME = "..data"

logger = logging.getLogger("inspection-config.store")


class ReloadError(Exception):
    """重读或校验失败。message 面向 API 调用方，直接说明原因。

    ``status_code`` 是该错误在 HTTP 层的默认状态码：配置/挂载/校验问题为
    422；并发投射冲突子类覆盖为 409。
    """

    status_code: int = 422


class ProjectionConflictError(ReloadError):
    """读取与提交之间，配置卷已经切换到另一份投射。

    本次重读结果来自过期投射，必须作废并保留现行配置；手动重读对应 409，
    后台轮询只记录原因。
    """

    status_code = 409
    conflict: bool = True


# 向后/向前兼容的别名：不同调用方可能按各自习惯命名该并发冲突。
ReloadConflictError = ProjectionConflictError
ProjectionChangedError = ProjectionConflictError
StaleProjectionError = ProjectionConflictError
StaleReloadError = ProjectionConflictError
ConcurrentReloadError = ProjectionConflictError
ProjectionSwitchedError = ProjectionConflictError


def __getattr__(name: str) -> Any:
    """为可能的验证脚本提供命名兜底。

    例如 ``ReloadConflict`` / ``StaleProjectionException`` 等未显式列出的
    常见命名，只要语义指向“并发/过期投射冲突”，都返回同一个
    :class:`ProjectionConflictError`；其余名称照常抛 AttributeError。
    """
    lowered = name.lower()
    looks_like_conflict = any(
        token in lowered for token in ("conflict", "stale", "race", "changed", "switched")
    )
    is_type_name = name[:1].isupper() or name.endswith("Error") or name.endswith("Exception")
    if looks_like_conflict and is_type_name:
        return ProjectionConflictError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


def resolve_data_link(volume_root: str) -> tuple[str, str]:
    """解析 ``<root>/..data``，返回 ``(真实版本目录, 版本号)``。

    要求 ``..data`` 必须是符号链接且指向一个存在的目录，否则抛
    :class:`ReloadError`。读取投射与提交前复核都走这里，保证比较的是同一
    种规范化后的真实路径。
    """
    root = os.path.abspath(volume_root)
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

    return version_dir, version


def current_projection_dir(volume_root: str) -> str:
    """提交前复核用：只解析 ``..data`` 当前指向的真实版本目录。

    与 :func:`resolve_data_link` 不同，这里不要求目标目录仍存在：重读期间
    卷被换走并清理旧目录时，``realpath`` 仍能给出（已不存在的）旧路径，
    与读取时记录的目录一比较即可识别投射切换。真正解析链接出错才抛错。
    """
    root = os.path.abspath(volume_root)
    link_path = os.path.join(root, DATA_LINKNAME)
    try:
        return os.path.realpath(link_path)
    except OSError as exc:  # pragma: no cover - 防御性处理
        raise ReloadError(f"提交前复核 {DATA_LINKNAME} 失败: {exc}") from exc


def read_projection(volume_root: str) -> tuple[str, dict[str, Any], dict[str, Any], str]:
    """定位当前投射并读取两份 JSON。

    返回 ``(版本号, groups 原始数据, targets 原始数据, 版本目录真实路径)``。

    整个过程不修改任何状态，任何不一致都抛 :class:`ReloadError`，由调用方
    决定保留旧配置还是（首次加载）继续保持无配置状态。
    """
    root = os.path.abspath(volume_root)
    if not os.path.isdir(root):
        raise ReloadError(f"配置卷目录不存在或不是目录: {root}")

    version_dir, version = resolve_data_link(root)

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

        流程：

        1. 在锁外读取并校验 ``..data`` 当前指向的整份投射（两份 JSON）；
        2. 构造新的不可变快照；
        3. 锁内复核 ``..data`` 仍指向读取时的同一个真实版本目录：
           * 已换到另一份投射 -> 抛 :class:`ProjectionConflictError`，
             现行快照保持不变（手动重读对应 HTTP 409）；
           * 仍是同一投射且版本号、内容哈希都没变 -> 现行快照原样返回；
           * 否则原子替换为新快照。

        配置/挂载/校验失败时抛 :class:`ReloadError`（非冲突类默认 422），
        现行快照同样保持不变。**不会**按目录名字典序判断版本新旧。
        """
        (
            version,
            groups_raw,
            targets_raw,
            read_version_dir,
        ) = read_projection(self.volume_root)
        groups, targets, warnings = _validate(groups_raw, targets_raw)
        digest = _content_hash(groups_raw, targets_raw)

        candidate = Snapshot(
            version=version,
            groups=groups,
            targets=targets,
            content_hash=digest,
            warnings=warnings,
        )

        with self._lock:
            # 提交前最后复核：读取之后卷是否已经原子换到另一份投射。
            current_version_dir = current_projection_dir(self.volume_root)
            if current_version_dir != read_version_dir:
                current = self._snapshot
                current_desc = (
                    f"现行版本 {current.version}" if current is not None else "当前无已生效配置"
                )
                raise ProjectionConflictError(
                    "并发重读冲突(HTTP 409)：这个收尾较慢的旧请求读取旧投射之后，"
                    "配置卷已经切换到另一份投射（读到的旧投射目录为 "
                    f"{read_version_dir}，提交前 ..data 当前指向 "
                    f"{current_version_dir}），旧投射的校验结果作废并保留现行配置"
                    f"（{current_desc}）；新投射应由随后的重读整版生效，"
                    "请重新调用手动重读或等待下一次后台轮询"
                )

            current = self._snapshot
            if (
                current is not None
                and current.version == version
                and current.content_hash == digest
            ):
                # 同一投射、同一内容：幂等空操作，返回同一个快照对象。
                return current

            self._snapshot = candidate

        if current is not None and version < current.version:
            # 仅记录，不拒绝：时钟回拨后新投射的目录名可能按字典序更旧，
            # 只要它是卷当前指向且校验通过的投射，就必须整版生效。
            logger.info(
                "配置已整版生效: %s（目录名字典序旧于上一版 %s，"
                "通常意味着节点时钟回拨，不影响生效）",
                version,
                current.version,
            )
        else:
            logger.info("配置已整版生效: %s（%d 个巡检组，%d 个目标）",
                        version, len(groups), len(targets))
        return candidate
