import asyncio
import base64
import os
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
RUNPOD_BASE = "https://api.runpod.ai/v2"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/nibras-output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Nibras Audio - Music Remover", version="1.0.0")


def _headers():
    if not RUNPOD_API_KEY:
        raise HTTPException(503, "RUNPOD_API_KEY is not configured")
    return {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}


def _endpoint():
    if not RUNPOD_ENDPOINT_ID:
        raise HTTPException(503, "RUNPOD_ENDPOINT_ID is not configured")
    return RUNPOD_ENDPOINT_ID


async def _submit(source_url: str, youtube_id: Optional[str]):
    payload = {"input": {"source_url": source_url, "youtube_id": youtube_id or ""}}
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(f"{RUNPOD_BASE}/{_endpoint()}/run", headers=_headers(), json=payload)
        if r.status_code >= 300:
            raise HTTPException(502, f"RunPod submit failed: {r.text[:500]}")
        data = r.json()
    job_id = data.get("id")
    if not job_id:
        raise HTTPException(502, "RunPod did not return a job id")
    return job_id


async def _wait(job_id: str, timeout_seconds: int = 1800):
    deadline = time.time() + timeout_seconds
    async with httpx.AsyncClient(timeout=45) as client:
        while time.time() < deadline:
            r = await client.get(f"{RUNPOD_BASE}/{_endpoint()}/status/{job_id}", headers=_headers())
            if r.status_code >= 300:
                raise HTTPException(502, f"RunPod status failed: {r.text[:500]}")
            data = r.json()
            status = data.get("status")
            if status == "COMPLETED":
                return data.get("output") or {}
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                raise HTTPException(502, f"RunPod job ended with {status}: {data}")
            await asyncio.sleep(2)
    raise HTTPException(504, "RunPod processing timed out")


def _save_output(output: dict) -> Path:
    b64 = output.get("audio_base64")
    filename = output.get("filename") or "vocals.m4a"
    filename = Path(filename).name
    if not b64:
        raise HTTPException(502, "RunPod worker returned no audio")
    try:
        raw = base64.b64decode(b64)
    except Exception as exc:
        raise HTTPException(502, f"Invalid audio output: {exc}")
    path = OUTPUT_DIR / filename
    path.write_bytes(raw)
    return path


@app.get("/health")
def health():
    return {
        "ok": True,
        "runpod_key_configured": bool(RUNPOD_API_KEY),
        "runpod_endpoint_configured": bool(RUNPOD_ENDPOINT_ID),
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html>
<html lang="ar" dir="rtl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nibras Audio</title>
<style>
body{font-family:Arial,sans-serif;background:#0e1726;color:#fff;max-width:760px;margin:50px auto;padding:20px}
.card{background:#16243a;padding:28px;border-radius:20px}input,button{width:100%;box-sizing:border-box;padding:14px;margin:8px 0;border-radius:12px;border:0}
button{background:#22c55e;color:#08130c;font-weight:700;cursor:pointer}.muted{opacity:.7;font-size:14px}
</style></head>
<body><div class="card"><h1>نبراس — إزالة الموسيقى</h1>
<p>ضع رابط الفيديو أو الصوت، وسيتم إرساله إلى RunPod لمعالجته بواسطة Demucs.</p>
<form action="/process" method="post">
<input name="source_url" placeholder="https://..." required>
<input name="youtube_id" placeholder="YouTube ID (اختياري)">
<button type="submit">إزالة الموسيقى</button>
</form><p class="muted">النتيجة: ملف M4A يحتوي على مسار vocals.</p></div></body></html>"""


@app.post("/process")
async def process(source_url: str = Form(...), youtube_id: str = Form("")):
    job_id = await _submit(source_url, youtube_id or None)
    output = await _wait(job_id)
    path = _save_output(output)
    return FileResponse(path, media_type="audio/mp4", filename=path.name)


@app.post("/api/process")
async def api_process(payload: dict):
    source_url = str(payload.get("source_url", "")).strip()
    youtube_id = str(payload.get("youtube_id", "")).strip() or None
    if not source_url:
        raise HTTPException(400, "source_url is required")
    job_id = await _submit(source_url, youtube_id)
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.get(f"{RUNPOD_BASE}/{_endpoint()}/status/{job_id}", headers=_headers())
        return r.json()
