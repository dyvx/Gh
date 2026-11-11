"""WebSocket 流式聊天（自托管部署使用）

实际的对话处理在 app.services.chat_pipeline 里，这里只负责 WebSocket 收发。
Serverless 部署（Vercel）请使用 HTTP 流式接口：POST /api/v1/conversations/{id}/chat
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.chat_pipeline import BackgroundJob, ChatIdentity, identify_token, run_chat_turn

logger = logging.getLogger(__name__)

router = APIRouter()

# 保留对后台任务的强引用，防止被垃圾回收
_background_tasks: set[asyncio.Task] = set()


def _spawn(job: BackgroundJob) -> None:
    task = asyncio.create_task(job())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.websocket("/{conversation_id}")
async def chat_websocket(websocket: WebSocket, conversation_id: str):
    await websocket.accept()
    ident: ChatIdentity | None = None

    try:
        # 首条消息必须是 {"type": "auth", "token": "..."}
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        msg = json.loads(raw)
        if msg.get("type") == "auth":
            ident = await identify_token(msg.get("token"))
        if not ident:
            await websocket.send_json({"type": "error", "message": "请先登录"})
            await websocket.close(code=4401)
            return

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("type") != "chat":
                continue

            background: list[BackgroundJob] = []
            async for event in run_chat_turn(
                conversation_id=conversation_id,
                ident=ident,
                user_message=msg.get("message", ""),
                image_data=msg.get("image"),
                background=background,
            ):
                await websocket.send_json(event)

            # 长驻进程里摘要 / 记忆合并可以放到后台，不阻塞下一条消息
            for job in background:
                _spawn(job)

    except asyncio.TimeoutError:
        pass  # 超时静默关闭
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket chat error")
        try:
            await websocket.send_json({"type": "error", "message": "服务暂时不可用，请稍后再试"})
        except Exception:
            pass
