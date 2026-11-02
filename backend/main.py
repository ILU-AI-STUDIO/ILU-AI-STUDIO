import ipaddress
import os
import re
import socket
from urllib.parse import urlparse, unquote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse

VERSION = "3.3.0"
USER_AGENT = "ILU-HD/3.3"
VIMEO_API = "https://api.vimeo.com"

app = FastAPI(title="ILU HD API", version=VERSION)

# Render environment:
# FRONTEND_ORIGINS=https://ilu-ai-studio.github.io
# ALLOWED_HOSTS=www.w3schools.com
# MAX_DOWNLOAD_MB=300
# VIMEO_ACCESS_TOKEN=your_private_token (optional; never put it in GitHub)
origins = [
    value.strip().rstrip("/")
    for value in os.getenv("FRONTEND_ORIGINS", "").split(",")
    if value.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "Content-Length",
        "Content-Type",
    ],
)

ALLOWED_HOSTS = {
    value.strip().lower().rstrip(".")
    for value in os.getenv("ALLOWED_HOSTS", "").split(",")
    if value.strip()
}

MAX_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "300")) * 1024 * 1024
VIMEO_ACCESS_TOKEN = os.getenv("VIMEO_ACCESS_TOKEN", "").strip()
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_ACCESS_TOKEN = os.getenv("TWITCH_ACCESS_TOKEN", "").strip()
TWITCH_EDITOR_ID = os.getenv("TWITCH_EDITOR_ID", "").strip()
TWITCH_API = "https://api.twitch.tv/helix"


# -------------------------------------------------------------------
# PLATFORM DETECTION
# -------------------------------------------------------------------

PLATFORM_DOMAINS = {
    "YouTube": [
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
    ],
    "Instagram": ["instagram.com", "cdninstagram.com"],
    "Facebook": ["facebook.com", "fb.watch", "fbcdn.net"],
    "TikTok": ["tiktok.com"],
    "X (Twitter)": ["x.com", "twitter.com", "t.co"],
    "Vimeo": ["vimeo.com"],
    "Dailymotion": ["dailymotion.com", "dai.ly"],
    "Twitch": ["twitch.tv"],
    "Reddit": ["reddit.com", "redd.it"],
    "Snapchat": ["snapchat.com", "snap.com"],
    "Pinterest": ["pinterest.com", "pin.it"],
    "LinkedIn": ["linkedin.com"],
    "Threads": ["threads.net"],
}


def identify_platform(host: str) -> str:
    host = host.lower().rstrip(".")

    for platform, domains in PLATFORM_DOMAINS.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return platform

    return "Direct Video / Other"


# -------------------------------------------------------------------
# URL AND NETWORK VALIDATION
# -------------------------------------------------------------------

def parse_https_url(raw: str):
    try:
        parsed = urlparse(raw.strip())

        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Invalid HTTPS URL")

        host = (
            parsed.hostname.encode("idna")
            .decode("ascii")
            .lower()
            .rstrip(".")
        )

        return parsed, host

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid HTTPS URL.",
        )


def validate_public_host(host: str):
    """Reject private, loopback, link-local and other non-public DNS results."""
    try:
        records = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
        )

        if not records:
            raise ValueError("No DNS records")

        for record in records:
            address = ipaddress.ip_address(record[4][0])

            if not address.is_global:
                raise HTTPException(
                    status_code=400,
                    detail="Private or local network addresses are not allowed.",
                )

    except HTTPException:
        raise

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="The video host could not be validated.",
        )


def is_meta_media_cdn(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(
        host == domain or host.endswith("." + domain)
        for domain in ("cdninstagram.com", "fbcdn.net", "fbsbx.com")
    )


def validate_direct_host(host: str):
    if is_meta_media_cdn(host):
        validate_public_host(host)
        return

    if identify_platform(host) != "Direct Video / Other":
        raise HTTPException(
            status_code=415,
            detail="Platform page links require an authorized integration.",
        )

    if not ALLOWED_HOSTS or host not in ALLOWED_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="This exact video hostname is not enabled.",
        )

    validate_public_host(host)


def safe_filename(path: str, fallback: str = "ilu-hd-video") -> str:
    name = unquote(path.rsplit("/", 1)[-1]) or fallback

    name = "".join(
        character
        for character in name
        if character.isalnum() or character in "._- "
    )[:100].strip()

    return name or fallback


# -------------------------------------------------------------------
# VIMEO OFFICIAL API INTEGRATION
# -------------------------------------------------------------------

def extract_vimeo_id(parsed) -> str | None:
    """
    Supports standard Vimeo video URLs and player.vimeo.com/video/{id}.
    This extracts a numeric video ID only. Access is still controlled by
    Vimeo's API token permissions and the video's own download settings.
    """
    match = re.search(
        r"(?:^|/)(?:video/)?([0-9]{5,})(?:$|/)",
        parsed.path,
    )

    return match.group(1) if match else None


async def get_vimeo_video(video_id: str) -> dict:
    if not VIMEO_ACCESS_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=(
                "Vimeo integration is not configured. "
                "Add VIMEO_ACCESS_TOKEN in Render environment variables."
            ),
        )

    headers = {
        "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}",
        "Accept": "application/vnd.vimeo.*+json;version=3.4",
        "User-Agent": USER_AGENT,
    }

    timeout = httpx.Timeout(
        20.0,
        connect=10.0,
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            response = await client.get(
                f"{VIMEO_API}/videos/{video_id}",
                headers=headers,
                params={
                    "fields": (
                        "uri,name,duration,pictures.sizes,download"
                    )
                },
            )

        except httpx.HTTPError:
            raise HTTPException(
                status_code=502,
                detail="Could not contact the Vimeo API.",
            )

    if response.status_code == 401:
        raise HTTPException(
            status_code=502,
            detail=(
                "Vimeo rejected the access token. "
                "Check the token and its permissions."
            ),
        )

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail=(
                "Vimeo denied file access. The token needs the required "
                "public, private and video_files scopes, and the account "
                "must be eligible to access downloadable files."
            ),
        )

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail=(
                "Vimeo video was not found, or this account cannot access it."
            ),
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Vimeo API returned HTTP {response.status_code}.",
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Vimeo returned an unreadable response.",
        )

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail="Vimeo returned an unexpected video response.",
        )

    return data


def choose_vimeo_download(data: dict) -> dict:
    downloads = data.get("download") or []

    if not isinstance(downloads, list) or not downloads:
        raise HTTPException(
            status_code=403,
            detail=(
                "Vimeo did not provide a download link. The video owner "
                "may have disabled downloads, or the account/token may not "
                "have permission to access downloadable files."
            ),
        )

    usable = [
        item
        for item in downloads
        if isinstance(item, dict) and item.get("link")
    ]

    if not usable:
        raise HTTPException(
            status_code=404,
            detail="No downloadable Vimeo file was returned.",
        )

    # Prefer the highest available resolution, then the largest file.
    usable.sort(
        key=lambda item: (
            int(item.get("height") or 0),
            int(item.get("size") or 0),
        ),
        reverse=True,
    )

    return usable[0]


# -------------------------------------------------------------------
# TWITCH OFFICIAL CLIP DOWNLOAD INTEGRATION
# -------------------------------------------------------------------

def extract_twitch_clip_slug(parsed) -> str | None:
    """Read a Twitch clip slug from clips.twitch.tv or twitch.tv/.../clip/... URLs."""
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname and parsed.hostname.lower() == "clips.twitch.tv":
        return parts[0] if parts else None
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "clip":
            return parts[index + 1]
    return None


def twitch_is_configured() -> bool:
    return bool(TWITCH_CLIENT_ID and TWITCH_ACCESS_TOKEN and TWITCH_EDITOR_ID)


async def get_twitch_clip_download(parsed) -> tuple[dict, str]:
    """Ask Twitch for clip metadata and an authorized, temporary download URL."""
    if not twitch_is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Twitch integration is not configured. Add the Twitch Client ID, "
                "authorized user access token and editor user ID in Render. "
                "The token must have editor:manage:clips or channel:manage:clips."
            ),
        )

    slug = extract_twitch_clip_slug(parsed)
    if not slug or not re.fullmatch(r"[A-Za-z0-9_-]{3,100}", slug):
        raise HTTPException(status_code=400, detail="Could not read a Twitch clip ID from this URL.")

    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {TWITCH_ACCESS_TOKEN}",
        "User-Agent": USER_AGENT,
    }
    timeout = httpx.Timeout(20.0, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        try:
            clip_response = await client.get(
                f"{TWITCH_API}/clips", headers=headers, params={"id": slug}
            )
            if clip_response.status_code == 401:
                raise HTTPException(status_code=502, detail="Twitch rejected the token or Client ID. Check the Render settings.")
            if clip_response.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Twitch API returned HTTP {clip_response.status_code}.")
            clip_rows = clip_response.json().get("data", [])
            if not clip_rows:
                raise HTTPException(status_code=404, detail="Twitch clip was not found or is unavailable.")
            clip = clip_rows[0]

            # Twitch requires the authenticated broadcaster/editor relationship.
            params = {
                "broadcaster_id": clip.get("broadcaster_id", ""),
                "editor_id": TWITCH_EDITOR_ID,
                "clip_id": slug,
            }
            download_response = await client.get(
                f"{TWITCH_API}/clips/downloads", headers=headers, params=params
            )
            if download_response.status_code == 401:
                raise HTTPException(status_code=403, detail="Twitch authorization is missing the required clip-download scope, or the token user ID does not match TWITCH_EDITOR_ID.")
            if download_response.status_code == 403:
                raise HTTPException(status_code=403, detail="This Twitch account is not an authorized editor/broadcaster for this channel.")
            if download_response.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Twitch download API returned HTTP {download_response.status_code}.")
            rows = download_response.json().get("data", [])
            if not rows:
                raise HTTPException(status_code=404, detail="Twitch did not provide a downloadable clip file.")
            file_url = rows[0].get("landscape_download_url") or rows[0].get("portrait_download_url")
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(status_code=502, detail="Could not complete the Twitch API request.")

    if not file_url:
        raise HTTPException(status_code=403, detail="Twitch did not provide a download URL for this clip.")

    # Only accept HTTPS URLs on Twitch's official CDN domains.
    signed = urlparse(file_url)
    signed_host = (signed.hostname or "").lower().rstrip(".")
    if signed.scheme != "https" or not any(
        signed_host == domain or signed_host.endswith("." + domain)
        for domain in ("twitchcdn.net", "twitch.tv")
    ):
        raise HTTPException(status_code=502, detail="Twitch returned an unexpected download host.")

    return clip, file_url


# -------------------------------------------------------------------
# META OFFICIAL API INTEGRATIONS (ACCOUNT / PAGE OWNED CONTENT ONLY)
# -------------------------------------------------------------------

def instagram_is_configured() -> bool:
    # ILU HD does not require or use a connected Instagram account.
    return False


def facebook_is_configured() -> bool:
    # ILU HD does not require or use a connected Facebook Page.
    return False


def extract_instagram_path(parsed) -> str | None:
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() in {"p", "reel", "reels", "tv"}:
        return "/" + parts[0].lower() + "/" + parts[1] + "/"
    return None


def extract_facebook_video_id(parsed) -> str | None:
    from urllib.parse import parse_qs
    query = parse_qs(parsed.query)
    candidate = (query.get("v") or [None])[0]
    if not candidate:
        parts = [part for part in parsed.path.split("/") if part]
        for index, part in enumerate(parts[:-1]):
            if part.lower() in {"videos", "reel", "watch"} and parts[index + 1].isdigit():
                candidate = parts[index + 1]
                break
        if not candidate and parts and parts[-1].isdigit():
            candidate = parts[-1]
    return candidate if candidate and str(candidate).isdigit() else None


async def get_instagram_media(parsed) -> dict:
    raise HTTPException(
        status_code=403,
        detail=(
            "Instagram link detected, but Instagram does not provide ILU HD "
            "an unauthenticated official file-download link for arbitrary public "
            "posts or Reels. No account connection is requested. If you have a "
            "direct cdninstagram.com video-file URL, paste that URL instead."
        ),
    )


async def get_facebook_video(parsed) -> dict:
    raise HTTPException(
        status_code=403,
        detail=(
            "Facebook link detected, but ILU HD does not use a connected Page "
            "account and cannot obtain an official file URL for arbitrary public "
            "videos. No account connection is requested. If you have a direct "
            "fbcdn.net video-file URL, paste that URL instead."
        ),
    )


# -------------------------------------------------------------------
# HEALTH AND PLATFORM INSPECTION
# -------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "ILU HD API",
        "version": VERSION,
        "vimeo_configured": bool(VIMEO_ACCESS_TOKEN),
        "twitch_configured": twitch_is_configured(),
        "instagram_account_required": False,
        "facebook_account_required": False,
        "meta_direct_cdn_supported": True,
        "tiktok_download_supported": False,
    }


@app.get("/inspect")
def inspect(
    url: str = Query(..., min_length=8, max_length=2048),
):
    parsed, host = parse_https_url(url)
    platform = identify_platform(host)

    if platform == "Vimeo":
        video_id = extract_vimeo_id(parsed)
        configured = bool(VIMEO_ACCESS_TOKEN and video_id)

        return {
            "platform": platform,
            "host": host,
            "download_available": configured,
            "integration_required": not configured,
            "provider": "vimeo" if configured else None,
            "message": (
                "Vimeo detected. Official API access is configured. "
                "The actual download is confirmed when Vimeo returns an "
                "authorized file link."
                if configured
                else (
                    "Vimeo detected. Configure an eligible Vimeo API token "
                    "with the required scopes. Downloads are available only "
                    "when Vimeo supplies an authorized download link."
                )
            ),
        }

    if platform == "Twitch":
        slug = extract_twitch_clip_slug(parsed)
        configured = twitch_is_configured() and bool(slug)
        return {
            "platform": platform,
            "host": host,
            "download_available": configured,
            "integration_required": not configured,
            "provider": "twitch" if configured else None,
            "message": (
                "Twitch clip detected. The official download API is configured; "
                "Twitch will still verify that the configured account is the "
                "broadcaster or an authorized channel editor."
                if configured else
                "Twitch detected. Clip downloads require a Twitch user access token "
                "with editor:manage:clips or channel:manage:clips, and the token "
                "owner must be the broadcaster or an authorized editor."
            ),
        }

    if is_meta_media_cdn(host):
        return {
            "platform": platform,
            "host": host,
            "download_available": True,
            "integration_required": False,
            "provider": "direct",
            "message": (
                "Direct media CDN link detected. ILU HD will verify that the URL "
                "returns a video file. This works without connecting an account; "
                "temporary or expired links may not work."
            ),
        }

    if platform == "Instagram":
        return {
            "platform": platform,
            "host": host,
            "download_available": False,
            "integration_required": False,
            "provider": None,
            "message": (
                "Instagram detected. No account connection is required. "
                "Instagram does not expose a general official download link "
                "for arbitrary public posts or Reels. Paste a direct "
                "cdninstagram.com video-file URL if you have one."
            ),
        }

    if platform == "Facebook":
        return {
            "platform": platform,
            "host": host,
            "download_available": False,
            "integration_required": False,
            "provider": None,
            "message": (
                "Facebook detected. No account connection is required. "
                "ILU HD cannot obtain an official file URL for arbitrary "
                "public Facebook videos. Paste a direct fbcdn.net video-file "
                "URL if you have one."
            ),
        }

    if platform == "TikTok":
        return {"platform": platform, "host": host, "download_available": False, "integration_required": True, "provider": None, "message": "TikTok detected. TikTok's official Display API provides creator-authorized video metadata and embeds, but does not provide a general video-file download endpoint. ILU HD will not extract or bypass platform media URLs."}

    if platform != "Direct Video / Other":
        return {
            "platform": platform,
            "host": host,
            "download_available": False,
            "integration_required": True,
            "provider": None,
            "message": (
                f"{platform} detected successfully. Its authorized download "
                "integration is not enabled yet. ILU HD does not bypass "
                "login, DRM, privacy or platform restrictions."
            ),
        }

    supported = bool(
        ALLOWED_HOSTS and host in ALLOWED_HOSTS
    )

    return {
        "platform": "Direct Video",
        "host": host,
        "download_available": supported,
        "integration_required": False,
        "provider": "direct" if supported else None,
        "message": (
            "Trusted direct-video host."
            if supported
            else "This exact hostname is not enabled."
        ),
    }


# -------------------------------------------------------------------
# VIDEO INFORMATION
# -------------------------------------------------------------------

@app.get("/info")
async def info(
    url: str = Query(..., min_length=12, max_length=2048),
):
    parsed, host = parse_https_url(url)
    platform = identify_platform(host)

    if platform == "Vimeo":
        video_id = extract_vimeo_id(parsed)

        if not video_id:
            raise HTTPException(
                status_code=400,
                detail="Could not read a Vimeo video ID from this URL.",
            )

        data = await get_vimeo_video(video_id)
        selected = choose_vimeo_download(data)

        pictures = data.get("pictures") or {}
        picture_sizes = pictures.get("sizes") or []

        thumbnail = (
            picture_sizes[-1].get("link")
            if picture_sizes
            else None
        )

        video_name = data.get("name") or f"vimeo-{video_id}"
        filename = safe_filename(video_name + ".mp4")
        size = selected.get("size")
        mime = selected.get("type") or "video/mp4"

        return {
            "provider": "vimeo",
            "host": host,
            "filename": filename,
            "content_type": mime,
            "size_bytes": size,
            "size_mb": (
                round(size / (1024 * 1024), 2)
                if isinstance(size, (int, float))
                else None
            ),
            "format": mime.split("/")[-1].upper(),
            "duration": data.get("duration"),
            "thumbnail": thumbnail,
            "quality": (
                selected.get("quality")
                or selected.get("rendition")
                or selected.get("height")
            ),
            "note": (
                "Official Vimeo download link. Vimeo signed links are "
                "temporary and are requested again when you start a download."
            ),
        }

    if platform == "Twitch":
        clip, _ = await get_twitch_clip_download(parsed)
        return {
            "provider": "twitch",
            "host": host,
            "filename": safe_filename((clip.get("title") or "twitch-clip") + ".mp4"),
            "content_type": "video/mp4",
            "size_bytes": None,
            "size_mb": None,
            "format": "MP4",
            "duration": clip.get("duration"),
            "thumbnail": clip.get("thumbnail_url"),
            "quality": "Official Twitch clip download",
            "note": "Twitch supplied an authorized temporary clip download URL. Availability depends on the configured broadcaster/editor account.",
        }

    if platform == "Instagram" and not is_meta_media_cdn(host):
        item = await get_instagram_media(parsed)
        title = (item.get("caption") or "instagram-video").splitlines()[0][:80]
        return {"provider": "instagram", "host": host, "filename": safe_filename(title + ".mp4"), "content_type": "video/mp4", "size_bytes": None, "size_mb": None, "format": "MP4", "duration": None, "thumbnail": item.get("thumbnail_url"), "quality": "Meta authorized media", "note": "Meta supplied this media URL for a video belonging to the connected Instagram Professional account. The URL is temporary and requested again at download time."}

    if platform == "Facebook" and not is_meta_media_cdn(host):
        item = await get_facebook_video(parsed)
        title = item.get("title") or "facebook-video"
        length = item.get("length")
        return {"provider": "facebook", "host": host, "filename": safe_filename(title + ".mp4"), "content_type": "video/mp4", "size_bytes": None, "size_mb": None, "format": "MP4", "duration": length, "thumbnail": item.get("picture"), "quality": "Meta Page-owned video", "note": "Meta supplied this source URL for a video accessible to the connected Facebook Page. The URL is temporary and requested again at download time."}

    validate_direct_host(host)

    timeout = httpx.Timeout(
        15.0,
        connect=8.0,
    )

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    ) as client:
        try:
            response = await client.head(
                url,
                headers={"User-Agent": USER_AGENT},
            )

        except httpx.HTTPError:
            raise HTTPException(
                status_code=502,
                detail="Could not contact the video host.",
            )

    if response.status_code in (301, 302, 303, 307, 308):
        raise HTTPException(
            status_code=400,
            detail="Redirected direct-video links are not supported.",
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Video host returned HTTP {response.status_code}.",
        )

    content_type = (
        response.headers.get("content-type", "")
        .split(";")[0]
        .lower()
    )

    if not (
        content_type.startswith("video/")
        or content_type == "application/octet-stream"
    ):
        raise HTTPException(
            status_code=415,
            detail="This URL does not appear to be a direct video file.",
        )

    raw_size = response.headers.get("content-length")
    size = (
        int(raw_size)
        if raw_size and raw_size.isdigit()
        else None
    )

    if size is not None and size > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail="File exceeds the configured download limit.",
        )

    return {
        "provider": "direct",
        "host": host,
        "filename": safe_filename(parsed.path),
        "content_type": content_type,
        "size_bytes": size,
        "size_mb": (
            round(size / (1024 * 1024), 2)
            if size is not None
            else None
        ),
        "format": content_type.split("/")[-1].upper(),
        "duration": None,
        "thumbnail": None,
        "note": (
            "Duration and thumbnail may not be available "
            "for direct video URLs."
        ),
    }


# -------------------------------------------------------------------
# DOWNLOAD
# -------------------------------------------------------------------

@app.get("/download")
async def download(
    url: str = Query(..., min_length=12, max_length=2048),
):
    parsed, host = parse_https_url(url)
    platform = identify_platform(host)

    if platform == "Vimeo":
        video_id = extract_vimeo_id(parsed)

        if not video_id:
            raise HTTPException(
                status_code=400,
                detail="Could not read a Vimeo video ID from this URL.",
            )

        data = await get_vimeo_video(video_id)
        selected = choose_vimeo_download(data)

        # Vimeo returns a signed, temporary download URL through its
        # authenticated API. We redirect the user's browser to that URL.
        # We do not fetch arbitrary user-provided CDN URLs on the server.
        return RedirectResponse(
            url=selected["link"],
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    if platform == "Twitch":
        _, signed_url = await get_twitch_clip_download(parsed)
        return RedirectResponse(
            url=signed_url,
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    if platform == "Instagram" and not is_meta_media_cdn(host):
        item = await get_instagram_media(parsed)
        return RedirectResponse(url=item["media_url"], status_code=302, headers={"Cache-Control": "no-store"})

    if platform == "Facebook" and not is_meta_media_cdn(host):
        item = await get_facebook_video(parsed)
        return RedirectResponse(url=item["source"], status_code=302, headers={"Cache-Control": "no-store"})

    validate_direct_host(host)

    timeout = httpx.Timeout(
        25.0,
        connect=10.0,
    )

    client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    )

    try:
        upstream = await client.send(
            client.build_request(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT},
            ),
            stream=True,
        )

    except httpx.HTTPError:
        await client.aclose()

        raise HTTPException(
            status_code=502,
            detail="Could not reach the video host.",
        )

    if upstream.status_code in (301, 302, 303, 307, 308):
        await upstream.aclose()
        await client.aclose()

        raise HTTPException(
            status_code=400,
            detail="Redirected direct-video links are not supported.",
        )

    if upstream.status_code != 200:
        code = upstream.status_code
        await upstream.aclose()
        await client.aclose()

        raise HTTPException(
            status_code=502,
            detail=f"Video host returned HTTP {code}.",
        )

    content_type = (
        upstream.headers.get("content-type", "")
        .split(";")[0]
        .lower()
    )

    if not (
        content_type.startswith("video/")
        or content_type == "application/octet-stream"
    ):
        await upstream.aclose()
        await client.aclose()

        raise HTTPException(
            status_code=415,
            detail="This is not a direct video file.",
        )

    content_length = upstream.headers.get("content-length")

    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > MAX_BYTES
    ):
        await upstream.aclose()
        await client.aclose()

        raise HTTPException(
            status_code=413,
            detail="File exceeds the configured download limit.",
        )

    filename = safe_filename(parsed.path)

    async def stream_video():
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

    if content_length and content_length.isdigit():
        headers["Content-Length"] = content_length

    return StreamingResponse(
        stream_video(),
        media_type=content_type,
        headers=headers,
    )
