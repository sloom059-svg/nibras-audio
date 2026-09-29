import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import runpod


def run(cmd):
    subprocess.run(cmd, check=True)


def download_source(url: str, work: Path) -> Path:
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        target = work / "source.%(ext)s"
        run(["yt-dlp", "-f", "bestaudio/best", "--no-playlist", "-o", str(target), url])
        candidates = [p for p in work.iterdir() if p.name.startswith("source.")]
        if not candidates:
            raise RuntimeError("yt-dlp produced no file")
        return candidates[0]

    ext = Path(urlparse(url).path).suffix or ".bin"
    target = work / f"source{ext}"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with target.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    return target


def handler(event):
    inp = event.get("input") or {}
    source_url = str(inp.get("source_url", "")).strip()
    youtube_id = str(inp.get("youtube_id", "")).strip()
    upload_url = str(inp.get("upload_url", "")).strip()
    if not source_url:
        return {"error": "source_url is required"}

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        source = download_source(source_url, work)
        wav = work / "input.wav"
        run(["ffmpeg", "-y", "-i", str(source), "-vn", "-ac", "2", "-ar", "44100", str(wav)])

        out_root = work / "demucs"
        model = os.getenv("DEMUCS_MODEL", "htdemucs_ft")
        run([sys.executable, "-m", "demucs", "-n", model, "-o", str(out_root), str(wav)])

        stem_dir = out_root / model / "input"
        vocals = stem_dir / "vocals.wav"
        drums = stem_dir / "drums.wav"
        bass = stem_dir / "bass.wav"
        other = stem_dir / "other.wav"
        if not all(p.exists() for p in (vocals, drums, bass, other)):
            raise RuntimeError("Demucs four-stem output was not found")

        other_gain = os.getenv("NIBRAS_OTHER_GAIN", "0.30")
        drums_gain = os.getenv("NIBRAS_DRUMS_GAIN", "0.12")
        bass_gain = os.getenv("NIBRAS_BASS_GAIN", "0.04")

        name = f"{youtube_id}.m4a" if youtube_id else "clean.m4a"
        final = work / name
        run([
            "ffmpeg", "-y",
            "-i", str(vocals), "-i", str(other), "-i", str(drums), "-i", str(bass),
            "-filter_complex",
            f"[0:a]volume=1.0[v];[1:a]volume={other_gain}[o];"
            f"[2:a]volume={drums_gain}[d];[3:a]volume={bass_gain}[b];"
            "[v][o][d][b]amix=inputs=4:duration=longest:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95[out]",
            "-map", "[out]", "-c:a", "aac", "-b:a", "192k", str(final)
        ])

        size = final.stat().st_size
        if upload_url:
            with final.open("rb") as fh:
                response = requests.post(
                    upload_url,
                    files={"file": (name, fh, "audio/mp4")},
                    timeout=600,
                )
            response.raise_for_status()
            return {"filename": name, "size": size, "model": model, "uploaded": True}

        raw = final.read_bytes()
        return {
            "filename": name,
            "audio_base64": base64.b64encode(raw).decode("ascii"),
            "size": len(raw),
            "model": model,
            "uploaded": False,
        }


runpod.serverless.start({"handler": handler})
