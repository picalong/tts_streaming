#!/usr/bin/env node
/**
 * Piper TTS Streaming Server — SSE, 2 voices, variable speed
 * Run: node piper_server.js
 */
const os = require("os");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const express = require("express");

const app = express();
app.use(express.json({ limit: "50mb" }));

const MODEL_DIR = path.join(__dirname, "models/vi");
const VOICE_NAMES = [
  "ngochuyennew", "ngocngan3701"
];
const VOICES = {};

const MAX_CONCURRENT = parseInt(process.env.TTS_MAX_CONCURRENT || "2", 10);
const MAX_RAM_GB = parseFloat(process.env.TTS_MAX_RAM_GB || "8.0");

let activeCount = 0;

function checkResources() {
  if (activeCount >= MAX_CONCURRENT) {
    return { ok: false, message: `Server đang xử lý ${MAX_CONCURRENT} yêu cầu cùng lúc. Vui lòng thử lại sau ít phút.` };
  }
  return { ok: true, message: null };
}

function loadVoices() {
  for (const name of VOICE_NAMES) {
    const onnxPath = path.join(MODEL_DIR, `${name}.onnx`);
    if (fs.existsSync(onnxPath)) {
      VOICES[name] = onnxPath;
      console.log(`  ${name} OK`);
    } else {
      console.warn(`  ${name} NOT FOUND at ${onnxPath}`);
    }
  }
}

function buildPiperArgs(voicePath, text, speed) {
  return [
    "--model", voicePath,
    "--output-raw",
    "--length-scale", (1.0 / Math.max(speed, 0.1)).toFixed(4),
  ];
}

function rawToWav(rawBuffer, sampleRate = 22050, sampleWidth = 2, channels = 1) {
  const dataLength = rawBuffer.length;
  const buffer = Buffer.alloc(44 + dataLength);

  buffer.write("RIFF", 0);
  buffer.writeUInt32LE(36 + dataLength, 4);
  buffer.write("WAVE", 8);
  buffer.write("fmt ", 12);
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(channels, 22);
  buffer.writeUInt32LE(sampleRate, 24);
  buffer.writeUInt32LE(sampleRate * channels * sampleWidth, 28);
  buffer.writeUInt16LE(channels * sampleWidth, 32);
  buffer.writeUInt16LE(sampleWidth * 8, 34);
  buffer.write("data", 36);
  buffer.writeUInt32LE(dataLength, 40);

  rawBuffer.copy(buffer, 44);
  return buffer;
}

app.use((req, res, next) => {
  res.header("Access-Control-Allow-Origin", "*");
  res.header("Access-Control-Allow-Headers", "Content-Type");
  res.header("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  next();
});

app.post("/tts/stream", (req, res) => {
  let { text = "", voice = "ngochuyennew", speed = 1.0 } = req.body || {};
  text = text.trim();

  if (!text) {
    return res.status(400).json({ error: "text required" });
  }

  const resourceCheck = checkResources();
  if (!resourceCheck.ok) {
    return res.status(503).json({ error: "overload", message: resourceCheck.message });
  }

  activeCount++;

  const voicePath = VOICES[voice] || VOICES["ngochuyennew"];
  if (!voicePath) {
    activeCount--;
    return res.status(500).json({ error: "voice not found" });
  }

  const sentences = text.match(/[^.?!]+[.?!]+|[^.?!]+$/g) || [text];
  let sentIndex = 0;
  let rawBuffer = Buffer.alloc(0);
  let sampleRate = 22050;
  let sampleWidth = 2;
  let channels = 1;

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("X-Accel-Buffering", "no");
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();

  function synthesizeNext() {
    if (sentIndex >= sentences.length) {
      if (rawBuffer.length > 0) {
        const wavChunk = rawToWav(rawBuffer, sampleRate, sampleWidth, channels);
        res.write(`data: ${JSON.stringify({ audio: wavChunk.toString("base64") })}\n\n`);
      }
      activeCount--;
      res.write("data: [DONE]\n\n");
      res.end();
      return;
    }

    const sentence = sentences[sentIndex].trim();
    if (!sentence) {
      sentIndex++;
      synthesizeNext();
      return;
    }

    const PIPER_PATH = "/var/www/tts_streaming/piper/piper";

    const piper = spawn(PIPER_PATH, [
      "--model", voicePath,
      "--output-raw",
      "--length-scale", (1.0 / Math.max(speed, 0.1)).toFixed(4),
    ]);

    piper.stderr.on("data", (chunk) => {
      const msg = chunk.toString();
      const rateMatch = msg.match(/sample_rate["\s:]+(\d+)/);
      if (rateMatch) sampleRate = parseInt(rateMatch[1], 10);
    });

    let sentBuffer = Buffer.alloc(0);
    piper.stdout.on("data", (chunk) => {
      sentBuffer = Buffer.concat([sentBuffer, chunk]);
    });

    piper.on("close", () => {
      if (sentBuffer.length > 0) {
        const wavChunk = rawToWav(sentBuffer, sampleRate, sampleWidth, channels);
        res.write(`data: ${JSON.stringify({ audio: wavChunk.toString("base64") })}\n\n`);
      }
      sentIndex++;
      synthesizeNext();
    });

    piper.on("error", (err) => {
      res.write(`data: ${JSON.stringify({ error: err.message })}\n\n`);
      sentIndex++;
      synthesizeNext();
    });

    piper.stdin.write(sentence);
    piper.stdin.end();
  }

  synthesizeNext();
});

app.post("/tts/wav", (req, res) => {
  const { text = "Xin chào", voice = "ngochuyennew" } = req.body || {};
  const voicePath = VOICES[voice] || VOICES["ngochuyennew"];

  if (!voicePath) {
    return res.status(500).json({ error: "voice not found" });
  }

  const piper = spawn("piper", [
    "--model", voicePath,
    "--output-raw",
  ]);

  const rawChunks = [];
  piper.stdout.on("data", (chunk) => rawChunks.push(chunk));

  piper.on("close", () => {
    const rawBuffer = Buffer.concat(rawChunks);
    const wavBuffer = rawToWav(rawBuffer);
    res.setHeader("Content-Type", "audio/wav");
    res.send(wavBuffer);
  });

  piper.stdin.write(text.trim());
  piper.stdin.end();
});

app.get("/", (req, res) => {
  res.json({ ok: true, voices: Object.keys(VOICES) });
});

app.get("/health", (req, res) => {
  res.json({ ok: true, voices: Object.keys(VOICES) });
});

app.get("/status", (req, res) => {
  const totalMemGB = os.totalmem() / (1024 ** 3);
  const freeMemGB = os.freemem() / (1024 ** 3);
  const usedMemGB = totalMemGB - freeMemGB;
  const cpus = os.cpus();
  let idle = 0, total = 0;
  for (const cpu of cpus) {
    for (const type in cpu.times) {
      total += cpu.times[type];
    }
    idle += cpu.times.idle;
  }
  const cpuPercent = total > 0 ? (1 - idle / total) * 100 : 0;
  const slotsFree = Math.max(0, MAX_CONCURRENT - activeCount);

  res.json({
    ram_used_gb: round(usedMemGB, 2),
    ram_total_gb: round(totalMemGB, 2),
    ram_limit_gb: MAX_RAM_GB,
    cpu_percent: round(cpuPercent, 2),
    slots_free: slotsFree,
    slots_total: MAX_CONCURRENT,
    overloaded: usedMemGB > MAX_RAM_GB || slotsFree === 0,
  });
});

function round(n, decimals) {
  const factor = Math.pow(10, decimals);
  return Math.round(n * factor) / factor;
}

app.get("/test", (req, res) => {
  const btnLabel = "\u25B6 \u0110\u1ECD8"; // ▶ Đọc
  const html = "<!DOCTYPE html>" +
    "<html>" +
    "<head><meta charset='utf-8'><title>TTS Test</title></head>" +
    "<body style='font-family:sans-serif;max-width:600px;margin:40px auto;padding:0 20px'>" +
    "<h2>TTS Server Test</h2>" +
    "<textarea id='text' rows='4' style='width:100%;font-size:16px'>Xin chào, đây là thử nghiệm giọng đọc tiếng Việt.</textarea>" +
    "<br><br>" +
    "<select id='voice'>" +
    "<option value='ngochuyennew'>Ngọc Huyền New</option>" +
    "<option value='ngocngan3701'>Ngọc Ngạn</option>" +
    "</select>" +
    "<select id='speed'>" +
    "<option value='0.75'>0.75x</option>" +
    "<option value='1.0' selected>1.0x</option>" +
    "<option value='1.25'>1.25x</option>" +
    "<option value='1.5'>1.5x</option>" +
    "</select>" +
    "<button onclick='speak()' style='padding:8px 20px;font-size:16px'>▶ Đọc</button>" +
    "<p id='status' style='color:#888'></p>" +
    "<script>" +
    "async function speak() {" +
    "  const btn = document.querySelector('button');" +
    "  const status = document.getElementById('status');" +
    "  btn.disabled = true;" +
    "  status.textContent = 'Đang tạo âm thanh...';" +
    "  const queue = [];" +
    "  let currentAudio = null;" +
    "  let playing = false;" +
    "  function playNext() {" +
    "    if (!queue.length) { playing = false; status.textContent = 'Xong!'; btn.disabled = false; return; }" +
    "    playing = true;" +
    "    const blob = queue.shift();" +
    "    const url = URL.createObjectURL(blob);" +
    "    currentAudio = new Audio(url);" +
    "    currentAudio.onended = () => { URL.revokeObjectURL(url); currentAudio = null; playNext(); };" +
    "    currentAudio.onerror = (e) => { console.error('Audio error', e); playNext(); };" +
    "    currentAudio.play().catch(e => { console.error('Play failed', e); playNext(); });" +
    "    status.textContent = 'Đang đọc... (còn ' + queue.length + ' đoạn chờ)';" +
    "  }" +
    "  const res = await fetch('/tts/stream', {" +
    "    method: 'POST'," +
    "    headers: {'Content-Type':'application/json'}," +
    "    body: JSON.stringify({" +
    "      text: document.getElementById('text').value," +
    "      voice: document.getElementById('voice').value," +
    "      speed: parseFloat(document.getElementById('speed').value)" +
    "    })" +
    "  });" +
    "  const reader = res.body.getReader();" +
    "  const decoder = new TextDecoder();" +
    "  let buf = '';" +
    "  let chunkCount = 0;" +
    "  while (true) {" +
    "    const {done, value} = await reader.read();" +
    "    if (done) break;" +
    "    buf += decoder.decode(value, {stream: true});" +
    "    const lines = buf.split('\\n');" +
    "    buf = lines.pop();" +
    "    for (const line of lines) {" +
    "      if (!line.startsWith('data: ')) continue;" +
    "      const payload = line.slice(6).trim();" +
    "      if (payload === '[DONE]') { if (!playing && !queue.length) { status.textContent = 'Xong!'; btn.disabled = false; } break; }" +
    "      const {audio} = JSON.parse(payload);" +
    "      if (!audio) continue;" +
    "      const bytes = Uint8Array.from(atob(audio), c => c.charCodeAt(0));" +
    "      const blob = new Blob([bytes], {type:'audio/wav'});" +
    "      queue.push(blob);" +
    "      chunkCount++;" +
    "      status.textContent = 'Đã tạo ' + chunkCount + ' đoạn...';" +
    "      if (!playing) playNext();" +
    "    }" +
    "  }" +
    "  status.textContent = 'Xong! (' + chunkCount + ' chunk)';" +
    "}" +
    "</script>" +
    "</body>" +
    "</html>";
  res.send(html);
});

if (require.main === module) {
  loadVoices();
  const port = parseInt(process.env.PORT || "3002", 10);
  console.log(`TTS server running on port ${port}`);
  app.listen(port, "0.0.0.0");
}

module.exports = app;
