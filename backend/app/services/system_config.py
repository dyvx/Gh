"""系统级 API 配置持久化 — 数据库存储 + 内存同步

原实现写本地 system_config.json。Serverless 平台文件系统只读、且同时存在多个实例，
所以改为存数据库：
- 环境变量（PAINTING_API_KEY 等）提供默认值
- 管理员在后台保存过的项覆盖默认值，存在 system_settings 表里
- 每个实例定期重新读取，保证多个实例最终一致
"""

import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.system_setting import SystemSetting

SETTINGS_KEY = "system"
REFRESH_INTERVAL = 60  # 秒

DEFAULTS = {
    "painting_enabled": True,
    "painting_api_key": "",
    "painting_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "painting_model": "doubao-seedream-4-5-251128",
    "painting_size": "2048x2048",
    "vision_enabled": True,
    "vision_api_key": "",
    "vision_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "vision_model": "doubao-seed-2-0-pro",
}

_last_loaded: float = 0.0


def _apply(data: dict) -> None:
    for key in DEFAULTS:
        if key in data and hasattr(settings, key):
            val = data[key]
            if isinstance(DEFAULTS[key], bool) and not isinstance(val, bool):
                val = str(val).lower() in ("true", "1", "yes")
            setattr(settings, key, val)


async def load_system_config(db: AsyncSession) -> None:
    """从数据库加载管理员保存过的配置，覆盖环境变量默认值"""
    global _last_loaded
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == SETTINGS_KEY))
    row = result.scalar_one_or_none()
    if row and isinstance(row.value, dict):
        _apply(row.value)
    _last_loaded = time.monotonic()


def is_stale() -> bool:
    return time.monotonic() - _last_loaded > REFRESH_INTERVAL


def get_system_config() -> dict:
    """获取当前系统配置"""
    return {key: getattr(settings, key, DEFAULTS[key]) for key in DEFAULTS}


async def update_system_config(data: dict, db: AsyncSession) -> dict:
    """更新系统配置（部分更新），返回完整新状态"""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == SETTINGS_KEY))
    row = result.scalar_one_or_none()
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}

    for key in DEFAULTS:
        if key in data:
            val = data[key]
            if isinstance(DEFAULTS[key], bool) and not isinstance(val, bool):
                val = str(val).lower() in ("true", "1", "yes")
            stored[key] = val

    stmt = pg_insert(SystemSetting).values(key=SETTINGS_KEY, value=stored)
    stmt = stmt.on_conflict_do_update(index_elements=[SystemSetting.key], set_={"value": stored})
    await db.execute(stmt)

    _apply(stored)
    return get_system_config()
