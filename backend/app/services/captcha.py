"""SVG 图形验证码 — 无状态签名 + 数据库防重放

原实现把验证码答案存在 Redis 里。Serverless 部署不想为此多挂一个 Redis，
所以改成：答案的 HMAC 签名随 captcha_id 下发给前端（前端看不到答案），
校验时重新计算签名比对；用过的 captcha_id 记入数据库，保证一次性使用。
"""

import base64
import hashlib
import hmac
import random
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.system_setting import UsedCaptcha

TTL = 300  # 5 分钟


def _sign(nonce: str, expires: int, code: str) -> str:
    msg = f"captcha:{nonce}:{expires}:{code.strip().upper()}".encode()
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()


async def generate_captcha() -> dict:
    """生成 SVG 验证码，返回 {captcha_id, captcha_image}"""
    # 4 位随机字母数字（排除易混淆字符 0O1Il）
    chars = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    code = "".join(secrets.choice(chars) for _ in range(4))

    svg = _render_svg(code)

    nonce = secrets.token_hex(8)
    expires = int(time.time()) + TTL
    captcha_id = f"{nonce}.{expires}.{_sign(nonce, expires, code)}"

    svg_b64 = base64.b64encode(svg.encode()).decode()
    return {
        "captcha_id": captcha_id,
        "captcha_image": f"data:image/svg+xml;base64,{svg_b64}",
    }


async def verify_captcha(captcha_id: str, captcha_code: str, db: AsyncSession) -> bool:
    """校验验证码，无论对错都作废（一次性使用）"""
    if not captcha_id or not captcha_code:
        return False

    parts = captcha_id.split(".")
    if len(parts) != 3:
        return False
    nonce, expires_raw, signature = parts
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    if expires < time.time() or len(nonce) > 64:
        return False

    now = datetime.now(timezone.utc)
    # 顺手清理过期记录，表不会无限增长
    await db.execute(delete(UsedCaptcha).where(UsedCaptcha.expires_at < now))
    # 先占用再比对：同一个 captcha_id 只有第一次请求能插入成功
    claimed = await db.execute(
        pg_insert(UsedCaptcha)
        .values(nonce=nonce, expires_at=now + timedelta(seconds=TTL + 60))
        .on_conflict_do_nothing(index_elements=[UsedCaptcha.nonce])
        .returning(UsedCaptcha.nonce)
    )
    first_use = claimed.scalar_one_or_none() is not None
    # 立刻提交，保证即使后续登录失败抛异常回滚，这个验证码也已作废
    await db.commit()
    if not first_use:
        return False

    return hmac.compare_digest(signature, _sign(nonce, expires, captcha_code))


def _render_svg(code: str) -> str:
    """生成带干扰的 SVG"""
    w, h = 120, 44
    colors = ["#e87d8a", "#c44d6a", "#d4687c", "#b84560", "#f598a5", "#9b3d54"]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" fill="#fef5f7" rx="6"/>',
    ]

    # 干扰线（3 条）
    for _ in range(3):
        x1, y1 = random.randint(0, w // 3), random.randint(4, h - 4)
        x2, y2 = random.randint(w * 2 // 3, w), random.randint(4, h - 4)
        stroke = random.choice(colors)
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="1" opacity="0.3"/>'
        )

    # 噪点（15 个）
    for _ in range(15):
        cx, cy = random.randint(4, w - 4), random.randint(4, h - 4)
        fill = random.choice(colors)
        r = random.uniform(0.5, 1.5)
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" opacity="0.3"/>')

    # 字符（旋转 + 偏移）
    char_w = w // len(code)
    for i, ch in enumerate(code):
        x = char_w * i + random.randint(6, char_w - 20)
        y = random.randint(28, 36)
        angle = random.randint(-30, 30)
        font_size = random.randint(22, 26)
        fill = random.choice(colors)
        parts.append(
            f'<text x="{x}" y="{y}" font-size="{font_size}" fill="{fill}" '
            f'font-family="Arial,Helvetica,sans-serif" font-weight="bold" '
            f'transform="rotate({angle},{x},{y})">{ch}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)
