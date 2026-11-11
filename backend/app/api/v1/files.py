"""文件读取 API — 输出存在数据库里的头像等小文件"""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.stored_file import StoredFile

router = APIRouter()


@router.get("/{key}")
async def get_file(key: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StoredFile).where(StoredFile.key == key))
    stored = result.scalar_one_or_none()
    if not stored:
        raise HTTPException(status_code=404, detail="文件不存在")
    return Response(
        content=stored.data,
        media_type=stored.content_type,
        headers={
            # key 每次上传都会变，内容不可变，可以放心长期缓存
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
