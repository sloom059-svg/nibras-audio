#!/usr/bin/env python3
"""Upload audio to Acapella Extractor and optionally publish the result to GitHub.

The website receives only the audio file. A YouTube URL, when supplied, is used
locally to derive the catalog/video id and does not get sent to the website.
"""

from __future__ import annotations

import argparse
import base64
import html
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests

SITE_URL = "https://www.acapella-extractor.com/en/"
MAX_BYTES = 80 * 1024 * 1024
MAX_SECONDS = 600.0
TIMEOUT = (30, 2400)


def run(command: list[str]) -> str:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout[-4000:])
    return result.stdout


def probe(path: Path) -> tuple[float, int]:
    output = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(output.strip()), path.stat().st_size


def youtube_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", value):
        return value
    parsed = urlparse(value)
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/").split("/")[0]
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id
    match = re.search(r"/(?:shorts|embed|live)/([^/?]+)", parsed.path)
    if match:
        return match.group(1)
    raise ValueError("تعذر استخراج معرّف YouTube؛ استخدم --id أو --youtube-url")


def safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]", "", value)
    if not value:
        raise ValueError("معرّف الفيديو فارغ")
    return value


def prepare_input(source: Path, workdir: Path) -> Path:
    duration, size = probe(source)
    if duration > MAX_SECONDS + 0.1:
        raise ValueError(
            f"الموقع يقبل مدة أقصاها 10 دقائق، والملف مدته {duration / 60:.2f} دقيقة"
        )
    if size <= MAX_BYTES:
        return source

    compressed = workdir / "upload.mp3"
    print(f"ضغط الملف من {size / 1024 / 1024:.1f}MB إلى MP3...")
    run([
        "ffmpeg", "-y", "-i", str(source), "-vn",
        "-codec:a", "libmp3lame", "-b:a", "128k",
        str(compressed),
    ])
    if compressed.stat().st_size > MAX_BYTES:
        raise ValueError("تعذر جعل الملف أقل من 80MB")
    return compressed


def upload_to_site(session: requests.Session, path: Path) -> str:
    page = session.get(SITE_URL, timeout=TIMEOUT)
    page.raise_for_status()

    match = re.search(
        r'<form[^>]+action=["\']([^"\']+)["\'][^>]*>',
        page.text,
        flags=re.I,
    )
    if not match:
        raise RuntimeError("لم أجد نموذج الرفع في موقع Acapella Extractor")

    action = urljoin(page.url, html.unescape(match.group(1)))
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as stream:
        response = session.post(
            action,
            files={"file": (path.name, stream, mime)},
            allow_redirects=False,
            timeout=TIMEOUT,
        )

    if response.status_code >= 400:
        raise RuntimeError(f"الموقع رفض الرفع HTTP {response.status_code}: {response.text[:500]}")

    location = response.headers.get("Location")
    if location:
        return urljoin(response.url, location)

    body = response.text.strip()
    if body.startswith(("http://", "https://", "/")):
        return urljoin(response.url, body)

    raise RuntimeError("تم الرفع لكن الموقع لم يرجع رابط صفحة النتيجة")


def find_audio_link(session: requests.Session, result_url: str) -> tuple[str, bytes]:
    response = session.get(result_url, timeout=TIMEOUT)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()

    if content_type.startswith(("audio/", "application/octet-stream")):
        return response.url, response.content

    links = re.findall(
        r'(?:href|src|data-download)=["\']([^"\']+)["\']',
        response.text,
        flags=re.I,
    )
    candidates = []
    for link in links:
        candidate = urljoin(response.url, html.unescape(link))
        lower = candidate.lower()
        if re.search(r"\.(?:wav|mp3|m4a|ogg|flac|wma)(?:$|[?#])", lower):
            candidates.append(candidate)
        elif "download" in lower:
            candidates.append(candidate)

    for candidate in candidates:
        downloaded = session.get(candidate, timeout=TIMEOUT)
        downloaded.raise_for_status()
        ctype = downloaded.headers.get("content-type", "").lower()
        if ctype.startswith("audio/") or len(downloaded.content) > 1024:
            return downloaded.url, downloaded.content

    raise RuntimeError(
        "تعذر العثور على رابط تنزيل الناتج. افتح صفحة النتيجة يدويًا: " + response.url
    )


def convert_to_m4a(source: Path, target: Path) -> None:
    run([
        "ffmpeg", "-y", "-i", str(source),
        "-vn", "-codec:a", "aac", "-b:a", "128k",
        str(target),
    ])


def github_upload(path: Path, video_id: str) -> str:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPO", "").strip()
    branch = os.getenv("GITHUB_BRANCH", "main").strip()
    folder = os.getenv("GITHUB_FOLDER", "audio").strip().strip("/")
    if not token or "/" not in repo:
        raise RuntimeError("أضف GITHUB_TOKEN و GITHUB_REPO قبل الرفع إلى GitHub")

    remote_path = f"{folder}/{video_id}.m4a" if folder else f"{video_id}.m4a"
    api = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    existing = requests.get(api, headers=headers, params={"ref": branch}, timeout=30)
    if existing.status_code == 200:
        return f"skipped: {remote_path}"
    if existing.status_code != 404:
        raise RuntimeError(f"GitHub check failed: {existing.status_code}")

    encoded = base64.b64encode(path.read_bytes()).decode()
    uploaded = requests.put(
        api,
        headers=headers,
        json={
            "message": f"Add acapella audio {video_id}",
            "content": encoded,
            "branch": branch,
        },
        timeout=180,
    )
    if uploaded.status_code not in (200, 201):
        raise RuntimeError(f"GitHub upload failed: {uploaded.status_code}: {uploaded.text[:600]}")
    return f"uploaded: {remote_path}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Acapella Extractor uploader")
    parser.add_argument("--input", required=True, help="ملف MP3/WAV")
    parser.add_argument("--id", help="معرّف الفيديو أو رابط YouTube")
    parser.add_argument("--youtube-url", help="رابط YouTube لاستخراج المعرّف والتسمية")
    parser.add_argument("--output-dir", default="output", help="مجلد الناتج")
    parser.add_argument("--upload-github", action="store_true", help="رفع الناتج إلى audio/ في GitHub")
    args = parser.parse_args()

    raw_id = args.id or args.youtube_url
    if not raw_id:
        raise SystemExit("استخدم --id أو --youtube-url")

    video_id = safe_id(youtube_id(raw_id))
    source = Path(args.input).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"الملف غير موجود: {source}")

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="acapella-") as temp:
        workdir = Path(temp)
        upload_path = prepare_input(source, workdir)
        print(f"رفع {upload_path.name} إلى Acapella Extractor...")
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (Nibras audio uploader)"
        result_url = upload_to_site(session, upload_path)
        print(f"تم قبول الملف: {result_url}")
        _, result_bytes = find_audio_link(session, result_url)
        downloaded = workdir / "result.download"
        downloaded.write_bytes(result_bytes)
        final = output_dir / f"{video_id}.m4a"
        convert_to_m4a(downloaded, final)

    print(f"الناتج: {final}")
    if args.upload_github:
        print(github_upload(final, video_id))


if __name__ == "__main__":
    main()
