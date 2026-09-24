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
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with target.open("wb") as out:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    out.write(chunk)
    return target


def handler(event):
    inp = event.get("input") or {}
    source_url = str(inp.get("source_url", "")).strip()
    youtube_id = str(inp.get("youtube_id", "")).strip()
    upload_url = str(inp.get("upload_url", "")).strip()

    if not source_url:
        return {"error": "source_url is required"}

    with tempfile.TemporaryDirectory(prefix="nibras-demucs-") as tmp:
        work = Path(tmp)
        source = download_source(source_url, work)

        wav = work / "input.wav"
        run([
            "ffmpeg", "-y", "-i", str(source),
            "-vn", "-ac", "2", "-ar", "44100", str(wav)
        ])

        model = os.getenv("DEMUCS_MODEL", "htdemucs_ft")
        out_root = work / "demucs"
        run([
            sys.executable, "-m", "demucs",
            "--two-stems=vocals",
            "-n", model,
            "-o", str(out_root),
            str(wav)
        ])

        vocals = out_root / model / "input" / "vocals.wav"
        if not vocals.exists():
            raise RuntimeError("Demucs vocals output was not found")

        filename = f"{youtube_id}.m4a" if youtube_id else "vocals.m4a"
        final = work / filename
        run([
            "ffmpeg", "-y", "-i", str(vocals),
            "-c:a", "aac", "-b:a", "192k", str(final)
        ])

        size = final.stat().st_size
        if upload_url:
            with final.open("rb") as fh:
                response = requests.post(
                    upload_url,
                    files={"file": (filename, fh, "audio/mp4")},
                    timeout=600,
                )
            response.raise_for_status()
            return {
                "filename": filename,
                "size": size,
                "model": model,
                "uploaded": True,
            }

        return {
            "filename": filename,
            "size": size,
            "model": model,
            "uploaded": False,
            "error": "upload_url is required for large audio results",
        }


runpod.serverless.start({"handler": handler})
