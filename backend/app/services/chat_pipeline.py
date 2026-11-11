"""聊天管线 — 记忆 + 情绪 + 摘要 + 自动生图

一轮对话的完整处理逻辑，以「事件流」的形式产出，与传输方式无关：
- HTTP 流式接口 (POST /api/v1/conversations/{id}/chat) —— Serverless 部署使用
- WebSocket (/ws/chat/{id}) —— 自托管部署仍可使用

事件格式（与原 WebSocket 协议一致）：
    {"type": "user_saved", "id": ...}                     用户消息已入库
    {"type": "image", "url": ..., "prompt": "", "id": ...} AI 生成的图片
    {"type": "chunk", "content": ...}                     模拟打字的增量文本
    {"type": "done", "full_reply": ..., "emotion_state": ..., "id": ...}
    {"type": "ai_error", "code": ..., "message": ...}     AI 接口报错（Key 无效等）
    {"type": "error", "message": ...}                     其它错误
"""

import asyncio
import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import AsyncIterator, Awaitable, Callable

import httpx
from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.security import decode_token, decrypt_api_key
from app.db.session import AsyncSessionLocal
from app.models.character import Character
from app.models.character_document import CharacterDocument
from app.models.conversation import Conversation
from app.models.memory import LongTermMemory
from app.models.message import Message
from app.models.user import User
from app.services.ai_engine import EnhancedAIEngine
from app.services.memory_service import consolidate_memories

logger = logging.getLogger(__name__)

# 每 N 条消息触发一次记忆合并
MEMORY_CONSOLIDATE_INTERVAL = 20
# 每 N 条消息触发一次对话摘要
SUMMARY_INTERVAL = 30
# 匹配 [IMAGE:xxx] / [image:xxx] / [IMAGE：xxx]（大小写不敏感+中英文冒号）
IMAGE_RE = re.compile(r'\[[Ii][Mm][Aa][Gg][Ee][：:](.*?)\]')
# 匹配 AI 假装发图的文字标记：[发送了...] / [已发送...] / [图片N张] 等
FAKE_SEND_RE = re.compile(r'\[(?:发送了|已发送|图片)[^\]]*\]')
# 用户要求图片的关键词
IMAGE_REQUEST_KW = re.compile(
    r'发.*(?:照片|图片|自拍|图|张)|'
    r'(?:照片|图片|自拍|爆照).*发|'
    r'看看你|发一张|来一张|拍一张|拍个照|拍张'
)

engine = EnhancedAIEngine()

# 后台任务：一个无参的协程工厂。调用方决定是 await 还是 create_task。
BackgroundJob = Callable[[], Awaitable[None]]


@dataclass
class ChatIdentity:
    """发起聊天的用户身份 + 该用户自带的 AI 接口配置"""
    user_id: str
    api_key: str | None = None
    api_base_url: str | None = None
    api_model: str | None = None


async def identify_token(token: str | None) -> ChatIdentity | None:
    """校验 access token 并取出用户的 API 配置。无效返回 None。

    使用独立的短会话，避免在整个流式响应期间占着数据库连接。
    """
    if not token:
        return None
    try:
        payload = decode_token(token)
    except Exception:
        return None
    if payload.get("type") not in (None, "access"):
        return None
    uid = payload.get("sub")
    if not uid:
        return None
    try:
        user_uuid = uuid.UUID(uid)
    except ValueError:
        return None

    from app.db.init import ensure_db_ready
    await ensure_db_ready()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            return None
        return ChatIdentity(
            user_id=str(user.id),
            api_key=decrypt_api_key(user.api_key_encrypted) if user.api_key_encrypted else None,
            api_base_url=user.api_base_url,
            api_model=user.api_model,
        )


# ── 上下文加载 ──


async def _load_context(db, conversation_id: uuid.UUID, user_id: uuid.UUID) -> dict | None:
    """加载对话上下文：角色、历史、记忆、知识库。对话不存在或不属于该用户时返回 None。"""
    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    conv = conv_result.scalar_one_or_none()
    if not conv:
        return None

    char_result = await db.execute(select(Character).where(Character.id == conv.character_id))
    char = char_result.scalar_one_or_none()

    # 历史消息（过滤 [图片] 占位符，防止 AI 模仿）
    msg_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(20)
    )
    history = []
    for m in reversed(msg_result.scalars().all()):
        content = FAKE_SEND_RE.sub("", m.content).strip()
        if not content:
            content = "[图片消息]"
        history.append({"role": m.role, "content": content})

    mem_result = await db.execute(
        select(LongTermMemory)
        .where(
            LongTermMemory.user_id == user_id,
            LongTermMemory.character_id == conv.character_id,
        )
        .order_by(LongTermMemory.importance.desc(), LongTermMemory.created_at.desc())
        .limit(10)
    )
    memories = [
        {"content": m.content, "importance": m.importance, "id": str(m.id)}
        for m in mem_result.scalars().all()
    ]

    doc_result = await db.execute(
        select(CharacterDocument)
        .where(CharacterDocument.character_id == conv.character_id)
        .order_by(CharacterDocument.created_at.desc())
        .limit(5)
    )
    knowledge_docs = [d.content_text for d in doc_result.scalars().all()]

    return {
        "conversation": conv,
        "character": char,
        "history": history,
        "memories": memories,
        "knowledge_docs": knowledge_docs,
    }


# ── 文本处理 ──


def _split_messages(text: str) -> list[str]:
    """将 AI 回复拆成多条消息，用于模拟真人连续发消息"""
    # 优先使用 AI 主动标记的分隔符
    if "[NEXT_MSG]" in text:
        return [p.strip() for p in text.split("[NEXT_MSG]") if p.strip()]

    # 短回复不拆
    if len(text) <= 80:
        return [text]

    # 按句子边界拆分
    sentences = re.split(r'(?<=[。！？~…\.\!\?\n])\s*', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) <= 1:
        return [text]

    # 2-4 句 → 拆成 2 条；更多 → 拆成 2-3 条
    if len(sentences) <= 4:
        mid = len(sentences) // 2
        return [''.join(sentences[:mid]), ''.join(sentences[mid:])]

    n = min(3, len(sentences) - 1)
    size = max(1, len(sentences) // n)
    result = []
    for i in range(0, len(sentences), size):
        group = sentences[i:i + size]
        if group:
            result.append(''.join(group))

    # 如果最后一条太短，合并到前一条
    if len(result) >= 2 and len(result[-1]) < 10:
        result[-2] += result[-1]
        result.pop()

    return result[:3]


def _clean_reply(raw: str) -> str:
    """移除 [IMAGE:xxx]、[发送了...] 伪装标记，以及动作描写括号"""
    text = IMAGE_RE.sub("", raw)
    text = FAKE_SEND_RE.sub("", text).strip()
    # 移除动作描写括号：（脸红）（叹气）*笑* 【开心】等
    text = re.sub(r'[（(][^）)]*?[）)]', '', text)
    text = re.sub(r'\*[^*]+?\*', '', text)
    text = re.sub(r'【[^】]+?】', '', text)
    return text


# ── 生图 ──


async def _call_painting_api(prompt: str) -> dict | None:
    """调用绘画 API，返回 {url, prompt}"""
    if not settings.painting_enabled or not settings.painting_api_key:
        logger.info("[IMAGE GEN] 绘画功能未启用或未配置 API Key，跳过")
        return None
    headers = {
        "Authorization": f"Bearer {settings.painting_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.painting_model,
        "prompt": prompt,
        "n": 1,
        "size": settings.painting_size,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.painting_base_url.rstrip('/')}/images/generations",
                json=payload, headers=headers,
            )
        if resp.status_code == 200:
            data = resp.json()
            return {"url": data["data"][0]["url"], "prompt": prompt}
        logger.error(f"Painting API error {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        logger.error(f"Painting API call failed: {e}")
    return None


async def _generate_image(conversation_id: uuid.UUID, image_prompt: str) -> dict | None:
    """调绘画 API 生图并保存到数据库，返回 {url, prompt, id}"""
    logger.info(f"[IMAGE GEN] conversation={conversation_id}, prompt={image_prompt[:80]}")
    try:
        result = await _call_painting_api(image_prompt)
        if not result:
            return None
        async with AsyncSessionLocal() as db:
            img_msg = Message(
                conversation_id=conversation_id,
                role="assistant",
                content="[图片]",
                content_type="image",
                metadata_={"image_url": result["url"], "prompt": image_prompt},
            )
            db.add(img_msg)
            await db.commit()
            result["id"] = str(img_msg.id)
        return result
    except Exception as e:
        logger.error(f"[IMAGE GEN] Exception: {e}", exc_info=True)
    return None


# ── 后台任务（摘要 / 记忆合并），各自使用独立会话 ──


async def _summarize_conversation(conversation_id: uuid.UUID, ident: ChatIdentity) -> None:
    try:
        async with AsyncSessionLocal() as db:
            msg_result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc())
                .limit(30)
            )
            recent = [
                {"role": m.role, "content": m.content}
                for m in reversed(msg_result.scalars().all())
            ]
            summary = await engine.summarize_conversation(
                recent, api_key=ident.api_key, api_base_url=ident.api_base_url, api_model=ident.api_model,
            )
            if summary:
                conv = (await db.execute(
                    select(Conversation).where(Conversation.id == conversation_id)
                )).scalar_one_or_none()
                if conv:
                    conv.summary = summary
                    await db.commit()
    except Exception:
        logger.exception("Conversation summarization failed")


async def _consolidate(character_id: uuid.UUID, ident: ChatIdentity) -> None:
    try:
        async with AsyncSessionLocal() as db:
            await consolidate_memories(
                str(character_id), ident.user_id, db,
                ident.api_key, ident.api_base_url, ident.api_model,
            )
            await db.commit()
    except Exception:
        logger.exception("Memory consolidation failed")


async def _cancel(*tasks: asyncio.Task | None) -> None:
    for t in tasks:
        if t and not t.done():
            t.cancel()
    for t in tasks:
        if t:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


# ── 主流程 ──


async def run_chat_turn(
    *,
    conversation_id: str,
    ident: ChatIdentity,
    user_message: str,
    image_data: str | None,
    background: list[BackgroundJob],
) -> AsyncIterator[dict]:
    """处理一轮对话，逐个产出事件。

    摘要 / 记忆合并等不影响本轮回复的工作不在这里执行，而是追加到 background，
    由调用方在回复发完之后处理。
    """
    try:
        conv_uuid = uuid.UUID(str(conversation_id))
        user_uuid = uuid.UUID(ident.user_id)
    except ValueError:
        yield {"type": "error", "message": "对话不存在"}
        return

    user_message = user_message or ""
    original_text = user_message  # 保存原始文本，用于 force_image 判断
    logger.info(f"[CHAT] message_len={len(user_message)}, has_image={bool(image_data)}")

    # 有图片时先调视觉模型识图
    display_content = user_message or "[图片]"  # 用户消息气泡显示的内容
    fallback_image = None
    if image_data and settings.vision_enabled:
        try:
            description = await engine.describe_image(image_data)
            user_message = f"[用户发了一张照片：{description}] 用户说：{user_message or '看看这张图'}"
        except Exception:
            logger.exception("Vision description failed, falling back to raw image")
            user_message = user_message or "看看这张图"
            fallback_image = image_data

    async with AsyncSessionLocal() as db:
        # 先加载上下文（历史里不含本条消息，避免在提示词里重复出现）
        ctx = await _load_context(db, conv_uuid, user_uuid)
        if not ctx:
            yield {"type": "error", "message": "对话不存在"}
            return

        conv = ctx["conversation"]
        char = ctx["character"]
        history = ctx["history"]
        memories = ctx["memories"]
        knowledge_docs = ctx["knowledge_docs"]

        # 记录用户消息（只保存用户看到的文本，不保存 AI 识图上下文）
        message = Message(
            conversation_id=conv_uuid,
            role="user",
            content=display_content,
            content_type="image" if image_data else "text",
            metadata_={"image_base64": image_data} if image_data else None,
        )
        db.add(message)
        # 在数据库里原子自增，同一会话并发发消息时计数也不会丢
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conv_uuid)
            .values(message_count=func.coalesce(Conversation.message_count, 0) + 1)
        )

        system_prompt = char.system_prompt if char else "你是一个温柔友好的AI助手。请用中文回复。"
        personality_profile = char.personality_profile if char else None
        character_name = char.name if char else ""
        character_id = conv.character_id
        emotion_state = conv.emotion_state
        summary = conv.summary

        await db.commit()
        user_message_id = str(message.id)

    yield {"type": "user_saved", "id": user_message_id}

    creds = dict(api_key=ident.api_key, api_base_url=ident.api_base_url, api_model=ident.api_model)

    # 情绪检测（并行，不阻塞回复）
    async def _detect_emotion() -> dict | None:
        try:
            return await engine.detect_emotion(user_message, **creds)
        except Exception:
            logger.exception("Emotion detection failed")
            return None

    emotion_task = asyncio.create_task(_detect_emotion())

    # 约 1/3 的概率连发多条消息，模拟真人聊天节奏
    multi = random.random() < 0.35
    # 用原始文本判断，排除识图上下文干扰
    force_image = bool(IMAGE_REQUEST_KW.search(original_text))

    async def _image_from_intent(text_for_intent: str) -> dict | None:
        intent = await engine.detect_image_intent(
            user_message=text_for_intent, conversation_history=history,
            character_name=character_name, personality_profile=personality_profile,
            force_image=True, **creds,
        )
        prompt = intent.get("image_prompt") if intent.get("wants_image") else None
        return await _generate_image(conv_uuid, prompt or text_for_intent)

    # force_image：直接并行调绘图 API，不依赖 AI 的 [IMAGE:xxx] 标记
    image_task: asyncio.Task | None = None
    if force_image:
        image_task = asyncio.create_task(_image_from_intent(original_text))

    raw_full_reply = ""
    try:
        async for chunk in engine.chat_stream(
            system_prompt=system_prompt,
            history=history,
            user_message=user_message,
            image_data=fallback_image,
            personality_profile=personality_profile,
            emotion_state=emotion_state,
            memories=memories,
            summary=summary,
            character_name=character_name,
            knowledge_docs=knowledge_docs,
            multi_message=multi,
            force_image=force_image,
            **creds,
        ):
            raw_full_reply += chunk
    except BaseException:
        await _cancel(emotion_task, image_task)
        raise

    logger.info(f"[CHAT] Stream ended, reply_len={len(raw_full_reply)}")

    # 检测 AI 错误标记
    err_match = re.search(r'\[AI_ERROR:(\w+)\](.*)', raw_full_reply)
    if err_match:
        await _cancel(emotion_task, image_task)
        yield {
            "type": "ai_error",
            "code": err_match.group(1),
            "message": err_match.group(2).strip(),
        }
        return

    # 提取 [IMAGE:xxx] 自发标记（仅非 force_image 时，force_image 已并行生图）
    if not force_image:
        m = IMAGE_RE.search(raw_full_reply)
        if m and m.group(1).strip():
            image_task = asyncio.create_task(_generate_image(conv_uuid, m.group(1).strip()))

    full_reply = _clean_reply(raw_full_reply)

    tasks = [emotion_task] + ([image_task] if image_task else [])
    results = await asyncio.gather(*tasks, return_exceptions=True)

    new_emotion = results[0] if isinstance(results[0], dict) else None
    image_result = None
    if image_task and not isinstance(results[1], BaseException):
        image_result = results[1]

    # 多消息模式下拆分回复
    msg_parts = _split_messages(full_reply) if multi else [full_reply]
    # 过滤掉 [图片] 占位符和空字符串，避免空文字气泡
    text_parts = [p for p in msg_parts if p.strip() and p.strip() != "[图片]"]

    # 保存 AI 回复 + 更新会话状态
    saved_ids: list[str] = []
    async with AsyncSessionLocal() as db:
        saved = []
        for i, part in enumerate(text_parts):
            ai_msg = Message(
                conversation_id=conv_uuid,
                role="assistant",
                content=part,
                content_type="text",
            )
            if i > 0:
                # 同一事务里 now() 相同；连发的多条消息错开 1ms，重新加载后顺序才稳定
                ai_msg.created_at = func.now() + timedelta(milliseconds=i)
            db.add(ai_msg)
            saved.append(ai_msg)

        values: dict = {"message_count": func.coalesce(Conversation.message_count, 0) + 1}
        if new_emotion:
            values["emotion_state"] = new_emotion
        row = (await db.execute(
            update(Conversation)
            .where(Conversation.id == conv_uuid)
            .values(**values)
            .returning(Conversation.message_count, Conversation.emotion_state)
        )).first()
        if row:
            message_count, emotion_state = row
            if message_count % SUMMARY_INTERVAL == 0:
                background.append(lambda: _summarize_conversation(conv_uuid, ident))
            if message_count % MEMORY_CONSOLIDATE_INTERVAL == 0:
                background.append(lambda: _consolidate(character_id, ident))

        await db.commit()
        saved_ids = [str(m.id) for m in saved]

    logger.info(f"[SEND] image={bool(image_result)} text_parts={len(text_parts)}")

    # 发送图片事件（独立气泡，先于文字）
    if image_result:
        yield {"type": "image", "url": image_result["url"], "prompt": "", "id": image_result.get("id")}

    if not text_parts:
        if image_result is None:
            # 没有实际文字内容，也没有图片
            yield {
                "type": "ai_error",
                "code": "image_failed" if (force_image or IMAGE_RE.search(raw_full_reply)) else "empty_reply",
                "message": "呜…图片生成失败了，请稍后再试吧 (′；ω；`)"
                if (force_image or IMAGE_RE.search(raw_full_reply))
                else "呜…刚才没想好怎么回复，能再说一遍吗？",
            }
        return

    # 发送文字气泡
    for i, part in enumerate(text_parts):
        if i > 0:
            # 第二条开始：停顿一下再「打字」
            await asyncio.sleep(1.8 + random.uniform(0, 2.5))
            for j in range(0, len(part), 2):
                yield {"type": "chunk", "content": part[j:j + 2]}
                await asyncio.sleep(0.025)
        yield {
            "type": "done",
            "full_reply": part,
            "emotion_state": emotion_state,
            "id": saved_ids[i],
        }
