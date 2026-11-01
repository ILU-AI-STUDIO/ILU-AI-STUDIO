import os
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="ILU HD API")

origins = [
    x.strip()
    for x in os.getenv("FRONTEND_ORIGINS", "").split(",")
    if x.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET"],
    allow_headers=["*"]
)

allowed_hosts = {
    x.strip().lower()
    for x in os.getenv("ALLOWED_HOSTS", "").split(",")
    if x.strip()
}

MAX_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "300")) * 1024 * 1024


def validate_url(raw):
    try:
        p = urlparse(raw)

        if p.scheme != "https" or not p.hostname:
            raise ValueError()

        host = p.hostname.encode("idna").decode().lower()

        if not allowed_hosts or host not in allowed_hosts:
            raise HTTPException(403, "This video host is not enabled.")

        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)

        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise HTTPException(400, "Private addresses are not allowed.")

        return host

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Enter a valid HTTPS video URL.")


@app.get("/health")
def health():
    return {"ok": True, "service": "ILU HD API"}


@app.get("/download")
async def download(url: str = Query(..., min_length=12, max_length=2048)):
    validate_url(url)

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=25.0,
        trust_env=False
    ) as client:

        try:
            upstream = await client.send(
                client.build_request(
                    "GET",
                    url,
                    headers={"User-Agent": "ILU-HD/1.0"}
                ),
                stream=True
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Could not reach the video host.")

        if upstream.status_code != 200:
            await upstream.aclose()
            raise HTTPException(502, "Video host returned an error.")

        content_type = upstream.headers.get(
            "content-type", ""
        ).split(";")[0].lower()

        if not (
            content_type.startswith("video/")
            or content_type == "application/octet-stream"
        ):
            await upstream.aclose()
            raise HTTPException(415, "This is not a direct video file.")

        length = upstream.headers.get("content-length")

        if length and int(length) > MAX_BYTES:
            await upstream.aclose()
            raise HTTPException(413, "Video is too large.")

        name = urlparse(url).path.rsplit("/", 1)[-1]
        name = "".join(
            c for c in name if c.isalnum() or c in "._-"
        )[:100] or "ilu-hd-video.mp4"

        async def stream():
            total = 0
            try:
                async for chunk in upstream.aiter_bytes(65536):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        break
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(),
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store"
            }
        )
