"""HTTP 反向代理：/proxy/{job_id}/xxx -> http://127.0.0.1:{port}/xxx"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocket

from .manager import manager

router = APIRouter()

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


@router.api_route("/proxy/{job_id}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@router.api_route("/proxy/{job_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(job_id: str, request: Request, path: str = ""):
    job = manager.get(job_id)
    if not job.port:
        return Response("任务未运行", status_code=502)
    qs = str(request.url.query)
    target = f"http://127.0.0.1:{job.port}/{path}"
    if qs:
        target = f"{target}?{qs}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.request(request.method, target, headers=headers, content=body)
    except httpx.HTTPError as e:
        return Response(f"上游访问失败: {e}", status_code=502)

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=resp_headers)


@router.websocket("/proxy/{job_id}/ws/{path:path}")
async def proxy_ws(websocket: WebSocket, job_id: str, path: str = "") -> None:
    """尽力而为的 WebSocket 转发（仅文本/二进制帧，无 ping 续传）。"""
    await websocket.accept()
    job = manager.get(job_id)
    if not job.port:
        await websocket.close(code=1011)
        return
    try:
        import websockets  # type: ignore
    except ImportError:
        await websocket.send_text("服务端未安装 websockets，无法代理 WS")
        await websocket.close()
        return

    qs = str(websocket.url.query)
    target = f"ws://127.0.0.1:{job.port}/{path}" + (f"?{qs}" if qs else "")
    try:
        async with websockets.connect(target) as up:
            async def uplink():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if "text" in msg and msg["text"] is not None:
                            await up.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await up.send(msg["bytes"])
                except Exception:
                    pass

            async def downlink():
                try:
                    async for msg in up:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception:
                    pass

            import asyncio
            await asyncio.gather(uplink(), downlink())
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
