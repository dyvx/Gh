"""FastAPI 应用入口"""

import logging
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动/关闭时执行"""
    from app.db.init import ensure_db_ready
    from app.db.session import engine

    # 建表 / 系统用户 / 系统配置。失败不阻止启动：
    # 同样的初始化会在第一次访问数据库时重试（Serverless 平台不保证 lifespan 一定执行）。
    try:
        await ensure_db_ready()
        print("[OK] 数据库已就绪")
    except Exception as e:
        print(f"[WARN] 数据库未就绪: {getattr(e, 'detail', e)}")
        print("  请先启动 docker compose 或配置 DATABASE_URL")

    yield

    # 关闭时：释放资源
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="虚拟女友 API",
        description="商用级 AI 聊天伴侣后端服务",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS
    origins = [o.strip() for o in settings.cors_origins.split(",")]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/health")
    async def api_health():
        """部署自检：环境变量和数据库是否配置正确（只返回状态，不泄露任何配置内容）"""
        from app.core.config import DEFAULT_SECRET_KEY
        from app.db import init as db_init

        checks = {
            "secret_key": "ok" if settings.secret_key != DEFAULT_SECRET_KEY else "missing",
            "database": "ok",
        }
        try:
            await db_init.ensure_db_ready(require_secret=False)
        except HTTPException:
            checks["database"] = f"error: {db_init.last_error() or 'unknown'}"
        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "error", **checks},
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(_request: Request, exc: Exception):
        logger.error(f"Unhandled error: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务暂时不可用，请稍后再试"},
        )

    # 注册 REST 路由
    from app.api.v1.router import api_router
    app.include_router(api_router, prefix="/api/v1")

    # 注册 WebSocket 路由
    from app.api.v1.ws import router as ws_router
    app.include_router(ws_router, prefix="/ws/chat")

    # 头像等上传文件存在数据库里，由 /api/v1/files/{key} 提供（见 app/services/storage.py），
    # 不再挂载本地 static 目录 —— Serverless 平台的文件系统是只读的。

    return app


app = create_app()
