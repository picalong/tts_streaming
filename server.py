#!/usr/bin/env python3
"""
Edge-TTS Streaming Server — Queue, SSE MP3 streaming
Run: python server.py
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_CONCURRENT = int(os.getenv("TTS_MAX_CONCURRENT", "8"))
MAX_RAM_GB = float(os.getenv("TTS_MAX_RAM_GB", "8.0"))
PORT = int(os.getenv("PORT", "3002"))

DEFAULT_VOICE = "vi-VN-HoaiNeural"
DEFAULT_RATE = "+0%"

active_count = 0
response_queues: dict = {}
resp_lock = threading.Lock()


def split_into_sentences(text: str) -> list:
    sentences = re.findall(r'[^.!?]*[.!?]+|[^.!?]+$', text.strip())
    return [s.strip() for s in sentences if s.strip()]


async def run_edge_tts(text: str, voice: str, rate: str) -> Optional[bytes]:
    cmd = [
        sys.executable, "-m", "edge_tts",
        "--voice", voice,
        "--rate", rate,
        "--text", text,
        "--write-media", "pipe:1",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        data, _ = await process.communicate()
        return data if data else None
    except Exception:
        return None


async def generate_audio(req_id: str, text: str, voice: str, rate: str):
    global active_count

    sentences = split_into_sentences(text)

    with resp_lock:
        active_count += 1

    try:
        for sentence in sentences:
            mp3_data = await run_edge_tts(sentence, voice, rate)
            if mp3_data:
                with resp_lock:
                    if req_id in response_queues:
                        await response_queues[req_id].put(mp3_data)
    except Exception:
        pass

    with resp_lock:
        active_count -= 1
        if req_id in response_queues:
            await response_queues[req_id].put(None)


def sse_pack(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()


class TTSRequestBody(BaseModel):
    text: str = ""
    voice: Optional[str] = None
    rate: str = DEFAULT_RATE


@app.get("/")
async def root():
    return JSONResponse({"ok": True, "server": "edge-tts-streaming"})


@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


@app.get("/status")
async def status():
    import psutil

    vm = psutil.virtual_memory()
    ram_used_gb = vm.used / (1024 ** 3)
    ram_total_gb = vm.total / (1024 ** 3)

    with resp_lock:
        slots_used = active_count
        slots_free = max(0, MAX_CONCURRENT - slots_used)

    return JSONResponse({
        "ram_used_gb": round(ram_used_gb, 2),
        "ram_total_gb": round(ram_total_gb, 2),
        "ram_limit_gb": MAX_RAM_GB,
        "slots_used": slots_used,
        "slots_free": slots_free,
        "slots_total": MAX_CONCURRENT,
        "overloaded": ram_used_gb > MAX_RAM_GB or slots_free == 0,
    })


@app.post("/tts/stream")
async def tts_stream(body: TTSRequestBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    voice = DEFAULT_VOICE
    rate = body.rate or DEFAULT_RATE

    req_id = hashlib.sha256(f"{text}_{voice}_{rate}_{time.time()}".encode()).hexdigest()[:12]

    resp_q: asyncio.Queue = asyncio.Queue()
    with resp_lock:
        response_queues[req_id] = resp_q

    asyncio.create_task(generate_audio(req_id, text, voice, rate))

    async def event_generator() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(resp_q.get(), timeout=60)
                except asyncio.TimeoutError:
                    yield sse_pack("error", json.dumps({"error": "timeout"}))
                    break

                if chunk is None:
                    yield sse_pack("done", "[DONE]")
                    break

                yield sse_pack("audio", json.dumps({"audio": chunk.hex()}))
        finally:
            with resp_lock:
                response_queues.pop(req_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


if __name__ == "__main__":
    import uvicorn
    print(f"Edge-TTS server running on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)