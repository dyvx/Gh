"""小文件存储服务 — 把上传的头像存进数据库（Serverless 没有可写磁盘）"""

import secrets

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stored_file import StoredFile

FILES_URL_PREFIX = "/api/v1/files/"

# Vercel 单次请求体上限 4.5MB，留出 multipart 开销
AVATAR_MAX_SIZE = 4 * 1024 * 1024
AVATAR_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


async def save_file(db: AsyncSession, *, prefix: str, content: bytes, content_type: str,
                    replace_url: str | None = None) -> str:
    """保存文件并返回可公开访问的 URL。

    每次保存都生成新的 key（URL 随之变化），这样浏览器可以永久缓存，
    换头像后也不会看到旧图。replace_url 指向的旧文件会被删除。
    """
    await delete_file_by_url(db, replace_url)
    key = f"{prefix}-{secrets.token_hex(8)}"
    db.add(StoredFile(key=key, content_type=content_type, size=len(content), data=content))
    await db.flush()
    return f"{FILES_URL_PREFIX}{key}"


async def delete_file_by_url(db: AsyncSession, url: str | None) -> None:
    if not url or not url.startswith(FILES_URL_PREFIX):
        return
    key = url[len(FILES_URL_PREFIX):].split("?", 1)[0]
    await db.execute(delete(StoredFile).where(StoredFile.key == key))
