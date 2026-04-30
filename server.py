#!/usr/bin/env python3
"""
Edge-TTS Streaming Server — Queue, SSE MP3 streaming
Run: python server.py
"""
import asyncio
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import EventStreamResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI()

# Config
MAX_CONCURRENT = int(os.getenv("TTS_MAX_CONCURRENT", "8"))
MAX_RAM_GB = float(os.getenv("TTS_MAX_RAM_GB", "8.0"))
PORT = int(os.getenv("PORT", "3002"))

# Default voice
DEFAULT_VOICE = "vi-VN-HoaiNeural"
DEFAULT_RATE = "+0%"

# Queue system
queue_lock = threading.Lock()
request_queue: deque = deque()
active_count = 0
worker_busy = False
worker_task: Optional[asyncio.Task] = None


@dataclass
class TTSRequest:
    id: str
    text: str
    voice: str
    rate: str
    priority: int  # lower = higher priority (shorter text = higher priority)


def get_queue_status():
    """Return queue status info."""
    with queue_lock:
        slots_free = max(0, MAX_CONCURRENT - active_count)
        return {
            "queue_length": len(request_queue),
            "slots_used": active_count,
            "slots_free": slots_free,
            "slots_total": MAX_CONCURRENT,
            "worker_busy": worker_busy,
        }


def enqueue_request(req: TTSRequest) -> int:
    """Add request to queue. Returns queue position."""
    with queue_lock:
        # Insert by priority (shorter text first)
        pos = 0
        for i, r in enumerate(request_queue):
            if req.priority < r.priority:
                pos = i
                break
            pos = i + 1
        request_queue.insert(pos, req)
        return pos


def dequeue_request() -> Optional[TTSRequest]:
    """Get next request from queue."""
    with queue_lock:
        if request_queue:
            return request_queue.popleft()
        return None


def split_into_sentences(text: str) -> list:
    """Split text into sentences, preserving the delimiter."""
    # Split by sentence-ending punctuation followed by space or end
    sentences = re.findall(r'[^.!?]*[.!?]+|[^.!?]+$', text.strip())
    return [s.strip() for s in sentences if s.strip()]


def pcm_to_mp3(pcm_data: bytes, sample_rate: int = 24000) -> Optional[bytes]:
    """Convert PCM to MP3 using ffmpeg."""
    try:
        process = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-f", "s16le",
                "-ar", str(sample_rate),
                "-ac", "1",
                "-i", "pipe:0",
                "-codec:a", "libmp3lame",
                "-abr", "1",
                "-q:a", "5",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        mp3_data, _ = process.communicate(input=pcm_data)
        return mp3_data if mp3_data else None
    except Exception:
        return None


async def run_edge_tts(text: str, voice: str, rate: str) -> Optional[bytes]:
    """Run edge-tts and return MP3 data."""
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


async def process_queue():
    """Process requests from queue one by one."""
    global worker_busy, active_count

    while True:
        req = dequeue_request()
        if req is None:
            worker_busy = False
            # Wait a bit before checking again
            await asyncio.sleep(0.5)
            continue

        worker_busy = True
        with queue_lock:
            active_count += 1

        try:
            sentences = split_into_sentences(req.text)
            for sentence in sentences:
                mp3_data = await run_edge_tts(sentence, req.voice, req.rate)
                if mp3_data:
                    yield mp3_data
        except Exception:
            pass

        with queue_lock:
            active_count -= 1


async def worker_loop(queue_out: asyncio.Queue):
    """Main worker loop that processes queue and puts MP3 chunks into queue_out."""
    global worker_task

    while True:
        req = dequeue_request()
        if req is None:
            await asyncio.sleep(0.1)
            continue

        global active_count
        with queue_lock:
            active_count += 1

        try:
            sentences = split_into_sentences(req.text)
            for sentence in sentences:
                mp3_data = await run_edge_tts(sentence, req.voice, req.rate)
                if mp3_data:
                    await queue_out.put(mp3_data)
        except Exception:
            pass

        with queue_lock:
            active_count -= 1


# In-memory response tracking
response_queues: dict = {}
resp_lock = threading.Lock()


async def generate_audio(req_id: str, text: str, voice: str, rate: str):
    """Generate audio chunks and add to response queue."""
    sentences = split_into_sentences(text)
    with queue_lock:
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

    with queue_lock:
        active_count -= 1

    # Signal done
    with resp_lock:
        if req_id in response_queues:
            await response_queues[req_id].put(None)


class TTSRequestBody(BaseModel):
    text: str = ""
    voice: Optional[str] = None  # always uses vi-VN- HoaiNeural
    rate: str = DEFAULT_RATETE


@app.get("/")
async def root():
    return JSONResponse({"ok": True, "server": "edge-tts-streaming"})


@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


@app.get("/status")
async def status():
    import psutil
    global active_count

    vm = psutil.virtual_memory()
    ram_used_gb = vm.used / (1024 ** 3)
    ram_total_gb = vm.total / (1024 ** 3)

    with queue_lock:
        slots_used = active_count
        slots_free = max(0, MAX_CONCURRENT - slots_used)
        queue_len = len(request_queue)

    return JSONResponse({
        "ram_used_gb": round(ram_used_gb, 2),
        "ram_total_gb": round(ram_total_gb, 2),
        "ram_limit_gb": MAX_RAM_GB,
        "slots_used": slots_used,
        "slots_free": slots_free,
        "slots_total": MAX_CONCURRENT,
        "queue_length": queue_len,
        "worker_busy": worker_busy,
        "overloaded": ram_used_gb > MAX_RAM_GB or slots_free == 0,
    })


@app.post("/tts/stream")
async def tts_stream(body: TTSRequestBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    voice = DEFAULT_VOICE
    rate = body.rate or DEFAULT_RATE

    # Generate request ID
    req_id = hashlib.sha256(f"{text}_{voice}_{rate}_{time.time()}".encode()).hexdigest()[:12]

    # Create response queue for this request
    resp_q: asyncio.Queue = asyncio.Queue()
    with resp_lock:
        response_queues[req_id] = resp_q

    # Start generation in background
    asyncio.create_task(generate_audio(req_id, text, voice, rate))

    # Stream response via SSE
    async def event_generator():
        with queue_lock:
            active_count += 1

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(resp_q.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"event": "error", "data": json.dumps({"error": "timeout"})}
                    break

                if chunk is None:
                    yield {"event": "done", "data": "[DONE]"}
                    break

                yield {
                    "event": "audio",
                    "data": json.dumps({"audio": chunk.hex()})
                }
        finally:
            with queue_lock:
                active_count -= 1
            with resp_lock:
                response_queues.pop(req_id, None)

    return EventStreamResponse(content=event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    print(f"Edge-TTS server running on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)