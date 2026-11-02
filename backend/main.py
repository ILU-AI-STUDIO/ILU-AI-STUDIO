import ipaddress
import os
import socket
from urllib.parse import urlparse, unquote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI(title="ILU HD API", version="2.3.0")

origins = [
    x.strip().rstrip("/")
    for x in os.getenv("FRONTEND_ORIGINS", "").split(",")
    if x.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "Content-Length",
        "Content-Type"
    ],
)

allowed_hosts = {
    x.strip().lower().rstrip(".")
    for x in os.getenv("ALLOWED_HOSTS", "").split(",")
    if x.strip()
}

MAX_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "300")) * 1024 * 1024


# PLATFORM DETECTION

PLATFORM_DOMAINS = {
    "YouTube": [
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
    ],
    "Instagram": [
        "instagram.com",
    ],
    "Facebook": [
        "facebook.com",
        "fb.watch",
    ],
    "TikTok": [
        "tiktok.com",
    ],
    "X (Twitter)": [
        "x.com",
        "twitter.com",
        "t.co",
    ],
    "Vimeo": [
        "vimeo.com",
    ],
    "Dailymotion": [
        "dailymotion.com",
        "dai.ly",
    ],
    "Twitch": [
        "twitch.tv",
    ],
    "Reddit": [
        "reddit.com",
        "redd.it",
    ],
    "Snapchat": [
        "snapchat.com",
        "snap.com",
    ],
    "Pinterest": [
        "pinterest.com",
        "pin.it",
    ],
    "LinkedIn": [
        "linkedin.com",
    ],
    "Threads": [
        "threads.net",
    ],
}


def identify_platform(host):
    host = host.lower().rstrip(".")

    for platform, domains in PLATFORM_DOMAINS.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return platform

    return "Direct Video / Other"


def parse_https_url(raw):
    try:
        p = urlparse(raw)

        if (
            p.scheme != "https"
            or not p.hostname
            or p.username
            or p.password
            or p.port not in (None, 443)
        ):
            raise ValueError

        host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")

        return p, host

    except Exception:
        raise HTTPException(400, "Enter a valid HTTPS URL.")


def validate_direct_host(host):
    if identify_platform(host) != "Direct Video / Other":
        raise HTTPException(
            415,
            "Platform page links require an authorized integration."
        )

    if not allowed_hosts or host not in allowed_hosts:
        raise HTTPException(
            403,
            "This exact video hostname is not enabled."
        )

    try:
        infos = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM
        )

        if not infos:
            raise ValueError

        for info in infos:
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise HTTPException(
                    400,
                    "Private or local network addresses are not allowed."
                )

    except HTTPException:
        raise

    except Exception:
        raise HTTPException(
            400,
            "The video host could not be validated."
        )


def safe_filename(path):
    name = unquote(path.rsplit("/", 1)[-1]) or "ilu-hd-video"

    name = "".join(
        c for c in name
        if c.isalnum() or c in "._- "
    )[:100].strip()

    return name or "ilu-hd-video"


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "ILU HD API",
        "version": "2.3.0"
    }


# AUTOMATIC PLATFORM INSPECTION

@app.get("/inspect")
def inspect(url: str = Query(..., min_length=8, max_length=2048)):

    _, host = parse_https_url(url)

    platform = identify_platform(host)

    if platform != "Direct Video / Other":

        return {
            "platform": platform,
            "host": host,
            "download_available": False,
            "integration_required": True,
            "message": (
                f"{platform} detected successfully. "
                "Download requires an authorized platform integration "
                "or a creator-enabled downloadable video."
            )
        }

    supported = bool(
        allowed_hosts and host in allowed_hosts
    )

    return {
        "platform": "Direct Video",
        "host": host,
        "download_available": supported,
        "integration_required": False,
        "message": (
            "Trusted direct-video host."
            if supported
            else "This hostname is not enabled."
        )
    }


@app.get("/info")
async def info(url: str = Query(..., min_length=12, max_length=2048)):

    parsed, host = parse_https_url(url)

    validate_direct_host(host)

    timeout = httpx.Timeout(
        15.0,
        connect=8.0
    )

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False
    ) as client:

        try:
            response = await client.head(
                url,
                headers={"User-Agent": "ILU-HD/2.3"}
            )

        except httpx.HTTPError:
            raise HTTPException(
                502,
                "Could not contact the video host."
            )

        if response.status_code in (301, 302, 303, 307, 308):
            raise HTTPException(
                400,
                "Redirected links are not supported."
            )

        if response.status_code >= 400:
            raise HTTPException(
                502,
                f"Video host returned HTTP {response.status_code}."
            )

        content_type = response.headers.get(
            "content-type", ""
        ).split(";")[0].lower()

        if not (
            content_type.startswith("video/")
            or content_type == "application/octet-stream"
        ):
            raise HTTPException(
                415,
                "This URL does not appear to be a direct video file."
            )

        raw_size = response.headers.get("content-length")

        size = (
            int(raw_size)
            if raw_size and raw_size.isdigit()
            else None
        )

        if size is not None and size > MAX_BYTES:
            raise HTTPException(
                413,
                "File exceeds the configured download limit."
            )

        return {
            "host": host,
            "filename": safe_filename(parsed.path),
            "content_type": content_type,
            "size_bytes": size,
            "size_mb": (
                round(size / (1024 * 1024), 2)
                if size is not None else None
            ),
            "format": content_type.split("/")[-1].upper(),
            "duration": None,
            "thumbnail": None,
            "note": (
                "Duration and thumbnail may not be available "
                "for direct video URLs."
            )
        }


@app.get("/download")
async def download(url: str = Query(..., min_length=12, max_length=2048)):

    parsed, host = parse_https_url(url)

    validate_direct_host(host)

    timeout = httpx.Timeout(
        25.0,
        connect=10.0
    )

    client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False
    )

    try:
        upstream = await client.send(
            client.build_request(
                "GET",
                url,
                headers={"User-Agent": "ILU-HD/2.3"}
            ),
            stream=True,
        )

    except httpx.HTTPError:
        await client.aclose()

        raise HTTPException(
            502,
            "Could not reach the video host."
        )

    if upstream.status_code in (301, 302, 303, 307, 308):
        await upstream.aclose()
        await client.aclose()

        raise HTTPException(
            400,
            "Redirected links are not supported."
        )

    if upstream.status_code != 200:
        code = upstream.status_code

        await upstream.aclose()
        await client.aclose()

        raise HTTPException(
            502,
            f"Video host returned HTTP {code}."
        )

    content_type = upstream.headers.get(
        "content-type", ""
    ).split(";")[0].lower()

    if not (
        content_type.startswith("video/")
        or content_type == "application/octet-stream"
    ):
        await upstream.aclose()
        await client.aclose()

        raise HTTPException(
            415,
            "This is not a direct video file."
        )

    length = upstream.headers.get("content-length")

    if length and length.isdigit() and int(length) > MAX_BYTES:
        await upstream.aclose()
        await client.aclose()

        raise HTTPException(
            413,
            "File exceeds the configured download limit."
        )

    filename = safe_filename(parsed.path)

    async def stream():
        total = 0

        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):

                total += len(chunk)

                if total > MAX_BYTES:
                    raise RuntimeError(
                        "Download size limit exceeded."
                    )

                yield chunk

        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {
        "Content-Disposition": (
            f'attachment; filename="{filename}"'
        ),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }

    if length and length.isdigit():
        headers["Content-Length"] = length

    return StreamingResponse(
        stream(),
        media_type=content_type,
        headers=headers,
    )
