import ipaddress
import os
import re
import socket
import tempfile
import shutil
import subprocess
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.parse import urlparse, unquote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse, FileResponse
from starlette.background import BackgroundTask

VERSION = "4.1.0"
USER_AGENT = "ILU-HD/4.1"
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


async def probe_video_url(client: httpx.AsyncClient, url: str):
    """Probe a direct media URL; fall back to a one-byte ranged GET when HEAD is unsupported."""
    try:
        response = await client.head(
            url,
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code not in (403, 405, 501):
            return response
        await response.aclose()
        return await client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Could not contact the video host.",
        )


def video_content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].lower()


def is_supported_video_response(response: httpx.Response) -> bool:
    content_type = video_content_type(response)
    # HLS/DASH manifests are playlists, not standalone video files.
    return content_type.startswith("video/") or content_type == "application/octet-stream"


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


class PublicVideoMetaParser(HTMLParser):
    """Read only standard Open Graph video metadata from a public HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.video_urls = []
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = {str(k).lower(): (v or "") for k, v in attrs}
        prop = (values.get("property") or values.get("name") or "").lower()
        content = values.get("content", "").strip()
        if content and prop in {"og:video", "og:video:url", "og:video:secure_url"}:
            self.video_urls.append(content)
        elif content and prop in {"og:title", "twitter:title"} and not self.title:
            self.title = content


async def resolve_public_meta_video(parsed, platform: str) -> dict:
    """Best-effort public-page metadata lookup; no cookies, tokens or login."""
    host = (parsed.hostname or "").lower().rstrip(".")
    if platform == "Instagram":
        allowed_page_hosts = {"instagram.com", "www.instagram.com", "m.instagram.com"}
        detail = (
            "Instagram page did not expose a public video file. The post may be "
            "private, login-gated, region-limited, or not a video. No account is used."
        )
    else:
        allowed_page_hosts = {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"}
        detail = (
            "Facebook page did not expose a public video file. The video may be "
            "private, login-gated, region-limited, or not directly downloadable. No account is used."
        )

    if host not in allowed_page_hosts:
        raise HTTPException(status_code=400, detail="Unsupported social-media page hostname.")

    validate_public_host(host)
    timeout = httpx.Timeout(12.0, connect=8.0)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ILU-HD/3.5; +https://ilu-ai-studio.github.io/ILU-AI-STUDIO/)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=False,
            headers=headers,
        ) as client:
            async with client.stream("GET", parsed.geturl()) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    raise HTTPException(status_code=403, detail=detail)
                if response.status_code != 200:
                    raise HTTPException(status_code=403, detail=detail)
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    raise HTTPException(status_code=415, detail="The public post did not return an HTML page.")
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > 2 * 1024 * 1024:
                        break
                    chunks.append(chunk)
                html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not read the public social-media page.")

    parser = PublicVideoMetaParser()
    parser.feed(html)
    for candidate in parser.video_urls:
        media_url = urljoin(parsed.geturl(), candidate)
        media_parsed, media_host = parse_https_url(media_url)
        if not is_meta_media_cdn(media_host):
            continue
        validate_public_host(media_host)
        return {"media_url": media_parsed.geturl(), "title": parser.title or platform.lower() + "-video"}

    raise HTTPException(status_code=403, detail=detail)


async def get_instagram_media(parsed) -> dict:
    path = extract_instagram_path(parsed)
    if not path or not re.fullmatch(r"/[A-Za-z]+/[A-Za-z0-9_-]{5,}/", path):
        raise HTTPException(status_code=400, detail="Invalid Instagram Reel or post link. Copy the full public link from Instagram.")
    return await resolve_public_meta_video(parsed, "Instagram")


async def get_facebook_video(parsed) -> dict:
    return await resolve_public_meta_video(parsed, "Facebook")


# -------------------------------------------------------------------
# LIMITED HLS SUPPORT (PUBLIC, UNENCRYPTED VOD MPEG-TS ONLY)
# -------------------------------------------------------------------

MAX_HLS_PLAYLIST_BYTES = 2 * 1024 * 1024
MAX_HLS_SEGMENTS = 500


def is_hls_url(parsed) -> bool:
    return (parsed.path or "").lower().endswith(".m3u8")


def parse_hls_media_playlist(text: str, playlist_url: str, playlist_host: str):
    """Parse a deliberately narrow, unencrypted HLS VOD playlist.

    Only ordinary MPEG-TS segment playlists are supported. Master playlists,
    live playlists, encryption, byte ranges and fragmented-MP4 maps are rejected.
    Every segment must be HTTPS and on the exact same hostname as the playlist.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise HTTPException(status_code=415, detail="This is not a valid HLS playlist.")

    forbidden_tags = (
        "#EXT-X-KEY", "#EXT-X-SESSION-KEY", "#EXT-X-MAP",
        "#EXT-X-BYTERANGE", "#EXT-X-DISCONTINUITY", "#EXT-X-PART",
        "#EXT-X-PRELOAD-HINT", "#EXT-X-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF",
    )
    for line in lines:
        if line.startswith(forbidden_tags):
            raise HTTPException(
                status_code=415,
                detail=("This HLS playlist uses encryption, a master/variant playlist, "
                        "fragmented MP4, byte ranges or unsupported stream features. "
                        "Only unencrypted MPEG-TS VOD playlists are supported."),
            )

    if "#EXT-X-ENDLIST" not in lines:
        raise HTTPException(
            status_code=415,
            detail="Live or unfinished HLS playlists are not supported; provide a completed VOD playlist.",
        )

    segment_urls = []
    duration = 0.0
    for line in lines:
        if line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1].split(",", 1)[0]
            try:
                duration += max(0.0, float(value))
            except ValueError:
                pass
        elif not line.startswith("#"):
            absolute = urljoin(playlist_url, line)
            segment_parsed, segment_host = parse_https_url(absolute)
            if segment_host != playlist_host:
                raise HTTPException(
                    status_code=403,
                    detail="HLS segments must use the same trusted hostname as the playlist.",
                )
            segment_urls.append(segment_parsed.geturl())
            if len(segment_urls) > MAX_HLS_SEGMENTS:
                raise HTTPException(
                    status_code=413,
                    detail=f"HLS playlist exceeds the {MAX_HLS_SEGMENTS}-segment limit.",
                )

    if not segment_urls:
        raise HTTPException(status_code=415, detail="The HLS playlist contains no media segments.")
    return segment_urls, round(duration, 2)


MAX_HLS_VARIANTS = 12


def parse_hls_master_playlist(text: str, master_url: str, master_host: str):
    """Parse a bounded HLS master playlist and return same-host variants."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise HTTPException(status_code=415, detail="This is not a valid HLS playlist.")
    if any(line.startswith(("#EXT-X-KEY", "#EXT-X-SESSION-KEY")) for line in lines):
        raise HTTPException(status_code=415, detail="Encrypted HLS playlists are not supported.")

    variants = []
    pending_attrs = None
    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF:"):
            if pending_attrs is not None:
                raise HTTPException(status_code=415, detail="Malformed HLS master playlist.")
            pending_attrs = line.split(":", 1)[1]
            continue
        if line.startswith("#"):
            continue
        if pending_attrs is None:
            # A media playlist has segment lines but no STREAM-INF entries.
            continue

        absolute = urljoin(master_url, line)
        variant_parsed, variant_host = parse_https_url(absolute)
        if variant_host != master_host:
            raise HTTPException(status_code=403, detail="HLS variants must use the same trusted hostname as the master playlist.")
        attrs = {}
        for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', pending_attrs):
            attrs[match.group(1)] = match.group(2).strip('"')
        resolution = attrs.get("RESOLUTION", "")
        bandwidth = int(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH") or 0)
        if resolution and re.fullmatch(r"\d{1,5}x\d{1,5}", resolution):
            width, height = map(int, resolution.split("x"))
            label = f"{height}p"
        else:
            width, height, label = 0, 0, (f"{round(bandwidth/1000)} kbps" if bandwidth else f"Quality {len(variants)+1}")
        variants.append({
            "url": variant_parsed.geturl(),
            "width": width,
            "height": height,
            "bandwidth": bandwidth,
            "label": label,
        })
        pending_attrs = None
        if len(variants) > MAX_HLS_VARIANTS:
            raise HTTPException(status_code=413, detail=f"HLS master playlist exceeds the {MAX_HLS_VARIANTS}-variant limit.")

    if pending_attrs is not None:
        raise HTTPException(status_code=415, detail="HLS master playlist is missing a variant URL.")
    if not variants:
        raise HTTPException(status_code=415, detail="This HLS playlist contains no supported quality variants.")
    variants.sort(key=lambda item: (item["height"], item["bandwidth"]), reverse=True)
    return variants


async def get_hls_playlist_text(url: str, host: str):
    validate_direct_host(host)
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout, trust_env=False, follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*"},
    ) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    raise HTTPException(status_code=400, detail="Redirected HLS playlists are not supported.")
                if response.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"HLS host returned HTTP {response.status_code}.")
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_HLS_PLAYLIST_BYTES:
                        raise HTTPException(status_code=413, detail="HLS playlist is too large.")
                    chunks.append(chunk)
                body = b"".join(chunks)
        except HTTPException:
            raise
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Could not retrieve the HLS playlist.")
    return body.decode("utf-8-sig", errors="replace")


async def get_hls_quality_options(url: str, host: str):
    """Return validated quality variants for a master playlist."""
    master_text = await get_hls_playlist_text(url, host)
    variants = parse_hls_master_playlist(master_text, url, host)
    options = []
    for index, variant in enumerate(variants):
        variant_host = urlparse(variant["url"]).hostname or ""
        media_text = await get_hls_playlist_text(variant["url"], variant_host)
        segment_urls, duration = parse_hls_media_playlist(media_text, variant["url"], variant_host)
        options.append({
            "index": index,
            "label": variant["label"],
            "width": variant["width"],
            "height": variant["height"],
            "bandwidth": variant["bandwidth"],
            "segment_count": len(segment_urls),
            "duration": duration,
            "url": variant["url"],
        })
    return options


async def get_hls_playlist(url: str, host: str):
    playlist_text = await get_hls_playlist_text(url, host)
    segments, duration = parse_hls_media_playlist(playlist_text, url, host)
    return segments, duration


async def download_hls_as_ts(url: str, host: str, quality_index: int = 0):
    """Fetch validated VOD TS segments and concatenate them into a .ts file."""
    playlist_text = await get_hls_playlist_text(url, host)
    if "#EXT-X-STREAM-INF:" in playlist_text:
        variants = await get_hls_quality_options(url, host)
        if quality_index < 0 or quality_index >= len(variants):
            raise HTTPException(status_code=400, detail="Selected HLS quality is not available.")
        selected_url = variants[quality_index]["url"]
        selected_host = urlparse(selected_url).hostname or ""
        segments, duration = await get_hls_playlist(selected_url, selected_host)
        url = selected_url
        host = selected_host
    else:
        if quality_index not in (0,):
            raise HTTPException(status_code=400, detail="This HLS playlist has only one quality.")
        segments, duration = parse_hls_media_playlist(playlist_text, url, host)
    temp = tempfile.NamedTemporaryFile(prefix="ilu-hd-", suffix=".ts", delete=False)
    temp_path = temp.name
    total = 0
    timeout = httpx.Timeout(30.0, connect=10.0)

    try:
        with temp:
            async with httpx.AsyncClient(
                timeout=timeout, trust_env=False, follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                for segment_url in segments:
                    _, segment_host = parse_https_url(segment_url)
                    # Re-check DNS for each segment to avoid trusting stale or changed DNS.
                    validate_public_host(segment_host)
                    try:
                        async with client.stream("GET", segment_url) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                raise HTTPException(status_code=400, detail="Redirected HLS segments are not supported.")
                            if response.status_code != 200:
                                raise HTTPException(status_code=502, detail=f"HLS segment host returned HTTP {response.status_code}.")
                            mime = video_content_type(response)
                            if mime not in {"video/mp2t", "application/octet-stream"}:
                                raise HTTPException(status_code=415, detail="Only MPEG-TS HLS segments are supported.")
                            async for chunk in response.aiter_bytes(64 * 1024):
                                total += len(chunk)
                                if total > MAX_BYTES:
                                    raise HTTPException(status_code=413, detail="HLS download exceeds the configured download limit.")
                                temp.write(chunk)
                    except HTTPException:
                        raise
                    except httpx.HTTPError:
                        raise HTTPException(status_code=502, detail="Could not download an HLS media segment.")
        if total == 0:
            raise HTTPException(status_code=502, detail="The HLS playlist returned an empty video.")
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    filename = safe_filename(urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".ts")
    return FileResponse(
        temp_path,
        media_type="video/mp2t",
        filename=filename,
        background=BackgroundTask(os.unlink, temp_path),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    ), duration, len(segments)


# -------------------------------------------------------------------
# DASH SUPPORT (STATIC, UNENCRYPTED, MUXED MP4; SEGMENTLIST + TEMPLATE)
# -------------------------------------------------------------------

MAX_DASH_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_DASH_SEGMENTS = 500
MAX_DASH_REPRESENTATIONS = 12


def is_dash_url(parsed) -> bool:
    return (parsed.path or "").lower().endswith(".mpd")


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_child(element, name: str):
    for child in list(element):
        if _xml_local(child.tag) == name:
            return child
    return None


def _xml_children(element, name: str):
    return [child for child in list(element) if _xml_local(child.tag) == name]


def _dash_base(parent_url: str, element, expected_host: str) -> str:
    base = _xml_child(element, "BaseURL")
    if base is None or not (base.text or "").strip():
        return parent_url
    absolute = urljoin(parent_url, (base.text or "").strip())
    parsed, host = parse_https_url(absolute)
    if host != expected_host:
        raise HTTPException(status_code=403, detail="DASH BaseURL must use the same trusted hostname as the MPD manifest.")
    return parsed.geturl()


def _dash_iso_duration(value: str | None):
    if not value:
        return None
    match = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if not match:
        return None
    days, hours, minutes, seconds = (float(x or 0) for x in match.groups())
    return round(days * 86400 + hours * 3600 + minutes * 60 + seconds, 3)


def _dash_same_host_url(base_url: str, relative: str, expected_host: str, label: str) -> str:
    absolute = urljoin(base_url, relative)
    parsed, part_host = parse_https_url(absolute)
    if part_host != expected_host:
        raise HTTPException(status_code=403, detail=f"DASH {label} must use the same trusted hostname as the MPD.")
    return parsed.geturl()


def _dash_inherited_template(root, period, adaptation, representation):
    """DASH SegmentTemplate attributes inherit MPD -> Period -> AdaptationSet -> Representation."""
    merged = {}
    timeline = None
    for element in (root, period, adaptation, representation):
        node = _xml_child(element, "SegmentTemplate")
        if node is not None:
            merged.update(node.attrib)
            candidate = _xml_child(node, "SegmentTimeline")
            if candidate is not None:
                timeline = candidate
    return merged, timeline


def _dash_template_expand(template: str, rep_id: str, bandwidth: int, number: int, time_value: int | None = None) -> str:
    """Expand the safe DASH template tokens; reject unsupported tokens rather than guessing."""
    marker = "\x00DOLLAR\x00"
    value = template.replace("$$", marker)
    token_re = re.compile(r"\$(RepresentationID|Bandwidth|Number|Time)(%0([1-9][0-9]?)d)?\$")

    def sub(match):
        key, _fmt, width = match.groups()
        if key == "RepresentationID":
            raw = rep_id
        elif key == "Bandwidth":
            raw = str(bandwidth)
        elif key == "Number":
            raw = str(number)
        else:
            if time_value is None:
                raise HTTPException(status_code=415, detail="DASH $Time$ token requires a SegmentTimeline.")
            raw = str(time_value)
        if width:
            return raw.zfill(int(width))
        return raw

    value = token_re.sub(sub, value)
    if "$" in value:
        raise HTTPException(status_code=415, detail="This DASH template contains an unsupported token.")
    return value.replace(marker, "$")


def _dash_template_segments(attrs, timeline, rep_id, bandwidth, period_duration):
    media_template = attrs.get("media")
    init_template = attrs.get("initialization")
    if not media_template or not init_template:
        raise HTTPException(status_code=415, detail="DASH SegmentTemplate must declare both initialization and media templates.")
    try:
        timescale = int(attrs.get("timescale", "1"))
        start_number = int(attrs.get("startNumber", "1"))
        if timescale <= 0 or start_number < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=415, detail="DASH SegmentTemplate has invalid timescale or startNumber.")

    entries = []
    if timeline is not None:
        current_number = start_number
        current_time = 0
        s_nodes = _xml_children(timeline, "S")
        if not s_nodes:
            raise HTTPException(status_code=415, detail="DASH SegmentTimeline is empty.")
        for idx, node in enumerate(s_nodes):
            try:
                duration = int(node.attrib["d"])
                explicit_t = int(node.attrib.get("t", current_time))
                repeat = int(node.attrib.get("r", "0"))
            except (KeyError, ValueError):
                raise HTTPException(status_code=415, detail="DASH SegmentTimeline contains an invalid S entry.")
            if duration <= 0 or repeat < -1:
                raise HTTPException(status_code=415, detail="DASH SegmentTimeline has an invalid duration or repeat count.")
            if repeat == -1:
                next_t = None
                for later in s_nodes[idx + 1:]:
                    if "t" in later.attrib:
                        try:
                            next_t = int(later.attrib["t"])
                        except ValueError:
                            raise HTTPException(status_code=415, detail="DASH SegmentTimeline has an invalid next timestamp.")
                        break
                if next_t is None:
                    if period_duration is None:
                        raise HTTPException(status_code=415, detail="DASH SegmentTimeline uses r=-1 without a known VOD duration.")
                    next_t = int(period_duration * timescale)
                repeat = max(0, (next_t - explicit_t + duration - 1) // duration - 1)
            if repeat > MAX_DASH_SEGMENTS:
                raise HTTPException(status_code=413, detail="DASH SegmentTimeline repeat exceeds the segment limit.")
            for offset in range(repeat + 1):
                entries.append((current_number, explicit_t + offset * duration))
                current_number += 1
                if len(entries) > MAX_DASH_SEGMENTS:
                    raise HTTPException(status_code=413, detail=f"DASH representation exceeds the {MAX_DASH_SEGMENTS}-segment limit.")
            current_time = explicit_t + (repeat + 1) * duration
    else:
        try:
            duration_units = int(attrs["duration"])
        except (KeyError, ValueError):
            raise HTTPException(status_code=415, detail="DASH SegmentTemplate needs either SegmentTimeline or duration.")
        if duration_units <= 0 or period_duration is None:
            raise HTTPException(status_code=415, detail="Duration-based DASH SegmentTemplate requires a positive duration and known static VOD duration.")
        count = int((period_duration * timescale + duration_units - 1) // duration_units)
        if count < 1 or count > MAX_DASH_SEGMENTS:
            raise HTTPException(status_code=413, detail=f"DASH representation exceeds the {MAX_DASH_SEGMENTS}-segment limit.")
        entries = [(start_number + i, i * duration_units) for i in range(count)]

    init_name = _dash_template_expand(init_template, rep_id, bandwidth, start_number, None)
    segment_names = [
        _dash_template_expand(media_template, rep_id, bandwidth, number, time_value)
        for number, time_value in entries
    ]
    return init_name, segment_names, len(segment_names)


def _dash_parse_track(root, period, adaptation, rep, period_base, mpd_host, duration):
    """Resolve one DASH representation to ordered init/media URLs."""
    adaptation_mime = adaptation.attrib.get("mimeType", "").lower()
    adaptation_codecs = adaptation.attrib.get("codecs", "").lower()
    mime = rep.attrib.get("mimeType", adaptation_mime).lower()
    codecs = rep.attrib.get("codecs", adaptation_codecs).lower()
    adaptation_type = adaptation.attrib.get("contentType", "").lower()
    codec_tokens = ("avc", "hvc", "hev", "av01", "vp09", "mp4v")
    audio_tokens = ("mp4a", "ac-3", "ec-3", "opus", "vorbis", "ac-4")
    has_video = adaptation_type == "video" or mime.startswith("video/") or any(x in codecs for x in codec_tokens)
    has_audio = adaptation_type == "audio" or mime.startswith("audio/") or any(x in codecs for x in audio_tokens)
    if not (has_video or has_audio):
        return None

    rep_base = _dash_base(period_base, adaptation, mpd_host)
    rep_base = _dash_base(rep_base, rep, mpd_host)
    init_url = None
    segment_urls = []
    seglist = _xml_child(rep, "SegmentList") or _xml_child(adaptation, "SegmentList")
    if seglist is not None:
        initialization = _xml_child(seglist, "Initialization")
        if initialization is None or not initialization.attrib.get("sourceURL"):
            return None
        init_url = _dash_same_host_url(rep_base, initialization.attrib["sourceURL"], mpd_host, "initialization segment")
        for segment in _xml_children(seglist, "SegmentURL"):
            media = segment.attrib.get("media")
            if not media:
                raise HTTPException(status_code=415, detail="DASH SegmentURL is missing its media attribute.")
            segment_urls.append(_dash_same_host_url(rep_base, media, mpd_host, "media segments"))
            if len(segment_urls) > MAX_DASH_SEGMENTS:
                raise HTTPException(status_code=413, detail=f"DASH representation exceeds the {MAX_DASH_SEGMENTS}-segment limit.")
    else:
        attrs, timeline = _dash_inherited_template(root, period, adaptation, rep)
        if not attrs:
            return None
        rep_id = rep.attrib.get("id", "")
        try:
            bandwidth = int(rep.attrib.get("bandwidth", "0") or 0)
        except ValueError:
            raise HTTPException(status_code=415, detail="DASH Representation has an invalid bandwidth value.")
        init_name, media_names, _count = _dash_template_segments(attrs, timeline, rep_id, bandwidth, duration)
        init_url = _dash_same_host_url(rep_base, init_name, mpd_host, "initialization segment")
        segment_urls = [_dash_same_host_url(rep_base, name, mpd_host, "media segments") for name in media_names]

    if not init_url or not segment_urls:
        return None
    try:
        height = int(rep.attrib.get("height", "0") or 0)
        bandwidth = int(rep.attrib.get("bandwidth", "0") or 0)
    except ValueError:
        raise HTTPException(status_code=415, detail="DASH Representation has invalid height or bandwidth metadata.")
    return {
        "id": rep.attrib.get("id", ""),
        "codecs": codecs,
        "has_video": bool(has_video),
        "has_audio": bool(has_audio),
        "height": height,
        "bandwidth": bandwidth,
        "init_url": init_url,
        "segments": segment_urls,
        "duration": duration,
        "segment_count": len(segment_urls),
    }


def parse_dash_mpd(text: str, mpd_url: str, mpd_host: str):
    """Parse static, clear DASH VOD using SegmentList or SegmentTemplate/Timeline.

    Supports muxed representations and separate video/audio AdaptationSets.
    Separate tracks are muxed into MP4 at download time using the bundled ffmpeg
    executable from imageio-ffmpeg. Live, DRM, cross-host and unbounded content
    remains unsupported.
    """
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise HTTPException(status_code=415, detail="DASH XML declarations with entities are not supported.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise HTTPException(status_code=415, detail="The DASH MPD manifest is malformed.")
    if _xml_local(root.tag) != "MPD":
        raise HTTPException(status_code=415, detail="This is not a valid DASH MPD manifest.")
    if root.attrib.get("type", "static").lower() != "static":
        raise HTTPException(status_code=415, detail="Live or dynamic DASH manifests are not supported.")
    if any(_xml_local(node.tag) == "ContentProtection" for node in root.iter()):
        raise HTTPException(status_code=415, detail="Encrypted or DRM-protected DASH streams are not supported.")
    periods = _xml_children(root, "Period")
    if len(periods) != 1:
        raise HTTPException(status_code=415, detail="DASH manifests must contain exactly one Period.")
    period = periods[0]
    period_base = _dash_base(mpd_url, root, mpd_host)
    period_base = _dash_base(period_base, period, mpd_host)
    mpd_duration = _dash_iso_duration(root.attrib.get("mediaPresentationDuration") or period.attrib.get("duration"))

    video_tracks, audio_tracks, muxed_tracks = [], [], []
    for adaptation in _xml_children(period, "AdaptationSet"):
        adaptation_base = _dash_base(period_base, adaptation, mpd_host)
        for rep in _xml_children(adaptation, "Representation"):
            track = _dash_parse_track(root, period, adaptation, rep, period_base, mpd_host, mpd_duration)
            if not track:
                continue
            if track["has_video"] and track["has_audio"]:
                muxed_tracks.append(track)
            elif track["has_video"]:
                video_tracks.append(track)
            elif track["has_audio"]:
                audio_tracks.append(track)

    # Prefer an existing muxed representation. Otherwise pair each video
    # representation with the best declared audio representation.
    candidates = list(muxed_tracks)
    if video_tracks:
        if audio_tracks:
            audio_tracks.sort(key=lambda item: item["bandwidth"], reverse=True)
            chosen_audio = audio_tracks[0]
            for video in video_tracks:
                paired = dict(video)
                paired["audio"] = {
                    "init_url": chosen_audio["init_url"],
                    "segments": chosen_audio["segments"],
                    "segment_count": chosen_audio["segment_count"],
                    "bandwidth": chosen_audio["bandwidth"],
                }
                candidates.append(paired)
        else:
            # Do not silently create a muted output from video-only DASH.
            if not muxed_tracks:
                raise HTTPException(status_code=415, detail="This DASH MPD has separate video but no usable audio track.")

    reps = []
    for item in candidates:
        height = item["height"]
        bandwidth = item["bandwidth"]
        label = f"{height}p" if height else (f"{round(bandwidth / 1000)} kbps" if bandwidth else f"Quality {len(reps) + 1}")
        reps.append({
            "label": label, "height": height, "bandwidth": bandwidth,
            "init_url": item["init_url"], "segments": item["segments"],
            "audio": item.get("audio"),
            "duration": item["duration"], "segment_count": item["segment_count"],
        })
        if len(reps) > MAX_DASH_REPRESENTATIONS:
            raise HTTPException(status_code=413, detail=f"DASH MPD exceeds the {MAX_DASH_REPRESENTATIONS}-representation limit.")

    if not reps:
        raise HTTPException(status_code=415, detail=(
            "No supported DASH representation found. ILU HD requires static, unencrypted MP4 SegmentList or SegmentTemplate/Timeline VOD with muxed tracks or separate video/audio tracks."
        ))
    reps.sort(key=lambda item: (item["height"], item["bandwidth"]), reverse=True)
    for index, item in enumerate(reps):
        item["index"] = index
    return reps


async def get_dash_manifest_text(url: str, host: str):
    validate_direct_host(host)
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout, trust_env=False, follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/dash+xml,application/xml,text/xml,*/*"},
    ) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    raise HTTPException(status_code=400, detail="Redirected DASH manifests are not supported.")
                if response.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"DASH host returned HTTP {response.status_code}.")
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DASH_MANIFEST_BYTES:
                        raise HTTPException(status_code=413, detail="DASH MPD manifest is too large.")
                    chunks.append(chunk)
                body = b"".join(chunks)
        except HTTPException:
            raise
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Could not retrieve the DASH MPD manifest.")
    return body.decode("utf-8-sig", errors="replace")


async def get_dash_quality_options(url: str, host: str):
    text = await get_dash_manifest_text(url, host)
    return parse_dash_mpd(text, url, host)


async def _download_dash_track(client, track, host, output_path, total_counter):
    urls = [track["init_url"], *track["segments"]]
    with open(output_path, "wb") as output:
        for part_url in urls:
            _, part_host = parse_https_url(part_url)
            if part_host != host:
                raise HTTPException(status_code=403, detail="DASH parts must remain on the manifest hostname.")
            validate_public_host(part_host)
            try:
                async with client.stream("GET", part_url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        raise HTTPException(status_code=400, detail="Redirected DASH media parts are not supported.")
                    if response.status_code != 200:
                        raise HTTPException(status_code=502, detail=f"DASH media host returned HTTP {response.status_code}.")
                    mime = video_content_type(response)
                    if mime not in {"video/mp4", "audio/mp4", "application/mp4", "video/iso.segment", "audio/iso.segment", "application/octet-stream", "binary/octet-stream"}:
                        raise HTTPException(status_code=415, detail="Only fragmented MP4 DASH media parts are supported.")
                    async for chunk in response.aiter_bytes(64 * 1024):
                        total_counter[0] += len(chunk)
                        if total_counter[0] > MAX_BYTES:
                            raise HTTPException(status_code=413, detail="DASH download exceeds the configured download limit.")
                        output.write(chunk)
            except HTTPException:
                raise
            except httpx.HTTPError:
                raise HTTPException(status_code=502, detail="Could not download a DASH media part.")


async def download_dash_as_mp4(url: str, host: str, quality_index: int = 0):
    reps = await get_dash_quality_options(url, host)
    if quality_index < 0 or quality_index >= len(reps):
        raise HTTPException(status_code=400, detail="Selected DASH quality is not available.")
    selected = reps[quality_index]
    work_dir = tempfile.mkdtemp(prefix="ilu-hd-dash-")
    output_path = os.path.join(work_dir, "output.mp4")
    video_track_path = os.path.join(work_dir, "video-track.mp4")
    audio_track_path = os.path.join(work_dir, "audio-track.mp4")
    total_counter = [0]
    timeout = httpx.Timeout(45.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
            video_track = {"init_url": selected["init_url"], "segments": selected["segments"]}
            if selected.get("audio"):
                await _download_dash_track(client, video_track, host, video_track_path, total_counter)
                await _download_dash_track(client, selected["audio"], host, audio_track_path, total_counter)
            else:
                await _download_dash_track(client, video_track, host, output_path, total_counter)

        if selected.get("audio"):
            try:
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                raise HTTPException(status_code=503, detail="Separate DASH audio/video muxing is unavailable. Ensure imageio-ffmpeg is installed in backend requirements.")
            command = [
                ffmpeg_exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", video_track_path, "-i", audio_track_path,
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                "-movflags", "+faststart", output_path,
            ]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
            except subprocess.TimeoutExpired:
                raise HTTPException(status_code=504, detail="DASH audio/video muxing took too long.")
            if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                detail = (result.stderr or "").strip()[-700:]
                raise HTTPException(status_code=415, detail="Could not mux the DASH audio and video tracks into MP4." + (" " + detail if detail else ""))

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise HTTPException(status_code=502, detail="The DASH MPD returned an empty video.")
        if os.path.getsize(output_path) > MAX_BYTES:
            raise HTTPException(status_code=413, detail="DASH output exceeds the configured download limit.")
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    filename = safe_filename(urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".mp4")
    return FileResponse(output_path, media_type="video/mp4", filename=filename,
        background=BackgroundTask(shutil.rmtree, work_dir, True),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}), selected["duration"], selected["segment_count"]


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
        "public_og_video_lookup": True,
        "direct_video_mime_types": ["video/mp4", "video/webm", "video/quicktime", "video/x-matroska", "application/octet-stream"],
        "head_fallback_enabled": True,
        "hls_dash_playlist_download": True,
        "hls_playlist_download": True,
        "dash_playlist_download": True,
        "dash_support": "static_unencrypted_mp4_segmentlist_segmenttemplate_timeline_same_host_only_muxed_or_separate_tracks",
        "dash_separate_audio_video_support": True,
        "dash_segmentlist_support": True,
        "dash_segmenttemplate_support": True,
        "dash_segmenttimeline_support": True,
        "dash_quality_selection": True,
        "hls_support": "unencrypted_vod_mpegts_same_host_only",
        "hls_quality_selection": True,
        "hls_master_playlist_support": True,
        "tiktok_download_supported": False,
    }


@app.get("/inspect")
async def inspect(
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

    if platform in {"Instagram", "Facebook"}:
        try:
            if platform == "Instagram":
                await get_instagram_media(parsed)
            else:
                await get_facebook_video(parsed)
            return {
                "platform": platform,
                "host": host,
                "download_available": True,
                "integration_required": False,
                "provider": platform.lower(),
                "message": "Public video metadata exposed a direct media file. No account connection was used.",
            }
        except HTTPException as exc:
            return {
                "platform": platform,
                "host": host,
                "download_available": False,
                "integration_required": False,
                "provider": None,
                "message": str(exc.detail),
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

    if is_dash_url(parsed):
        variants = await get_dash_quality_options(url, host)
        highest = variants[0]
        return {
            "provider": "dash",
            "host": host,
            "filename": safe_filename(parsed.path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".mp4"),
            "content_type": "video/mp4",
            "size_bytes": None,
            "size_mb": None,
            "format": "DASH (fragmented MP4)",
            "duration": highest["duration"],
            "thumbnail": None,
            "quality": f"{highest['label']} · {highest['segment_count']} segments",
            "variants": [{k: v for k, v in item.items() if k not in {"init_url", "segments", "audio"}} for item in variants],
            "note": "Static, unencrypted DASH VOD. SegmentList and SegmentTemplate/Timeline are supported. Separate audio/video tracks are muxed into MP4; output is an MP4 file.",
        }

    if is_hls_url(parsed):
        playlist_text = await get_hls_playlist_text(url, host)
        if "#EXT-X-STREAM-INF:" in playlist_text:
            variants = await get_hls_quality_options(url, host)
            highest = variants[0]
            base_name = safe_filename(parsed.path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".ts")
            return {
                "provider": "hls",
                "host": host,
                "filename": base_name,
                "content_type": "video/mp2t",
                "size_bytes": None,
                "size_mb": None,
                "format": "HLS (MPEG-TS)",
                "duration": highest["duration"],
                "thumbnail": None,
                "quality": f"{highest['label']} · {highest['segment_count']} segments",
                "variants": [{k: v for k, v in item.items() if k != "url"} for item in variants],
                "note": "Multi-quality unencrypted HLS VOD. Choose a quality before downloading; output is a .ts video file.",
            }
        segments, duration = parse_hls_media_playlist(playlist_text, url, host)
        base_name = safe_filename(parsed.path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".ts")
        return {
            "provider": "hls",
            "host": host,
            "filename": base_name,
            "content_type": "video/mp2t",
            "size_bytes": None,
            "size_mb": None,
            "format": "HLS (MPEG-TS)",
            "duration": duration,
            "thumbnail": None,
            "quality": f"{len(segments)} segments · single quality",
            "variants": [],
            "note": "Unencrypted completed HLS VOD playlist. Segments will be combined into a .ts video file.",
        }

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

    if platform in {"Instagram", "Facebook"} and not is_meta_media_cdn(host):
        item = await (get_instagram_media(parsed) if platform == "Instagram" else get_facebook_video(parsed))
        return {
            "provider": platform.lower(),
            "host": host,
            "filename": safe_filename((item.get("title") or platform.lower() + "-video") + ".mp4"),
            "content_type": "video/mp4",
            "size_bytes": None,
            "size_mb": None,
            "format": "MP4",
            "duration": None,
            "thumbnail": None,
            "quality": "Public Open Graph media",
            "note": "A public page exposed a direct media URL. No account, cookies or login were used; availability may change before download.",
        }

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
            response = await probe_video_url(client, url)

        except HTTPException:
            raise

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

    content_type = video_content_type(response)

    if not is_supported_video_response(response):
        await response.aclose()
        raise HTTPException(
            status_code=415,
            detail=(
                "This URL is not a direct video file. MP4, WebM and other "
                "video/* files are supported; M3U8/DASH playlists are not "
                "single downloadable files."
            ),
        )

    raw_size = response.headers.get("content-length")
    if response.status_code == 206:
        content_range = response.headers.get("content-range", "")
        range_match = re.search(r"/([0-9]+)$", content_range)
        if range_match:
            raw_size = range_match.group(1)
    await response.aclose()
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
    quality_index: int = Query(0, ge=0, le=11),
):
    parsed, host = parse_https_url(url)
    platform = identify_platform(host)

    if is_dash_url(parsed):
        response, _duration, _segment_count = await download_dash_as_mp4(url, host, quality_index)
        return response

    if is_hls_url(parsed):
        response, _duration, _segment_count = await download_hls_as_ts(url, host, quality_index)
        return response

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

    if platform in {"Instagram", "Facebook"} and not is_meta_media_cdn(host):
        item = await (get_instagram_media(parsed) if platform == "Instagram" else get_facebook_video(parsed))
        url = item["media_url"]
        parsed, host = parse_https_url(url)
        platform = identify_platform(host)

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
