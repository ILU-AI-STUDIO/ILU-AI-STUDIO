import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI(title="ILU HD API", version="2.0.0")

origins = [x.strip() for x in os.getenv("FRONTEND_ORIGINS", "").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Comma-separated exact hostnames. Do not use '*' or broad wildcards.
allowed_hosts = {
    x.strip().lower().rstrip(".")
    for x in os.getenv("ALLOWED_HOSTS", "").split(",")
    if x.strip()
}
MAX_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "300")) * 1024 * 1024

PLATFORM_HOSTS = {
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"},
    "instagram": {"instagram.com", "www.instagram.com", "m.instagram.com"},
    "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch", "www.fb.watch"},
}


def identify_platform(host: str) -> str:
    host = host.lower().rstrip(".")
    for platform, hosts in PLATFORM_HOSTS.items():
        if host in hosts:
            return platform
    return "direct_or_other"


def parse_https_url(raw: str):
    try:
        p = urlparse(raw)
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            raise ValueError
        if p.port not in (None, 443):
            raise ValueError
        host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        return p, host
    except Exception:
        raise HTTPException(400, "Enter a valid HTTPS URL.")


def validate_direct_host(host: str):
    if identify_platform(host) != "direct_or_other":
        raise HTTPException(
            415,
            "This is a platform page link. This endpoint only accepts direct video-file URLs; platform-specific authorized integrations are not connected.",
        )
    if not allowed_hosts:
        raise HTTPException(503, "The service owner has not configured any trusted video hosts yet.")
    if host not in allowed_hosts:
        raise HTTPException(403, "This video host is not enabled by the site owner.")

    # Reject private, loopback, link-local and reserved destinations.
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not infos:
            raise ValueError
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise HTTPException(400, "Private or local network addresses are not allowed.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "The video host could not be validated.")


@app.get("/health")
def health():
    return {"ok": True, "service": "ILU HD API", "version": "2.0.0"}


@app.get("/inspect")
def inspect(url: str = Query(..., min_length=8, max_length=2048)):
    _, host = parse_https_url(url)
    platform = identify_platform(host)
    if platform == "direct_or_other":
        supported = bool(allowed_hosts and host in allowed_hosts)
        return {
            "platform": "direct_video_link",
            "host": host,
            "download_available": supported,
            "message": "This host is enabled for direct video-file links." if supported else "This host is not enabled. The site owner must add this exact hostname to ALLOWED_HOSTS.",
        }
    return {
        "platform": platform,
        "host": host,
        "download_available": False,
        "message": f"{platform.title()} link detected. A permitted, platform-specific integration is not connected, so this page link cannot be downloaded here.",
    }


@app.get("/download")
async def download(url: str = Query(..., min_length=12, max_length=2048)):
    parsed, host = parse_https_url(url)
    validate_direct_host(host)

    timeout = httpx.Timeout(25.0, connect=10.0)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    ) as client:
        try:
            upstream = await client.send(
                client.build_request("GET", url, headers={"User-Agent": "ILU-HD/2.0"}),
                stream=True,
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Could not reach the video host.")

        if upstream.status_code in (301, 302, 303, 307, 308):
            await upstream.aclose()
            raise HTTPException(400, "Redirected links are not supported. Use the final direct video URL.")
        if upstream.status_code != 200:
            await upstream.aclose()
            raise HTTPException(502, f"Video host returned HTTP {upstream.status_code}.")

        content_type = upstream.headers.get("content-type", "").split(";")[0].lower()
        if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
            await upstream.aclose()
            raise HTTPException(415, "This is not a direct video file. Platform page links are not supported.")

        length = upstream.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_BYTES:
                    await upstream.aclose()
                    raise HTTPException(413, "File exceeds the configured download limit.")
            except ValueError:
                await upstream.aclose()
                raise HTTPException(502, "Video host sent an invalid file size.")

        filename = parsed.path.rsplit("/", 1)[-1] or "ilu-hd-video.mp4"
        filename = "".join(c for c in filename if c.isalnum() or c in "._-")[:100] or "ilu-hd-video.mp4"

        async def stream():
            total = 0
            try:
                async for chunk in upstream.aiter_bytes(64 * 1024):
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
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
