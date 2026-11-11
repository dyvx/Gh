"""数据库首次就绪检查

Serverless 平台上不能依赖「启动时跑一次」的 lifespan 钩子或单独的迁移步骤，
所以把建表 / 创建系统用户 / 加载系统配置做成幂等的懒初始化：
每个实例在第一次访问数据库前执行一次。
"""

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import DEFAULT_SECRET_KEY, is_serverless, settings

logger = logging.getLogger(__name__)

GUEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

_ready = False
_last_error: str | None = None


def last_error() -> str | None:
    return _last_error


def is_ready() -> bool:
    return _ready


async def _create_tables() -> None:
    from app.db.base import Base
    from app.db.session import engine
    import app.models  # noqa: F401  确保所有模型都已注册到 metadata

    for attempt in range(2):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception:
            # 两个实例同时冷启动时可能撞上「表已存在」，重试一次即可
            if attempt == 1:
                raise


async def _ensure_system_users() -> None:
    from app.core.security import hash_password
    from app.db.session import AsyncSessionLocal
    from app.models.user import User

    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(User.id).where(User.id == GUEST_USER_ID))
        if not existing.scalar_one_or_none():
            db.add(User(
                id=GUEST_USER_ID,
                email="system_guest@virtual-gf.local",
                username="系统访客",
                password_hash=hash_password(uuid.uuid4().hex),
            ))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()

    # 可选：用环境变量引导创建第一个管理员
    if settings.admin_email and settings.admin_password:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.email == settings.admin_email))
            admin = result.scalar_one_or_none()
            if admin is None:
                username = settings.admin_username or "admin"
                taken = await db.execute(select(User.id).where(User.username == username))
                if taken.scalar_one_or_none():
                    username = f"{username}_{uuid.uuid4().hex[:6]}"
                db.add(User(
                    email=settings.admin_email,
                    username=username,
                    password_hash=hash_password(settings.admin_password),
                    is_admin=True,
                    membership_tier="vip",
                ))
            elif not admin.is_admin:
                admin.is_admin = True
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()


async def _load_config() -> None:
    from app.db.session import AsyncSessionLocal
    from app.services.system_config import load_system_config

    async with AsyncSessionLocal() as db:
        await load_system_config(db)


async def ensure_db_ready(require_secret: bool = True) -> None:
    """幂等。成功一次后只做轻量的配置刷新；失败则抛 503 并在下次请求重试。"""
    global _ready, _last_error

    # 线上环境必须显式配置 SECRET_KEY，否则任何人都能用默认密钥伪造登录令牌
    if require_secret and is_serverless() and settings.secret_key == DEFAULT_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="服务尚未配置：请在部署平台的环境变量中设置 SECRET_KEY (SECRET_KEY is not configured)",
        )

    if _ready:
        from app.services.system_config import is_stale
        if is_stale():
            try:
                await _load_config()
            except Exception:
                logger.exception("刷新系统配置失败")
        return

    try:
        if settings.auto_create_tables:
            await _create_tables()
        await _ensure_system_users()
        await _load_config()
    except Exception as e:
        # 只记录异常类型：错误详情可能包含主机名 / 用户名，完整信息看服务端日志
        _last_error = type(e).__name__
        logger.exception("数据库初始化失败")
        raise HTTPException(
            status_code=503,
            detail="数据库未就绪，请检查 DATABASE_URL 配置 (database not ready)",
        ) from e

    _ready = True
    _last_error = None
    logger.info("数据库已就绪")
