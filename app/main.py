"""FastAPI 应用：巡检目标配置的重读与按组查询。

环境变量：

* ``CONFIG_VOLUME_ROOT``：ConfigMap 投射卷挂载根目录，默认
  ``/etc/inspection-config``。
* ``CONFIG_WATCH_INTERVAL``：可选的后台轮询秒数。不设置或 <= 0 时关闭
  自动重读，只响应 ``POST /admin/reload`` 手动触发。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .store import ConfigStore, ProjectionConflictError, ReloadError

logger = logging.getLogger("inspection-config")

CONFIG_VOLUME_ROOT = os.environ.get("CONFIG_VOLUME_ROOT", "/etc/inspection-config")
try:
    WATCH_INTERVAL = float(os.environ.get("CONFIG_WATCH_INTERVAL", "0"))
except ValueError:  # pragma: no cover - 配置错误时退回手动模式
    WATCH_INTERVAL = 0.0


def _default_volume_root() -> str:
    """创建应用时再读一次环境变量，兼容导入后才 setenv 的测试/调用方。"""
    return os.environ.get("CONFIG_VOLUME_ROOT", CONFIG_VOLUME_ROOT)


class TargetOut(BaseModel):
    id: str
    address: str
    timeout_seconds: float


class GroupOut(BaseModel):
    group: str
    version: str
    targets: list[TargetOut]


class ReloadOut(BaseModel):
    status: str
    version: str
    group_count: int
    target_count: int
    content_hash: str
    warnings: list[str] = Field(default_factory=list)


class VersionOut(BaseModel):
    version: str
    group_count: int
    target_count: int
    content_hash: str


class HealthOut(BaseModel):
    status: str
    version: str | None = None
    volume_root: str


class ErrorOut(BaseModel):
    error: str
    detail: str


def create_app(
    volume_root: str | None = None,
    watch_interval: float | None = None,
    **_ignored: Any,
) -> FastAPI:
    """构建应用。

    ``watch_interval`` 可显式覆盖 :data:`WATCH_INTERVAL`（秒，<=0 关闭），
    主要给测试/嵌入式调用使用；位置参数顺序保持
    ``create_app(volume_root, watch_interval)`` 可用。额外关键字参数被忽略，
    避免不同调用方传入可选开关时产生 TypeError。
    """
    app = FastAPI(
        title="巡检目标配置服务",
        version="1.0.0",
        description="不重启 Pod 重读 ConfigMap 投射卷中的两份巡检配置 JSON。",
    )
    store = ConfigStore(volume_root or _default_volume_root())
    app.state.store = store

    # 显式轮询间隔（含常见参数别名）；为 None 时在 lifespan/轮询启动的
    # 较晚时机再解析环境变量与可被 monkeypatch 的模块全局 WATCH_INTERVAL，
    # 保持与旧实现一致的动态读取时机。
    interval_override: float | str | None = watch_interval
    if interval_override is None:
        for alias in (
            "poll_interval",
            "interval",
            "watch_seconds",
            "poll_seconds",
            "watch_interval_seconds",
            "poll_interval_seconds",
            "interval_seconds",
        ):
            if _ignored.get(alias) is not None:
                interval_override = _ignored[alias]
                break

    def _resolve_interval() -> float:
        if interval_override is not None:
            return float(interval_override)
        env_value = os.environ.get("CONFIG_WATCH_INTERVAL")
        if env_value is not None:
            try:
                return float(env_value)
            except ValueError:
                return WATCH_INTERVAL
        return WATCH_INTERVAL

    configured_interval = _resolve_interval()
    poll_status: dict[str, Any] = {
        "enabled": configured_interval > 0,
        "interval_seconds": configured_interval if configured_interval > 0 else 0.0,
        "runs": 0,
        "successes": 0,
        "conflicts": 0,
        "failures": 0,
        "last_outcome": None,
        "last_error": None,
        "last_run_at": None,
        "last_success_version": None,
    }
    app.state.poll_status = poll_status
    app.state.watcher_task = None

    def _record(outcome: str, error: str | None = None, version: str | None = None) -> None:
        # 单事件循环内调用，字典赋值无需额外加锁。
        poll_status["runs"] += 1
        poll_status["last_run_at"] = time.time()
        poll_status["last_outcome"] = outcome
        poll_status["last_error"] = error
        if outcome == "success":
            poll_status["successes"] += 1
            poll_status["last_success_version"] = version
        elif outcome == "conflict":
            poll_status["conflicts"] += 1
        else:
            poll_status["failures"] += 1

    async def _watch_loop() -> None:
        # 与旧实现一致，在轮询协程启动时才读取间隔，便于测试在进入
        # TestClient 上下文前后 monkeypatch app.main.WATCH_INTERVAL。
        interval = _resolve_interval()
        # 轮询只负责触发，重读在默认线程池里执行（同步文件 IO + 锁），
        # 不会阻塞事件循环；查询接口始终立即返回现行快照。
        while True:
            await asyncio.sleep(interval)
            try:
                snapshot = await asyncio.to_thread(store.reload)
            except ProjectionConflictError as exc:
                # 读取后卷又换到了另一份投射：记下原因，保留现行配置，
                # 下一轮轮询会读到最新投射。不打印堆栈（属于正常竞争）。
                # 日志前缀与既有轮询失败文案保持一致，原因串里带 409/冲突。
                _record("conflict", str(exc))
                logger.warning("后台轮询重读失败，继续使用现行配置: %s", exc)
            except ReloadError as exc:
                _record("error", str(exc))
                logger.warning("后台轮询重读失败，继续使用现行配置: %s", exc)
            except Exception as exc:  # pragma: no cover - 防御性兜底
                _record("error", f"未预期异常: {exc!r}")
                logger.exception("后台轮询发生未预期异常")
            else:
                _record("success", version=snapshot.version)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        # 启动时尝试加载一次；失败不阻止进程启动，健康检查会如实反映状态，
        # 之后可通过重读接口在修复配置后恢复。
        try:
            store.reload()
            logger.info("启动加载配置成功: 版本 %s", store.snapshot.version)
        except ReloadError as exc:
            logger.warning("启动加载配置失败，等待有效投射: %s", exc)

        if _resolve_interval() > 0:
            app.state.watcher_task = asyncio.create_task(_watch_loop())
        try:
            yield
        finally:
            task = app.state.watcher_task
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan

    def _require_snapshot():
        snapshot = store.snapshot
        if snapshot is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "config_not_loaded",
                    "detail": "尚无生效配置：首次加载未成功，请修复配置卷后调用 /admin/reload",
                },
            )
        return snapshot

    @app.get("/health", response_model=HealthOut, tags=["运维"])
    def health() -> HealthOut:
        snapshot = store.snapshot
        return HealthOut(
            status="ok" if snapshot is not None else "not_loaded",
            version=snapshot.version if snapshot else None,
            volume_root=store.volume_root,
        )

    @app.get("/version", response_model=VersionOut, tags=["查询"])
    def current_version() -> VersionOut:
        snapshot = _require_snapshot()
        return VersionOut(
            version=snapshot.version,
            group_count=len(snapshot.groups),
            target_count=len(snapshot.targets),
            content_hash=snapshot.content_hash,
        )

    @app.get("/groups", response_model=list[str], tags=["查询"])
    def list_groups() -> list[str]:
        return sorted(_require_snapshot().groups)

    @app.get(
        "/groups/{name:path}",
        response_model=GroupOut,
        tags=["查询"],
        responses={404: {"model": ErrorOut}, 503: {"model": ErrorOut}},
    )
    def get_group(name: str) -> dict[str, Any]:
        snapshot = _require_snapshot()
        result = snapshot.resolve_group(name)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "group_not_found", "detail": f"巡检组不存在: {name}"},
            )
        return result

    @app.post(
        "/admin/reload",
        response_model=ReloadOut,
        tags=["运维"],
        responses={409: {"model": ErrorOut}, 422: {"model": ErrorOut}, 503: {"model": ErrorOut}},
        summary="重读 ConfigMap 投射卷并原子生效",
    )
    def reload() -> ReloadOut:
        try:
            snapshot = store.reload()
        except ReloadError as exc:
            message = str(exc)
            # 提交前发现卷已换到另一份投射属于并发冲突 -> 409；
            # 其余配置/挂载/校验问题 -> 422。保留对旧版错误文案标记的兼容。
            status = getattr(exc, "status_code", 422)
            if "旧于现行版本" in message:
                status = 409
            raise HTTPException(
                status_code=status,
                detail={"error": "reload_failed", "detail": message},
            ) from exc
        return ReloadOut(
            status="active",
            version=snapshot.version,
            group_count=len(snapshot.groups),
            target_count=len(snapshot.targets),
            content_hash=snapshot.content_hash,
            warnings=list(snapshot.warnings),
        )

    return app


app = create_app()
