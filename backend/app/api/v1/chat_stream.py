"""HTTP 流式聊天 — WebSocket 的 Serverless 友好替代

POST /api/v1/conversations/{conversation_id}/chat
请求体: {"message": "...", "image": "data:image/...;base64,..." | null}
响应:   application/x-ndjson，每行一个 JSON 事件（格式见 chat_pipeline）

为什么不用 WebSocket：Serverless 函数按请求计费、实例随时回收，
一个长期挂着的连接既不可靠也不划算。一问一答式的流式 HTTP 请求更合适。
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.core.dependencies import security_scheme
from app.services.chat_pipeline import BackgroundJob, identify_token, run_chat_turn

logger = logging.getLogger(__name__)

router = APIRouter()

# 等待 AI 回复期间定期发心跳，防止中间代理因长时间无数据而断开连接
HEARTBEAT_SECONDS = 10

# 保留对运行中任务的强引用，防止被垃圾回收
_running: set[asyncio.Task] = set()

_END = object()


class ChatStreamRequest(BaseModel):
    message: str = ""
    image: str | None = None


@router.post("/{conversation_id}/chat")
async def chat_stream(
    conversation_id: uuid.UUID,
    data: ChatStreamRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
):
    """发送一条消息，以 NDJSON 事件流返回 AI 的回复"""
    ident = await identify_token(credentials.credentials if credentials else None)
    if not ident:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if not data.message.strip() and not data.image:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="消息不能为空")

    queue: asyncio.Queue = asyncio.Queue()

    async def worker():
        """独立于响应运行：即使用户中途离开页面，回复也会生成完并入库。"""
        background: list[BackgroundJob] = []
        try:
            async for event in run_chat_turn(
                conversation_id=str(conversation_id),
                ident=ident,
                user_message=data.message,
                image_data=data.image,
                background=background,
            ):
                await queue.put(event)
        except Exception:
            logger.exception("Chat turn failed")
            await queue.put({"type": "error", "message": "服务暂时不可用，请稍后再试"})

        # 摘要 / 记忆合并：回复已经全部发出，但要在响应结束前做完——
        # Serverless 实例在响应结束后可能立刻被冻结，「响应之后」的工作不保证能执行。
        for job in background:
            try:
                await job()
            except Exception:
                logger.exception("Background job failed")
        await queue.put(_END)

    task = asyncio.create_task(worker())
    _running.add(task)
    task.add_done_callback(_running.discard)

    async def body():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield b'{"type":"ping"}\n'
                continue
            if event is _END:
                break
            yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(
        body(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
