"""数据库会话管理

同时兼容两种运行方式：
- 传统长驻进程（Docker / 本地）：使用连接池
- Serverless（Vercel）：不保留连接池（NullPool），并兼容 PgBouncer 事务模式
  （Neon / Supabase 的 "pooled" 连接串）
"""

import uuid

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import is_serverless, settings

_LOCAL_DEFAULT = "postgresql+asyncpg://postgres:postgres@localhost:5432/virtual_gf"


def _raw_database_url() -> str:
    """DATABASE_URL 优先；没配置时回退到托管集成注入的 POSTGRES_URL。"""
    if settings.database_url and settings.database_url != _LOCAL_DEFAULT:
        return settings.database_url
    if settings.postgres_url:
        return settings.postgres_url
    return _LOCAL_DEFAULT


def build_engine_config(raw_url: str, serverless: bool) -> tuple[URL, dict]:
    """把各种写法的 Postgres 连接串规范化成 asyncpg 能用的形式。

    托管数据库给出的连接串通常是 ``postgres://...?sslmode=require&channel_binding=require``，
    asyncpg 不认识这些 libpq 参数，直接传会抛 TypeError，所以这里统一处理。
    """
    url = make_url(raw_url.strip())

    if url.drivername in ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"):
        url = url.set(drivername="postgresql+asyncpg")

    connect_args: dict = {}
    engine_kwargs: dict = {}

    if url.drivername == "postgresql+asyncpg":
        query = dict(url.query)
        ssl_param = query.pop("ssl", None)
        sslmode = query.pop("sslmode", None) or ssl_param
        # libpq 专用参数，asyncpg 不支持
        for key in ("channel_binding", "pgbouncer", "connect_timeout", "options",
                    "supa", "connection_limit", "pool_timeout", "application_name"):
            query.pop(key, None)
        url = url.set(query=query)

        if isinstance(sslmode, (tuple, list)):
            sslmode = sslmode[0]
        if sslmode and sslmode not in ("disable", "allow", "false", "0"):
            # "prefer" 保持 asyncpg 默认行为，其余等价于 libpq 的 sslmode=require
            if sslmode != "prefer":
                connect_args["ssl"] = "require"

        if serverless:
            # PgBouncer 事务模式下不能复用预编译语句
            connect_args["statement_cache_size"] = 0
            connect_args["prepared_statement_cache_size"] = 0
            connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid.uuid4()}__"
            connect_args.setdefault("timeout", 15)

    if serverless:
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs["pool_size"] = 20
        engine_kwargs["max_overflow"] = 10
        engine_kwargs["pool_pre_ping"] = True

    if connect_args:
        engine_kwargs["connect_args"] = connect_args
    return url, engine_kwargs


_url, _engine_kwargs = build_engine_config(_raw_database_url(), is_serverless())
engine = create_async_engine(_url, echo=settings.debug, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    """FastAPI 依赖: 获取数据库会话"""
    from app.db.init import ensure_db_ready

    await ensure_db_ready()
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
