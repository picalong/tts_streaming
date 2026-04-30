#!/usr/bin/env python3
import asyncio
import base64
import hashlib
import json
import os
import threading
import time
from typing import AsyncGenerator
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
PORT = int(os.getenv("PORT", "3002"))
DEFAULT_VOICE = "vi-VN-HoaiMyNeural"
DEFAULT_RATE = "+0%"

active_count = 0
response_queues: dict = {}
resp_lock = threading.Lock()


def sse_pack(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()


class TTSRequestBody(BaseModel):
    text: str = ""
    voice: Optional[str] = None
    rate: str = DEFAULT_RATE


async def generate_audio(req_id: str, text: str, voice: str, rate: str):
    global active_count
    with resp_lock:
        active_count += 1
    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        audio_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data += chunk["data"]
        b64 = base64.b64encode(audio_data).decode()
        with resp_lock:
            if req_id in response_queues:
                await response_queues[req_id].put(("audio", b64))
    except Exception as e:
        print(f"generate_audio error: {e}", flush=True)
    finally:
        with resp_lock:
            active_count -= 1
            if req_id in response_queues:
                await response_queues[req_id].put(None)


@app.get("/")
async def root():
    return JSONResponse({"ok": True})


@app.get("/status")
async def status():
    import psutil
    vm = psutil.virtual_memory()
    with resp_lock:
        slots_used = active_count
        slots_free = max(0, MAX_CONCURRENT - slots_used)
    return JSONResponse({
        "ram_used_gb": round(vm.used / (1024**3), 2),
        "ram_total_gb": round(vm.total / (1024**3), 2),
        "slots_used": slots_used,
        "slots_free": slots_free,
        "slots_total": MAX_CONCURRENT,
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
                    break
                if chunk is None:
                    yield sse_pack("done", "[DONE]")
                    break
                event_type, data = chunk
                if event_type == "audio":
                    yield sse_pack("audio", json.dumps({"audio": data}))
        finally:
            with resp_lock:
                response_queues.pop(req_id, None)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


TEST_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>TTS Test</title><style>body{font-family:sans-serif;max-width:600px;margin:40px auto;padding:20px;}textarea{width:100%;font-size:16px;}button{padding:8px 20px;font-size:16px;}#status{color:#888;margin-top:10px;}</style></head>
<body><h2>TTS Test</h2><textarea id="text" rows="4">Xin chào, đây là thử nghiệm giọng đọc tiếng Việt.</textarea><br><br>
<select id="rate"><option value="-25%">0.75x</option><option value="+0%" selected>1.0x</option><option value="+25%">1.25x</option><option value="+50%">1.5x</option></select>
<button onclick="speak()">▶ Đọc</button><p id="status"></p>
<script>
async function speak(){
    const btn=document.querySelector("button"),status=document.getElementById("status");
    btn.disabled=true;status.textContent="Đang tạo âm thanh...";
    let audioB64=null;
    const res=await fetch("/tts/stream",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:document.getElementById("text").value,rate:document.getElementById("rate").value})});
    const reader=res.body.getReader(),decoder=new TextDecoder();let buf="";
    while(true){
        const{done,value}=await reader.read();if(done)break;
        buf+=decoder.decode(value,{stream:true});const lines=buf.split("\\n");buf=lines.pop();
        for(const line of lines){
            if(!line.startsWith("data: "))continue;
            const payload=line.slice(6).trim();
            if(payload==="[DONE]"){if(audioB64){const binaryString=atob(audioB64);const bytes=new Uint8Array(binaryString.length);for(let i=0;i<binaryString.length;i++)bytes[i]=binaryString.charCodeAt(i);const audio=new Audio(URL.createObjectURL(new Blob([bytes],{type:"audio/mpeg"})));audio.play();status.textContent="Đang phát...";audio.onended=()=>{status.textContent="Xong!";btn.disabled=false;};}else{status.textContent="Không có audio!";btn.disabled=false;}return;}
            const{audio}=JSON.parse(payload);if(audio)audioB64=audio;
        }
    }
}
</script></body></html>"""


@app.get("/test")
async def test_page():
    from starlette.responses import HTMLResponse
    return HTMLResponse(TEST_HTML)


if __name__ == "__main__":
    import uvicorn
    print(f"Server running on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
