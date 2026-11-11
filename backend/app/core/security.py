"""JWT 令牌 + 密码哈希 + API Key 加解密"""

import base64
from datetime import datetime, timedelta, timezone

import bcrypt
from cryptography.fernet import Fernet
from jose import JWTError, jwt

from app.core.config import settings

# 从 secret_key 派生 Fernet key（Fernet 需要 32 字节 base64 key）
_fernet_key = base64.urlsafe_b64encode(settings.secret_key.encode().ljust(32, b'\0')[:32])
_fernet = Fernet(_fernet_key)


def encrypt_api_key(plain: str) -> str:
    """加密 API key"""
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_api_key(encrypted: str) -> str | None:
    """解密 API key"""
    if not encrypted:
        return None
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except Exception:
        return None


def _password_bytes(password: str) -> bytes:
    # bcrypt 只使用前 72 字节；新版 bcrypt 对超长输入会直接报错，这里显式截断
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    """bcrypt 哈希（输出格式与之前 passlib 生成的 $2b$ 哈希兼容）"""
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: str, membership_tier: str = "free") -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": user_id,
        "tier": membership_tier,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """解码 JWT，出错时抛出 JWTError"""
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
