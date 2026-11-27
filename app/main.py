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
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .store import ConfigStore, ReloadError

logger = logging.getLogger("inspection-config")

CONFIG_VOLUME_ROOT = os.environ.get("CONFIG_VOLUME_ROOT", "/etc/inspection-config")
try:
    WATCH_INTERVAL = float(os.environ.get("CONFIG_WATCH_INTERVAL", "0"))
except ValueError:  # pragma: no cover - 配置错误时退回手动模式
    WATCH_INTERVAL = 0.0


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


def create_app(volume_root: str | None = None) -> FastAPI:
    app = FastAPI(
        title="巡检目标配置服务",
        version="1.0.0",
        description="不重启 Pod 重读 ConfigMap 投射卷中的两份巡检配置 JSON。",
    )
    store = ConfigStore(volume_root or CONFIG_VOLUME_ROOT)
    app.state.store = store
    app.state.watcher_task = None

    async def _watch_loop() -> None:
        # 轮询只负责触发，重读在默认线程池里执行（同步文件 IO + 锁），
        # 不会阻塞事件循环；查询接口始终立即返回现行快照。
        interval = WATCH_INTERVAL
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(store.reload)
            except ReloadError as exc:
                logger.warning("后台轮询重读失败，继续使用现行配置: %s", exc)
            except Exception:  # pragma: no cover - 防御性兜底
                logger.exception("后台轮询发生未预期异常")

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        # 启动时尝试加载一次；失败不阻止进程启动，健康检查会如实反映状态，
        # 之后可通过重读接口在修复配置后恢复。
        try:
            store.reload()
            logger.info("启动加载配置成功: 版本 %s", store.snapshot.version)
        except ReloadError as exc:
            logger.warning("启动加载配置失败，等待有效投射: %s", exc)

        if WATCH_INTERVAL > 0:
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
            # 旧版慢速重读属于并发冲突，语义上是 409；其余为配置/挂载问题。
            status = 409 if "旧于现行版本" in message else 422
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
