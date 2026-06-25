import os
import re
import json
import asyncio
import tempfile
import subprocess
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TimedOut, NetworkError

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "10000"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdlp_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

_RENDER_COOKIES = Path("/etc/secrets/youtube_cookies.txt")
_RUNTIME_COOKIES = DOWNLOAD_DIR / "youtube_cookies.txt"

MAX_FILESIZE_MB = 500
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50MB strict limit

INSTAGRAM_POST_DOC_ID  = os.environ.get("INSTAGRAM_POST_DOC_ID", "").strip()
INSTAGRAM_STORY_DOC_ID = os.environ.get("INSTAGRAM_STORY_DOC_ID", "").strip()
INSTAGRAM_APP_ID       = os.environ.get("INSTAGRAM_APP_ID", "936619743392459").strip()

KNOWN_SITES: dict[str, str] = {
    "tiktok.com":  "TikTok",
    "redgifs.com": "RedGifs",
    "instagram.com": "Instagram",
}

ALLOWED_DOMAINS = set(KNOWN_SITES.keys())
IMAGE_CAPABLE_SITES = {"tiktok.com", "instagram.com"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

QUALITY_PRESETS = [
    ("4K",    2160, "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"),
    ("2K",    1440, "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best"),
    ("1080p", 1080, "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"),
    ("720p",   720, "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best"),
    ("480p",   480, "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best"),
    ("360p",   360, "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best"),
]

# ── Cookie helpers ─────────────────────────────────────────────────────────────

def get_cookies_path() -> Path | None:
    source = None
    if _RENDER_COOKIES.exists():
        source = _RENDER_COOKIES
    elif _RUNTIME_COOKIES.exists():
        source = _RUNTIME_COOKIES
    if source:
        writable_copy = DOWNLOAD_DIR / "active_cookies.txt"
        try:
            writable_copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            return writable_copy
        except Exception as e:
            print(f"⚠️ Cookie copy failure: {e}")
            return source
    return None

# ── Health-check & File Server ────────────────────────────────────────────────

class CombinedServerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/download/"):
            try:
                filename = urllib.parse.unquote(self.path.split("/download/", 1)[1])
                target_file = (DOWNLOAD_DIR / filename).resolve()
                if target_file.exists() and target_file.is_file() and DOWNLOAD_DIR in target_file.parents:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(target_file.stat().st_size))
                    self.send_header("Content-Disposition", f'attachment; filename="{target_file.name}"')
                    self.end_headers()
                    with open(target_file, "rb") as f:
                        self.wfile.write(f.read())
                    return
            except Exception:
                pass
            self.send_error(404, "File Not Found")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass

def run_health_server():
    HTTPServer(("0.0.0.0", PORT), CombinedServerHandler).serve_forever()

# ── Extraction helpers ─────────────────────────────────────────────────────────

def is_allowed_site(url: str) -> bool:
    return any(d in url.lower() for d in ALLOWED_DOMAINS)

def is_image_capable(url: str) -> bool:
    return any(d in url.lower() for d in IMAGE_CAPABLE_SITES)

def detect_site(url: str) -> str:
    for domain, name in KNOWN_SITES.items():
        if domain in url.lower():
            return name
    return "Unknown site"

def extract_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s]+", text)
    return m.group(0) if m else None

def base_args(url: str) -> list[str]:
    args = [
        "--no-warnings",
        "--rm-cache-dir",
        "--no-cache-dir",
    ]

    is_youtube = "youtube.com" in url.lower() or "youtu.be" in url.lower()

    if is_youtube:
        # Use tv_embedded client: works without PO tokens, no Node.js required,
        # and is the most reliable player client for Render/server environments.
        # Fallback chain: tv_embedded → android → web (web_creator as last resort).
        args += [
            "--extractor-args",
            "youtube:player_client=tv_embedded,android,web_creator",
        ]
        # Cookie handling: prefer writable copy so yt-dlp can update the jar
        yt_cookies_path = DOWNLOAD_DIR / "youtube_cookies.txt"
        if not yt_cookies_path.exists() and _RENDER_COOKIES.exists():
            try:
                yt_cookies_path.write_text(
                    _RENDER_COOKIES.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except Exception:
                yt_cookies_path = _RENDER_COOKIES
        if yt_cookies_path.exists():
            args += ["--cookies", str(yt_cookies_path)]

    elif "tiktok.com" in url.lower():
        args += [
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
    elif "redgifs.com" in url.lower():
        args += [
            "--user-agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        ]

    args += ["--socket-timeout", "15"]

    if not is_youtube:
        cookies = get_cookies_path()
        if cookies:
            args += ["--cookies", str(cookies)]

    return args

def run_ytdlp(args: list[str]) -> tuple[str, str, int]:
    result = subprocess.run(
        ["yt-dlp"] + args,
        capture_output=True, text=True, timeout=600,
    )
    return result.stdout, result.stderr, result.returncode

def run_gallerydl(url: str, out_dir: Path) -> tuple[str, str, int]:
    result = subprocess.run(
        ["gallery-dl", "--dest", str(out_dir), url],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout, result.stderr, result.returncode

def clean_errors(stderr: str) -> str:
    errors = [l for l in stderr.splitlines() if "ERROR" in l]
    return "\n".join(errors) if errors else stderr.strip()

def is_tiktok_photo_url(url: str) -> bool:
    return "tiktok.com" in url.lower() and "/photo/" in url.lower()

def fetch_instagram_public(url: str, url_key: str) -> list[Path] | None:
    """
    Download a public Instagram post (photo/reel/carousel).
    - Images/carousels: gallery-dl with cookies
    - Reels/videos: yt-dlp with cookies
    """
    cookies = get_cookies_path()

    def collected() -> list[Path]:
        return [p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
                if not p.name.endswith((".part", ".ytdl"))]

    # ── Attempt 1: gallery-dl (handles image posts and carousels) ─────────
    sub = DOWNLOAD_DIR / url_key
    sub.mkdir(exist_ok=True)
    gdl_args = ["gallery-dl", "--dest", str(sub)]
    if cookies:
        gdl_args += ["-C", str(cookies)]
    gdl_args.append(url)
    subprocess.run(gdl_args, capture_output=True, text=True, timeout=120)
    gdl_files = sorted([p for p in sub.rglob("*")
                        if p.is_file() and p.suffix.lower() in IMAGE_EXTS])
    if gdl_files:
        moved = []
        for i, fp in enumerate(gdl_files):
            dest = DOWNLOAD_DIR / f"{url_key}_{i:03d}{fp.suffix}"
            fp.rename(dest)
            moved.append(dest)
        try: sub.rmdir()
        except Exception: pass
        return moved

    # ── Attempt 2: yt-dlp with cookies (reels / video posts) ─────────────
    if cookies:
        ig_args = [
            "--no-warnings", "--rm-cache-dir", "--no-cache-dir",
            "--user-agent",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1",
            "--add-header", f"X-Ig-App-Id:{INSTAGRAM_APP_ID}",
            "--cookies", str(cookies),
            "--socket-timeout", "20",
            "--no-playlist",
        ]
        out_vid = str(DOWNLOAD_DIR / f"{url_key}_%(title).60s.%(ext)s")
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        run_ytdlp(ig_args + [
            "-f", fmt, "--merge-output-format", "mp4",
            "--format-sort", "ext:mp4:m4a",
            "-o", out_vid, url,
        ])
        files = collected()
        if files:
            return files

    return None


def is_instagram_story_url(url: str) -> bool:
    """Return True for any Instagram Stories or Highlights URL."""
    low = url.lower()
    return "instagram.com" in low and (
        "/stories/" in low or
        "/s/" in low or
        "highlight:" in low
    )


def extract_instagram_story_target(url: str) -> tuple[str | None, str | None]:
    """Return (username, story_pk_or_none) from a stories URL."""
    m = re.search(r"instagram\.com/stories/([^/?#]+)/?([^/?#]+)?", url)
    if not m:
        return None, None
    username = m.group(1)
    story_pk = m.group(2) if m.group(2) and m.group(2).isdigit() else None
    return username, story_pk


def _build_ig_headers(cookies_path: "Path | None" = None) -> dict:
    """Build Instagram request headers, injecting cookies from a Netscape jar."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "X-Ig-App-Id": INSTAGRAM_APP_ID,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    }
    if cookies_path and cookies_path.exists():
        try:
            parts = []
            for line in cookies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) >= 7:
                    parts.append(f"{cols[5]}={cols[6]}")
            if parts:
                headers["Cookie"] = "; ".join(parts)
        except Exception:
            pass
    return headers


def _resolve_instagram_user_id(username: str, cookies_path: "Path | None" = None) -> "str | None":
    """Resolve a numeric user_id for the given username via the web profile API."""
    headers = _build_ig_headers(cookies_path)
    req = urllib.request.Request(
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={urllib.parse.quote(username)}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        uid = payload.get("data", {}).get("user", {}).get("id")
        return str(uid) if uid else None
    except Exception:
        return None


def _scrape_post_doc_id(shortcode: str) -> "str | None":
    """
    Fetch the post page HTML and grep for a doc_id.
    Returns the scraped value or None on failure.
    """
    try:
        req = urllib.request.Request(
            f"https://www.instagram.com/p/{shortcode}/",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m = re.search(r'"doc_id"\s*:\s*"?(\d{10,})"?', html)
        return m.group(1) if m else None
    except Exception:
        return None


def _scrape_story_doc_id(username: str) -> "str | None":
    """
    Fetch the stories page HTML and grep for a reels/story doc_id.
    Tries progressively looser patterns; returns None on failure.
    """
    try:
        req = urllib.request.Request(
            f"https://www.instagram.com/stories/{urllib.parse.quote(username)}/",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        for pattern in [
            r'reels_media.{0,200}"doc_id"\s*:\s*"?(\d{10,})"?',
            r'story.{0,200}"doc_id"\s*:\s*"?(\d{10,})"?',
            r'"doc_id"\s*:\s*"?(\d{10,})"?',
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                return m.group(1)
        return None
    except Exception:
        return None


def _execute_graphql(doc_id: str, variables: dict, cookies_path: "Path | None" = None) -> "dict | None":
    """
    Fire a GraphQL query against the Instagram endpoint and return the parsed JSON.
    Returns None on any network/parse error.
    """
    encoded = urllib.parse.quote(json.dumps(variables, separators=(",", ":")))
    url = f"https://www.instagram.com/graphql/query/?doc_id={doc_id}&variables={encoded}"
    req = urllib.request.Request(url, headers=_build_ig_headers(cookies_path))
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None


def fetch_instagram_post_graphql(url: str, url_key: str) -> "list[Path]":
    """
    Fetch a private/rate-limited post directly via GraphQL.

    doc_id resolution order:
      1. Scrape the post page for a live doc_id.
      2. Fall back to INSTAGRAM_POST_DOC_ID env var.

    Returns a list of downloaded Paths, or [] on failure.
    """
    match = re.search(r"instagram\.com/(?:p|reel|tv|share/v)/([^/?#&]+)", url)
    if not match:
        return []
    shortcode = match.group(1)

    post_doc_id = _scrape_post_doc_id(shortcode) or INSTAGRAM_POST_DOC_ID
    if not post_doc_id:
        return []

    cookies = get_cookies_path()
    variables = {
        "shortcode": shortcode,
        "fetch_tagged_user_count": None,
        "hoisted_comment_id": None,
        "hoisted_reply_id": None,
    }
    payload = _execute_graphql(post_doc_id, variables, cookies)
    if not payload:
        return []

    # Navigate the response to find media items
    data = payload.get("data", {})
    media = (
        data.get("xdt_api__v1__media__shortcode__web_info", {})
        or data.get("shortcode_media")
        or data
    )
    # Carousel
    items = media.get("edge_sidecar_to_children", {}).get("edges", [])
    if items:
        nodes = [e["node"] for e in items if "node" in e]
    else:
        nodes = [media]

    downloaded: list[Path] = []
    for idx, node in enumerate(nodes):
        is_video = node.get("is_video", False)
        if is_video:
            media_url = node.get("video_url")
            ext = ".mp4"
        else:
            media_url = node.get("display_url")
            ext = ".jpg"
        if not media_url:
            continue
        dest = DOWNLOAD_DIR / f"{url_key}_{idx:03d}{ext}"
        try:
            urllib.request.urlretrieve(media_url, dest)
            downloaded.append(dest)
        except Exception:
            continue

    return downloaded


def fetch_instagram_stories(url: str, url_key: str) -> list[Path]:
    """
    Private-story aware fetcher.

    Strategy:
      1. Resolve username -> user_id via Instagram profile API (needs cookies).
      2. Determine story doc_id: scrape page first, fall back to INSTAGRAM_STORY_DOC_ID env var.
      3. Execute the GraphQL reels_media query with authenticated headers.
      4. Parse video_versions / image_versions2 items and download directly.

    Returns [] on any failure so the caller shows manual instructions.
    """
    cookies = get_cookies_path()

    username, target_story_pk = extract_instagram_story_target(url)
    if not username:
        return []

    user_id = _resolve_instagram_user_id(username, cookies)
    if not user_id:
        return []

    story_doc_id = _scrape_story_doc_id(username) or INSTAGRAM_STORY_DOC_ID
    if not story_doc_id:
        return []

    variables = {
        "reel_ids": [user_id],
        "precomposed_overlay": False,
        "story_viewer_fetch_count": 50,
    }
    payload = _execute_graphql(story_doc_id, variables, cookies)
    if not payload:
        return []

    reels_media = (
        payload.get("data", {})
               .get("xdt_api__v1__feed__reels_media__connection", {})
               .get("reels_media")
        or payload.get("data", {}).get("reels_media")
        or []
    )
    if not reels_media:
        return []

    items: list[dict] = []
    for reel in reels_media:
        items.extend(reel.get("items") or [])

    if target_story_pk:
        items = [
            it for it in items
            if str(it.get("pk") or it.get("id") or "") == str(target_story_pk)
        ]

    downloaded: list[Path] = []
    for idx, item in enumerate(items):
        vid_candidates = item.get("video_versions") or []
        img_candidates = (
            item.get("image_versions2", {}).get("candidates", [])
            if isinstance(item.get("image_versions2"), dict) else []
        )
        media_url = (
            vid_candidates[0].get("url") if vid_candidates
            else (img_candidates[0].get("url") if img_candidates else None)
        )
        if not media_url:
            continue
        ext = ".mp4" if vid_candidates else ".jpg"
        dest = DOWNLOAD_DIR / f"{url_key}_{idx:03d}{ext}"
        try:
            urllib.request.urlretrieve(media_url, dest)
            downloaded.append(dest)
        except Exception:
            continue

    return downloaded


def get_instagram_graphql_instructions(url: str) -> tuple["str | None", bool]:
    """
    Build a GraphQL URL for manual browser paste (fallback when auto-fetch fails).

    doc_id resolution: scrape live first, then env var fallback.
    For stories also resolves user_id to build the reels_media query.
    """
    if is_instagram_story_url(url):
        username, _ = extract_instagram_story_target(url)
        if not username:
            return None, True
        cookies = get_cookies_path()
        user_id = _resolve_instagram_user_id(username, cookies)
        if not user_id:
            return None, True
        story_doc_id = _scrape_story_doc_id(username) or INSTAGRAM_STORY_DOC_ID
        if not story_doc_id:
            return None, True
        variables = {"reel_ids": [user_id], "precomposed_overlay": False, "story_viewer_fetch_count": 50}
        encoded = urllib.parse.quote(json.dumps(variables, separators=(",", ":")))
        return f"https://www.instagram.com/graphql/query/?doc_id={story_doc_id}&variables={encoded}", True

    match = re.search(r"instagram\.com/(?:p|reel|tv|share/v)/([^/?#&]+)", url)
    if not match:
        return None, False
    shortcode = match.group(1)

    post_doc_id = _scrape_post_doc_id(shortcode) or INSTAGRAM_POST_DOC_ID
    if not post_doc_id:
        return None, False

    variables = {
        "shortcode": shortcode,
        "fetch_tagged_user_count": None,
        "hoisted_comment_id": None,
        "hoisted_reply_id": None,
    }
    encoded = urllib.parse.quote(json.dumps(variables, separators=(",", ":")))
    return f"https://www.instagram.com/graphql/query/?doc_id={post_doc_id}&variables={encoded}", False


def parse_and_download_instagram(target_data: str, url_key: str, choice: str = "all", is_raw_json: bool = False, dynamic_target_idx: str = None) -> list[Path]:
    downloaded_paths = []
    if is_raw_json:
        try:
            payload = json.loads(target_data)

            def find_media_blocks(data):
                blocks = []
                if isinstance(data, dict):
                    if any(k in data for k in ["video_versions", "image_versions2", "video_url", "display_url"]):
                        if not any(k in data for k in ["shortcode_media", "xdt_api__v1__media__shortcode__web_info"]):
                            blocks.append(data)
                    if "carousel_media" in data and isinstance(data["carousel_media"], list):
                        for item in data["carousel_media"]:
                            blocks.extend(find_media_blocks(item))
                    for v in data.values():
                        blocks.extend(find_media_blocks(v))
                elif isinstance(data, list):
                    for item in data:
                        blocks.extend(find_media_blocks(item))
                return blocks

            media_items = find_media_blocks(payload)
            if not media_items:
                root = payload.get("data", {})
                web_info = root.get("xdt_api__v1__media__shortcode__web_info", {})
                media_items = web_info.get("items", []) or payload.get("items", []) or root.get("shortcode_media", [])
                if isinstance(media_items, dict):
                    media_items = [media_items]

            normalized_items = []
            for item in media_items:
                if isinstance(item, dict) and "node" in item:
                    item = item["node"]
                if isinstance(item, dict):
                    normalized_items.append(item)

            seen_urls = set()
            unique_items = []
            for item in normalized_items:
                v_url = item.get("video_url")
                if not v_url and "video_versions" in item and item["video_versions"]:
                    v_url = item["video_versions"][0].get("url")
                i_url = None
                if "image_versions2" in item and item["image_versions2"].get("candidates"):
                    i_url = item["image_versions2"]["candidates"][0].get("url")
                if not i_url:
                    i_url = item.get("display_url")
                primary = v_url if v_url else i_url
                if primary and primary not in seen_urls:
                    seen_urls.add(primary)
                    unique_items.append((item, v_url, i_url))

            for idx, (item, video_url, image_url) in enumerate(unique_items):
                if dynamic_target_idx is not None and dynamic_target_idx != "all":
                    if int(dynamic_target_idx) != idx:
                        continue
                else:
                    if choice == "video" and not video_url: continue
                    if choice == "image" and video_url: continue

                if video_url:
                    file_path = DOWNLOAD_DIR / f"{url_key}_{idx}.mp4"
                    urllib.request.urlretrieve(video_url, file_path)
                    downloaded_paths.append(file_path)
                elif image_url:
                    file_path = DOWNLOAD_DIR / f"{url_key}_{idx}.jpg"
                    urllib.request.urlretrieve(image_url, file_path)
                    downloaded_paths.append(file_path)

            return downloaded_paths
        except Exception as e:
            raise RuntimeError(f"Error compiling layouts: {e}")

    out = str(DOWNLOAD_DIR / f"{url_key}_%(index)s.%(ext)s")
    dl_args = base_args(target_data) + ["--allow-unplayable-formats", "--no-playlist", "-o", out]
    if choice == "video":
        dl_args += ["-f", "bv*+ba/b"]
    elif choice == "image":
        dl_args += ["-f", "all"]
    dl_args.append(target_data)
    run_ytdlp(dl_args)
    return list(DOWNLOAD_DIR.glob(f"{url_key}_*"))

def probe_url(url: str) -> dict:
    if is_tiktok_photo_url(url):
        return {"_use_gallerydl": True, "url": url}
    stdout, stderr, code = run_ytdlp(base_args(url) + ["-J", "--no-playlist", url])
    if code != 0:
        err = clean_errors(stderr)
        if "Unsupported URL" in err and is_image_capable(url):
            return {"_use_gallerydl": True, "url": url}
        raise RuntimeError(err)
    return json.loads(stdout)

def is_image_post(info: dict) -> bool:
    if info.get("_use_gallerydl"):
        return True

    def _is_image(e: dict) -> bool:
        if e.get("ext", "") in ("jpg", "jpeg", "png", "webp"): return True
        return (e.get("vcodec", "none") or "none") == "none" and not e.get("duration")

    entries = info.get("entries")
    if entries:
        return all(_is_image(e) for e in entries)
    return _is_image(info)

def get_available_heights(info: dict) -> list[int]:
    heights: set[int] = set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and f.get("vcodec", "none") not in ("none", None, ""):
            heights.add(int(h))
    return sorted(heights, reverse=True)

def find_downloaded_file(url_key: str) -> Path | None:
    for _ in range(10):
        candidates = [p for p in DOWNLOAD_DIR.glob(f"{url_key}_*") if not p.name.endswith((".part", ".ytdl"))]
        if candidates: return candidates[0]
        time.sleep(0.5)
    return None

def collect_image_files(url_key: str) -> list[Path]:
    return sorted([p for p in DOWNLOAD_DIR.glob(f"{url_key}_*") if p.suffix.lower() in IMAGE_EXTS and not p.name.endswith((".part", ".ytdl"))])

def download_images(url: str, url_key: str) -> list[Path]:
    out_tpl = str(DOWNLOAD_DIR / f"{url_key}_%(autonumber)03d.%(ext)s")
    if not is_tiktok_photo_url(url):
        run_ytdlp(base_args(url) + ["-o", out_tpl, url])
    files = collect_image_files(url_key)
    if files: return files

    sub = DOWNLOAD_DIR / url_key
    sub.mkdir(exist_ok=True)
    run_gallerydl(url, sub)
    gdl_files = sorted([p for p in sub.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS])
    moved = []
    for i, fp in enumerate(gdl_files):
        dest = DOWNLOAD_DIR / f"{url_key}_{i:03d}{fp.suffix}"
        fp.rename(dest)
        moved.append(dest)
    try: sub.rmdir()
    except Exception: pass
    return moved if moved else collect_image_files(url_key)

# ── Keyboards ──────────────────────────────────────────────────────────────────

def build_video_keyboard(url_key: str, heights: list[int]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton("⭐ Best Quality", callback_data=f"dl|{url_key}|best")]]
    for i, (label, h_int, _fmt) in enumerate(QUALITY_PRESETS):
        if any(h <= h_int for h in heights):
            buttons.append([InlineKeyboardButton(label, callback_data=f"dl|{url_key}|{i}")])
    buttons.append([InlineKeyboardButton("🎵 Audio only", callback_data=f"dl|{url_key}|audio")])
    return InlineKeyboardMarkup(buttons)

def build_photo_picker(url_key: str, count: int) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton("📸 All photos", callback_data=f"pick|{url_key}|all")]]
    for i in range(count):
        buttons.append([InlineKeyboardButton(f"Photo {i + 1}", callback_data=f"pick|{url_key}|{i}")])
    return InlineKeyboardMarkup(buttons)

def build_dynamic_instagram_keyboard(url_key: str, img_count: int, vid_count: int) -> InlineKeyboardMarkup:
    buttons = []
    total = img_count + vid_count
    if img_count > 0 and vid_count > 0:
        buttons.append([InlineKeyboardButton(f"📸 Images ({img_count})", callback_data=f"ig_choice|{url_key}|image")])
        buttons.append([InlineKeyboardButton(f"🎥 Videos ({vid_count})", callback_data=f"ig_choice|{url_key}|video")])
        buttons.append([InlineKeyboardButton("📦 Everything Combined", callback_data=f"ig_choice|{url_key}|all")])
    elif img_count > 1:
        buttons.append([InlineKeyboardButton(f"📸 All Images ({img_count})", callback_data=f"ig_choice|{url_key}|image")])
    elif vid_count > 1:
        buttons.append([InlineKeyboardButton(f"🎥 All Videos ({vid_count})", callback_data=f"ig_choice|{url_key}|video")])
    if total > 1:
        row = []
        for i in range(total):
            row.append(InlineKeyboardButton(f"Item {i+1}", callback_data=f"ig_pick|{url_key}|{i}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row: buttons.append(row)
    else:
        buttons.append([InlineKeyboardButton("⬇️ Extract Content Asset", callback_data=f"ig_choice|{url_key}|all")])
    return InlineKeyboardMarkup(buttons)

# ── Upload & Link Generation Helpers ──────────────────────────────────────────

def generate_download_link(filepath: Path) -> str:
    safe_name = urllib.parse.quote(filepath.name)
    if RENDER_EXTERNAL_URL:
        return f"{RENDER_EXTERNAL_URL}/download/{safe_name}"
    return f"http://localhost:{PORT}/download/{safe_name}"

async def send_photos(message, files: list[Path]) -> None:
    if not files: return
    if len(files) == 1:
        with open(files[0], "rb") as f:
            await message.reply_photo(photo=f, read_timeout=300, write_timeout=300)
        return
    for batch_start in range(0, len(files), 10):
        batch = files[batch_start:batch_start + 10]
        opened = [open(fp, "rb") for fp in batch]
        try:
            media = [InputMediaPhoto(media=fh) for fh in opened]
            await message.reply_media_group(media=media, read_timeout=300, write_timeout=300)
        finally:
            for fh in opened: fh.close()

# ── Handler Logic ─────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 Send me a URL and i'll download it")

async def handle_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = ""
    if update.message.document:
        doc = update.message.document
        if doc.file_name and doc.file_name.lower() in ["message.txt", "document.txt", "file.txt"] or doc.mime_type == "text/plain":
            msg = await update.message.reply_text("📥 Processing layout text...")
            try:
                tg_file = await ctx.bot.get_file(doc.file_id)
                text = (await tg_file.download_as_bytearray()).decode("utf-8").strip()
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"❌ Failed to parse file payload: {e}")
                return

    if not text and update.message.text:
        text = update.message.text.strip()
    if not text: return

    if text.startswith("{") and ("xdt_api" in text or "shortcode_media" in text or '"data"' in text or "items" in text):
        msg = await update.message.reply_text("⚙️ Analyzing Layout Blueprint...")
        url_key = str(msg.message_id)
        ctx.user_data[url_key] = {"raw_json": text, "is_raw": True}
        img_c = max(text.count('"display_url"'), text.count('"image_versions2"'))
        vid_c = max(text.count('"video_url"'), text.count('"video_versions"'))
        if vid_c > 0 and img_c >= vid_c: img_c -= vid_c
        if img_c == 0 and vid_c == 0: img_c, vid_c = 1, 1
        await msg.edit_text(f"📊 **Instagram Layout Data Parsed**\nFound {img_c} photos and {vid_c} videos.",
                            reply_markup=build_dynamic_instagram_keyboard(url_key, img_c, vid_c), parse_mode="Markdown")
        return

    url = extract_url(text)
    if not url or not is_allowed_site(url): return

    site = detect_site(url)

    if site == "Instagram":
        msg = await update.message.reply_text("🔍 Fetching Instagram post...", parse_mode="Markdown")
        url_key = str(msg.message_id)
        ctx.user_data[url_key] = {"url": url, "is_raw": False}

        # Try direct fetch first (works for public posts without any manual steps)
        if not is_instagram_story_url(url):
            files = await asyncio.get_event_loop().run_in_executor(
                None, fetch_instagram_public, url, url_key
            )
            if files:
                images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
                videos = [f for f in files if f.suffix.lower() not in IMAGE_EXTS]
                total  = len(files)
                # All cases: store under "files" so handle_photo_pick serves them directly
                ctx.user_data[url_key] = {"files": [str(p) for p in files]}
                if images and not videos:
                    await msg.edit_text(
                        f"🖼 Found {total} photo(s):",
                        reply_markup=build_photo_picker(url_key, total)
                    )
                elif videos and not images and total == 1:
                    if videos[0].stat().st_size <= TELEGRAM_MAX_BYTES:
                        await msg.edit_text("📤 Uploading...")
                        with open(videos[0], "rb") as fh:
                            await msg.reply_video(video=fh, supports_streaming=True,
                                                  read_timeout=600, write_timeout=600)
                        await msg.delete()
                        videos[0].unlink(missing_ok=True)
                        ctx.user_data.pop(url_key, None)
                    else:
                        dl_url = generate_download_link(videos[0])
                        await msg.edit_text(
                            f"📦 Video too large for Telegram.\n🔗 [Download directly]({dl_url})",
                            parse_mode="Markdown"
                        )
                else:
                    # multiple videos or mixed carousel — let user pick
                    label = (f"🎥 Found {total} video(s):" if not images
                             else f"📦 Found {len(images)} photo(s) and {len(videos)} video(s):")
                    await msg.edit_text(label, reply_markup=build_photo_picker(url_key, total))
                return

        # ── Stories: try automatic yt-dlp download first ──────────────────────
        if is_instagram_story_url(url):
            await msg.edit_text("👻 Attempting automatic Story download...")
            story_files = await asyncio.get_event_loop().run_in_executor(
                None, fetch_instagram_stories, url, url_key
            )
            if story_files:
                images = [f for f in story_files if f.suffix.lower() in IMAGE_EXTS]
                videos = [f for f in story_files if f.suffix.lower() not in IMAGE_EXTS]
                total = len(story_files)
                if total == 1:
                    await msg.edit_text("📤 Uploading story item...")
                    target = story_files[0]
                    try:
                        if target.suffix.lower() in IMAGE_EXTS:
                            with open(target, "rb") as fh:
                                await msg.reply_photo(photo=fh)
                        else:
                            if target.stat().st_size > TELEGRAM_MAX_BYTES:
                                dl_url = generate_download_link(target)
                                await msg.edit_text(
                                    f"📦 Story video too large for Telegram.\n🔗 [Download directly]({dl_url})",
                                    parse_mode="Markdown"
                                )
                                return
                            with open(target, "rb") as fh:
                                await msg.reply_video(video=fh, supports_streaming=True,
                                                      read_timeout=600, write_timeout=600)
                        await msg.delete()
                        target.unlink(missing_ok=True)
                    except Exception as e:
                        await msg.edit_text(f"❌ Upload failed: {e}")
                    return
                # Multiple story items — build a simple picker
                ctx.user_data[url_key] = {"files": [str(f) for f in story_files]}
                label = (
                    f"Found {len(images)} photo(s) and {len(videos)} video(s)"
                    if images and videos else f"Found {total} story item(s)"
                )
                await msg.edit_text(
                    f"👻 **Instagram Stories — {label}**",
                    reply_markup=build_photo_picker(url_key, total),
                    parse_mode="Markdown"
                )
                return
            # Auto-download failed — try to generate a story GraphQL link, else manual DevTools
            api_url, _ = get_instagram_graphql_instructions(url)
            if api_url:
                instructions = (
                    "👻 **Instagram Story Detected**\n\n"
                    "Automatic download failed.\n\n"
                    f"1. Open this link while logged in: [Story GraphQL Payload]({api_url})\n"
                    "2. Copy the full JSON response.\n"
                    "3. **Paste or upload that JSON here.**"
                )
            else:
                instructions = (
                    "👻 **Instagram Story Detected**\n\n"
                    "Automatic download failed (stories require a logged-in session).\n\n"
                    "**Manual steps:**\n"
                    "1. Open the Story in your browser and press **F12** → Network tab.\n"
                    "2. Filter requests by `reels_media` or `graphql`.\n"
                    "3. Refresh (**Ctrl+R**), then copy the full **Response** of the matching request.\n"
                    "4. **Paste or upload that JSON text** directly into this chat."
                )
            await msg.edit_text(instructions, parse_mode="Markdown", disable_web_page_preview=True)
            return

        # ── Non-story private/rate-limited fallback ────────────────────────────
        # Attempt 1: auto-fetch via GraphQL (scrape doc_id → env fallback)
        await msg.edit_text("🔒 Post is private or rate-limited — trying GraphQL fetch...")
        gql_files = await asyncio.get_event_loop().run_in_executor(
            None, fetch_instagram_post_graphql, url, url_key
        )
        if gql_files:
            images = [f for f in gql_files if f.suffix.lower() in IMAGE_EXTS]
            videos = [f for f in gql_files if f.suffix.lower() not in IMAGE_EXTS]
            total  = len(gql_files)
            ctx.user_data[url_key] = {"files": [str(p) for p in gql_files]}
            if images and not videos:
                await msg.edit_text(
                    f"🖼 Found {total} photo(s):",
                    reply_markup=build_photo_picker(url_key, total)
                )
            elif videos and not images and total == 1:
                if videos[0].stat().st_size <= TELEGRAM_MAX_BYTES:
                    await msg.edit_text("📤 Uploading...")
                    with open(videos[0], "rb") as fh:
                        await msg.reply_video(video=fh, supports_streaming=True,
                                              read_timeout=600, write_timeout=600)
                    await msg.delete()
                    videos[0].unlink(missing_ok=True)
                    ctx.user_data.pop(url_key, None)
                else:
                    dl_url = generate_download_link(videos[0])
                    await msg.edit_text(
                        f"📦 Video too large for Telegram.\n🔗 [Download directly]({dl_url})",
                        parse_mode="Markdown"
                    )
            else:
                label = (f"🎥 Found {total} video(s):" if not images
                         else f"📦 Found {len(images)} photo(s) and {len(videos)} video(s):")
                await msg.edit_text(label, reply_markup=build_photo_picker(url_key, total))
            return

        # Attempt 2: manual paste — generate a clickable GraphQL link
        api_url, _ = get_instagram_graphql_instructions(url)
        if api_url:
            instructions = (
                "🔒 **Private Instagram Post Detected**\n\n"
                "Automatic fetch failed — this post is private or heavily rate-limited.\n\n"
                f"1. Open this link while logged in: [GraphQL Payload]({api_url})\n"
                "2. Copy the full JSON response (**Ctrl+A**, **Ctrl+C**).\n"
                "3. **Paste or upload the text** right here in this chat."
            )
        else:
            instructions = (
                "🔒 **Private Instagram Post Detected**\n\n"
                "Direct fetch didn't work and no GraphQL URL could be generated.\n\n"
                "**Manual steps:**\n"
                "1. Open the post in your browser and press **F12** → Network tab.\n"
                "2. Filter requests by `graphql`.\n"
                "3. Reload and copy the full **Response** JSON.\n"
                "4. **Paste or upload the text** right here in this chat."
            )
        await msg.edit_text(instructions, parse_mode="Markdown", disable_web_page_preview=True)
        return
        
    msg = await update.message.reply_text(f"🔍 Scraping **{site}** content...", parse_mode="Markdown")
    url_key = str(msg.message_id)

    if is_tiktok_photo_url(url):
        files = await asyncio.get_event_loop().run_in_executor(None, download_images, url, url_key)
        if not files:
            await msg.edit_text("❌ Failed to resolve slideshow items.")
            return
        ctx.user_data[url_key] = {"files": [str(f) for f in files]}
        await msg.edit_text(f"📸 TikTok Slideshow Discovered ({len(files)} items):", reply_markup=build_photo_picker(url_key, len(files)))
        return

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
    except Exception as e:
        await msg.edit_text(f"❌ Scraping failure:\n`{clean_errors(str(e))}`", parse_mode="Markdown")
        return

    if is_image_post(info):
        files = await asyncio.get_event_loop().run_in_executor(None, download_images, url, url_key)
        if not files:
            await msg.edit_text("❌ Media extraction empty.")
            return
        ctx.user_data[url_key] = {"files": [str(p) for p in files]}
        await msg.edit_text(f"🖼 Found {len(files)} photos:", reply_markup=build_photo_picker(url_key, len(files)))
        return

    heights = get_available_heights(info)
    ctx.user_data[url_key] = {"url": url, "type": "video", "presets": [fmt for (_label, h_int, fmt) in QUALITY_PRESETS if any(hv <= h_int for hv in heights)]}
    await msg.edit_text(f"📺 *{site}* stream targeted. Select output layout profiles:", reply_markup=build_video_keyboard(url_key, heights), parse_mode="Markdown")

async def handle_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired.")
        return

    url = data["url"]
    presets = data.get("presets", [])

    if "redgifs.com" in url.lower():
        fmt_arg = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        is_audio = False
    elif choice == "audio":
        fmt_arg, is_audio = "bestaudio/best", True
    elif choice == "best":
        fmt_arg = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/bestvideo+bestaudio/best"
        is_audio = False
    else:
        idx = int(choice)
        h = presets[idx] if idx < len(presets) else "1080"
        fmt_arg = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}]/"
            f"best"
        )

    await query.edit_message_text("⬇️ Compiling media payload files...")
    out = str(DOWNLOAD_DIR / f"{url_key}_%(title).60s.%(ext)s")
    dl_args = base_args(url) + ["-f", fmt_arg, "--merge-output-format", "mp4", "--no-playlist", "--format-sort", "ext:mp4:m4a", "-o", out, url]
    if is_audio:
        dl_args += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]

    _, stderr, code = await asyncio.get_event_loop().run_in_executor(None, lambda: run_ytdlp(dl_args))
    if code != 0:
        await query.edit_message_text(f"❌ Extraction error:\n`{clean_errors(stderr)}`")
        return

    filepath = await asyncio.get_event_loop().run_in_executor(None, find_downloaded_file, url_key)
    if not filepath:
        await query.edit_message_text("❌ Target item compiled but missing on disk.")
        return

    size_bytes = filepath.stat().st_size
    if size_bytes > TELEGRAM_MAX_BYTES:
        dl_url = generate_download_link(filepath)
        await query.edit_message_text(
            f"📦 **File is too large for Telegram Upload ({size_bytes // 1024 // 1024} MB)**\n\n"
            f"You can download your video layout directly from the server stream link here:\n"
            f"🔗 [Click to Download Video Assets Directly]({dl_url})",
            parse_mode="Markdown"
        )
        return

    await query.edit_message_text("📤 Uploading assets to Telegram...")
    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await query.message.reply_audio(audio=f, filename=filepath.name, read_timeout=600, write_timeout=600)
            else:
                await query.message.reply_video(video=f, filename=filepath.name, supports_streaming=True, read_timeout=600, write_timeout=600)
        await query.delete_message()
        filepath.unlink(missing_ok=True)
    except Exception as e:
        dl_url = generate_download_link(filepath)
        await query.edit_message_text(f"⚠️ Telegram network stall. Fallback download link:\n🔗 [Server Download Link]({dl_url})", parse_mode="Markdown")
    finally:
        ctx.user_data.pop(url_key, None)

async def handle_instagram_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data: return

    is_raw = data.get("is_raw", False)
    target_payload = data["raw_json"] if is_raw else data["url"]
    await query.edit_message_text("⬇️ Extracting selected targets...")

    files = await asyncio.get_event_loop().run_in_executor(None, parse_and_download_instagram, target_payload, url_key, choice, is_raw)
    if not files:
        await query.edit_message_text("❌ Media targets matching request empty.")
        return

    await query.edit_message_text("📤 Shipping data items...")
    try:
        images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
        videos = [f for f in files if f.suffix.lower() not in IMAGE_EXTS]
        if images: await send_photos(query.message, images)
        for vid in videos:
            if vid.stat().st_size > TELEGRAM_MAX_BYTES:
                dl_url = generate_download_link(vid)
                await query.message.reply_text(f"📦 Huge file fallback link:\n🔗 [Download Item Directly]({dl_url})", parse_mode="Markdown")
            else:
                with open(vid, "rb") as f:
                    await query.message.reply_video(video=f, supports_streaming=True, read_timeout=600, write_timeout=600)
        await query.delete_message()
        for fp in files: fp.unlink(missing_ok=True)
    except Exception:
        pass
    finally:
        ctx.user_data.pop(url_key, None)

async def handle_instagram_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, url_key, target_idx = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data: return

    is_raw = data.get("is_raw", False)
    target_payload = data["raw_json"] if is_raw else data["url"]
    files = await asyncio.get_event_loop().run_in_executor(None, parse_and_download_instagram, target_payload, url_key, "all", is_raw, target_idx)
    if not files: return

    target = files[0]
    if target.stat().st_size > TELEGRAM_MAX_BYTES:
        dl_url = generate_download_link(target)
        await query.edit_message_text(f"📦 Link fallback:\n🔗 [Download]({dl_url})", parse_mode="Markdown")
    else:
        with open(target, "rb") as f:
            if target.suffix.lower() in IMAGE_EXTS:
                await query.message.reply_photo(photo=f)
            else:
                await query.message.reply_video(video=f, supports_streaming=True)
    await query.delete_message()
    target.unlink(missing_ok=True)
    ctx.user_data.pop(url_key, None)

async def handle_photo_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data: return

    all_files = [Path(p) for p in data.get("files", []) if Path(p).exists()]
    selected = all_files if choice == "all" else [all_files[int(choice)]]
    await send_photos(query.message, selected)
    await query.delete_message()
    if choice == "all" or len(all_files) <= 1:
        for fp in all_files: fp.unlink(missing_ok=True)
    ctx.user_data.pop(url_key, None)

async def debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    import shutil
    lines = []
    # Cookie file status
    render_path = Path("/etc/secrets/youtube_cookies.txt")
    runtime_path = DOWNLOAD_DIR / "youtube_cookies.txt"
    lines.append(f"Render secret exists: {render_path.exists()}")
    if render_path.exists():
        lines.append(f"Render secret size: {render_path.stat().st_size} bytes")
        first = render_path.read_text(encoding="utf-8", errors="replace")[:80]
        lines.append(f"First 80 chars: {repr(first)}")
    lines.append(f"Runtime cookie exists: {runtime_path.exists()}")
    active = DOWNLOAD_DIR / "active_cookies.txt"
    lines.append(f"Active cookie exists: {active.exists()}")
    # yt-dlp test on instagram
    stdout, stderr, code = run_ytdlp([
        "--no-warnings", "--no-check-certificates",
        "--user-agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "--add-header", f"X-Ig-App-Id:{INSTAGRAM_APP_ID}",
        "--socket-timeout", "15",
        "--no-playlist",
        "-J", "https://www.instagram.com/p/DZvyyGYDKD5/",
    ])
    lines.append(f"yt-dlp exit code (no cookies): {code}")
    lines.append(f"stderr: {stderr.strip()[:200]}")
    cookies = get_cookies_path()
    if cookies:
        stdout2, stderr2, code2 = run_ytdlp([
            "--no-warnings", "--no-check-certificates",
            "--user-agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "--add-header", f"X-Ig-App-Id:{INSTAGRAM_APP_ID}",
            "--cookies", str(cookies),
            "--socket-timeout", "15",
            "--no-playlist",
            "-J", "https://www.instagram.com/p/DZvyyGYDKD5/",
        ])
        lines.append(f"yt-dlp exit code (WITH cookies): {code2}")
        lines.append(f"stderr: {stderr2.strip()[:200]}")
    else:
        lines.append("No cookies file found — skipping cookie test")
    await update.message.reply_text("\n".join(lines))

def main() -> None:
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, handle_input))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_photo_pick, pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(handle_instagram_choice, pattern=r"^ig_choice\|"))
    app.add_handler(CallbackQueryHandler(handle_instagram_pick, pattern=r"^ig_pick\|"))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
