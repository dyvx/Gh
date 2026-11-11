"""小文件存储（头像等）

Serverless 平台的文件系统是只读的，上传的文件不能写到磁盘，
所以直接存进数据库，由 /api/v1/files/{key} 读出。
"""

from sqlalchemy import Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class StoredFile(Base, TimestampMixin):
    __tablename__ = "stored_files"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
