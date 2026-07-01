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
from socketserver import ThreadingMixIn
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

KNOWN_SITES: dict[str, str] = {
    "tiktok.com":  "TikTok",
    "redgifs.com": "RedGifs",
    "instagram.com": "Instagram",
    "pornhub.com": "PornHub",
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

PASTE_PAGE_HTML = """<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paste JSON</title>
<style>
body{font-family:-apple-system,sans-serif;background:#111;color:#eee;padding:16px;margin:0}
h3{font-size:16px}
textarea{width:100%;height:55vh;box-sizing:border-box;background:#1c1c1c;color:#eee;
  border:1px solid #444;border-radius:8px;padding:10px;font-size:14px}
button{width:100%;padding:14px;margin-top:12px;font-size:16px;border:none;border-radius:8px;
  background:#2ea6ff;color:#fff}
#status{margin-top:12px;text-align:center;font-size:15px}
</style></head>
<body>
<h3>Paste the copied JSON response below, then submit</h3>
<textarea id="j" placeholder="Long-press → Paste here..."></textarea>
<button onclick="submitIt()">Submit to bot</button>
<div id="status"></div>
<script>
async function submitIt(){
  const v = document.getElementById('j').value;
  const s = document.getElementById('status');
  s.textContent = 'Sending...';
  try {
    const r = await fetch('/submit/__KEY__', {method:'POST', body: v});
    s.textContent = r.ok ? '✅ Sent! You can go back to Telegram now.' : '❌ Failed, try again.';
  } catch (e) {
    s.textContent = '❌ Failed, try again.';
  }
}
</script>
</body></html>"""

class CombinedServerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/paste/"):
            key = urllib.parse.unquote(self.path.split("/paste/", 1)[1]).strip("/")
            key = re.sub(r"[^A-Za-z0-9_\-]", "", key)
            body = PASTE_PAGE_HTML.replace("__KEY__", key).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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

    def do_POST(self):
        if self.path.startswith("/submit/"):
            key = urllib.parse.unquote(self.path.split("/submit/", 1)[1]).strip("/")
            key = re.sub(r"[^A-Za-z0-9_\-]", "", key)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body_text = self.rfile.read(length).decode("utf-8", errors="ignore")
            except Exception:
                body_text = ""
            ok = bool(key) and bool(body_text.strip())
            if ok:
                try:
                    (DOWNLOAD_DIR / f"paste_{key}.json").write_text(body_text, encoding="utf-8")
                except Exception:
                    ok = False
            resp = b"OK" if ok else b"EMPTY"
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
        self.send_error(404, "Not Found")

    def log_message(self, *args):
        pass

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def run_health_server():
    _ThreadingHTTPServer(("0.0.0.0", PORT), CombinedServerHandler).serve_forever()

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

def fetch_instagram_public(url: str, url_key: str) -> tuple[list[Path] | None, str]:
    """
    Fetch a public Instagram post via the i.instagram.com media info API.
    Returns (files_or_None, error_reason) — error_reason is only meaningful
    when files is None, so the caller can log/show why it failed instead of
    silently moving on.
    """
    import urllib.request as _req
    import http.cookiejar

    match = re.search(r"instagram\.com/(?:p|reel|tv|share/v)/([^/?#&]+)", url)
    if not match:
        return None, "Couldn't parse a shortcode out of that URL."
    shortcode = match.group(1)

    api_url = f"https://i.instagram.com/api/v1/media/shortcode/web_info/?shortcode={shortcode}&include_reel=false"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                      "Mobile/15E148 Safari/604.1",
        "X-Ig-App-Id": "936619743392459",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    }

    # Instagram increasingly rate-limits/blocks fully anonymous requests from
    # cloud-hosted IPs (like Render's) — attaching the bot's own session
    # cookies, if configured, makes this look like a normal logged-in browser.
    opener = _req.build_opener()
    cookies = get_cookies_path()
    if cookies:
        try:
            jar = http.cookiejar.MozillaCookieJar(str(cookies))
            jar.load(ignore_discard=True, ignore_expires=True)
            opener = _req.build_opener(_req.HTTPCookieProcessor(jar))
        except Exception as e:
            print(f"⚠️ IG cookie jar load failed for public-post fetch: {e}")

    try:
        req = _req.Request(api_url, headers=headers)
        with opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"⚠️ IG web_info lookup failed for shortcode '{shortcode}': {error}")
        return None, error

    try:
        raw_json = json.dumps(data)
        files = parse_and_download_instagram(raw_json, url_key, "all", is_raw_json=True)
        if files:
            return files, ""
        return None, "API responded but no media items could be parsed out of it."
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def is_instagram_story_url(url: str) -> bool:
    """Return True for any Instagram Stories or Highlights URL."""
    low = url.lower()
    return "instagram.com" in low and (
        "/stories/" in low or
        "/s/" in low or          # short share links for stories
        "highlight:" in low      # highlight reel links
    )


def fetch_instagram_stories(url: str, url_key: str) -> tuple[list[Path], str]:
    """
    Try to download Instagram Stories via yt-dlp's native "instagram:story"
    extractor. Instagram requires a logged-in session to view *any* story
    (public accounts included), so this needs valid cookies — the same
    cookies file already used elsewhere in the bot for the account you're
    running it as.

    Returns (downloaded_paths, error_message). error_message is only
    meaningful when downloaded_paths is empty, so the caller can show the
    real reason (missing cookies, expired session, etc.) instead of a
    generic failure.
    """
    cookies = get_cookies_path()
    if not cookies:
        return [], "No Instagram session cookies configured — stories require a logged-in account."

    args = [
        "--no-warnings",
        "--rm-cache-dir",
        "--no-cache-dir",
        "--user-agent",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1",
        "--add-header", "X-Ig-App-Id:936619743392459",
        "--socket-timeout", "20",
        "--cookies", str(cookies),
    ]

    # Download directly in one pass — let yt-dlp enumerate every item in the
    # story reel itself instead of us trying to reconstruct per-item URLs.
    out_tpl = str(DOWNLOAD_DIR / f"{url_key}_%(autonumber)03d.%(ext)s")
    dl_args = args + [
        "--merge-output-format", "mp4",
        "--yes-playlist",
        "-o", out_tpl,
        url,
    ]
    _, stderr, code = run_ytdlp(dl_args)

    downloaded = [
        p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
        if not p.name.endswith((".part", ".ytdl"))
    ]
    if downloaded:
        return downloaded, ""
    return [], clean_errors(stderr) if code != 0 else "No story items returned."


def get_instagram_user_pk(username: str) -> tuple[str | None, str]:
    """Resolve a username to its numeric Instagram user id (needed for the
    reel_ids story GraphQL query). This just reads public profile metadata —
    the same info any browser gets loading the profile page — it does not
    fetch any story content itself.

    Returns (pk_or_None, error_reason). error_reason is only meaningful when
    pk is None, so callers/logs can tell *why* it failed instead of guessing."""
    import urllib.request as _req
    import http.cookiejar

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                      "Mobile/15E148 Safari/604.1",
        "X-Ig-App-Id": "936619743392459",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    opener = _req.build_opener()
    cookies = get_cookies_path()
    if cookies:
        try:
            jar = http.cookiejar.MozillaCookieJar(str(cookies))
            jar.load(ignore_discard=True, ignore_expires=True)
            opener = _req.build_opener(_req.HTTPCookieProcessor(jar))
        except Exception as e:
            print(f"⚠️ IG cookie jar load failed for pk lookup: {e}")

    # Attempt 1: mobile web_profile_info API (fast, structured)
    api_url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={urllib.parse.quote(username)}"
    try:
        req = _req.Request(api_url, headers=headers)
        with opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        pk = data.get("data", {}).get("user", {}).get("id")
        if pk:
            return str(pk), ""
    except Exception as e:
        api_error = f"{type(e).__name__}: {e}"
        print(f"⚠️ IG web_profile_info lookup failed for '{username}': {api_error}")
    else:
        api_error = "web_profile_info returned no user id (account may not exist)"

    # Attempt 2: scrape the profile page HTML for the embedded numeric id
    try:
        page_headers = dict(headers)
        page_headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        req = _req.Request(f"https://www.instagram.com/{urllib.parse.quote(username)}/", headers=page_headers)
        with opener.open(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m = re.search(r'"profilePage_(\d+)"', html) or re.search(r'"id"\s*:\s*"(\d+)"\s*,\s*"username"\s*:\s*"' + re.escape(username), html)
        if m:
            return m.group(1), ""
        return None, api_error
    except Exception as e:
        return None, f"{api_error}; page scrape also failed: {type(e).__name__}: {e}"


def get_instagram_graphql_instructions(url: str) -> tuple[str | None, bool, str]:
    """
    Returns (graphql_api_url_for_manual_paste, is_story, error_reason).
    For posts: tries to scrape a fresh doc_id from the post page so the link
    actually works, falling back to the last known working doc_id.
    For stories: builds a reel_ids query. The link still has to be opened by
    the user in a browser where THEY are logged into an Instagram account
    that already has access to that story (their own account, or an account
    already following the private user) — this bot never authenticates as
    anyone, it just formats the request for you to open and copy back.
    """
    import urllib.request as _req

    if is_instagram_story_url(url):
        m = re.search(r"instagram\.com/stories/([^/?#&]+)/", url)
        if not m:
            return None, True, "Couldn't parse a username out of that story URL."
        username = m.group(1)
        pk, pk_error = get_instagram_user_pk(username)
        if not pk:
            return None, True, pk_error or "Unknown lookup failure."
        variables = {
            "reel_ids": [int(pk)],
            "highlight_reel_ids": [],
            "precomposed_overlay": False,
        }
        encoded_vars = urllib.parse.quote(json.dumps(variables))
        return (
            f"https://www.instagram.com/graphql/query/"
            f"?query_hash=de8017ee0a7c9c45ec4260733d81ea31&variables={encoded_vars}",
            True,
            "",
        )
    match = re.search(r"instagram\.com/(?:p|reel|tv|share/v)/([^/?#&]+)", url)
    if not match:
        return None, False, "Couldn't parse a shortcode out of that URL."
    shortcode = match.group(1)

    # Try to scrape a live doc_id from the post page
    doc_id = "24368985919464652"  # fallback
    try:
        page_url = f"https://www.instagram.com/p/{shortcode}/"
        req = _req.Request(page_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with _req.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m = re.search(r'"doc_id"\s*:\s*"?(\d{10,})"?', html)
        if m:
            doc_id = m.group(1)
    except Exception:
        pass

    variables = {
        "shortcode": shortcode,
        "fetch_tagged_user_count": None,
        "hoisted_comment_id": None,
        "hoisted_reply_id": None,
    }
    encoded_vars = urllib.parse.quote(json.dumps(variables))
    return f"https://www.instagram.com/graphql/query/?doc_id={doc_id}&variables={encoded_vars}", False, ""

def parse_and_download_instagram(target_data: str, url_key: str, choice: str = "all", is_raw_json: bool = False, dynamic_target_idx: str = None) -> list[Path]:
    downloaded_paths = []
    if is_raw_json:
        try:
            payload = json.loads(target_data)

            def find_media_blocks(data):
                blocks = []
                if isinstance(data, dict):
                    if any(k in data for k in ["video_versions", "image_versions2", "video_url", "display_url", "video_resources", "display_resources"]):
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
                if not v_url and item.get("video_resources"):
                    # Story GraphQL shape (reel_ids query) — last entry is usually highest-res
                    v_url = item["video_resources"][-1].get("src")
                i_url = None
                if "image_versions2" in item and item["image_versions2"].get("candidates"):
                    i_url = item["image_versions2"]["candidates"][0].get("url")
                if not i_url:
                    i_url = item.get("display_url")
                if not i_url and item.get("display_resources"):
                    i_url = item["display_resources"][-1].get("src")
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

def generate_paste_link(url_key: str) -> str:
    if RENDER_EXTERNAL_URL:
        return f"{RENDER_EXTERNAL_URL}/paste/{url_key}"
    return f"http://localhost:{PORT}/paste/{url_key}"

async def wait_for_pasted_json(url_key: str, timeout: float = 900, interval: float = 1.5) -> str | None:
    """Poll for the paste_<key>.json file the web paste page writes. Returns
    the pasted text once it shows up (and deletes the file), or None if the
    user never submits within `timeout` seconds."""
    path = DOWNLOAD_DIR / f"paste_{url_key}.json"
    waited = 0.0
    while waited < timeout:
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                text = None
            path.unlink(missing_ok=True)
            return text
        await asyncio.sleep(interval)
        waited += interval
    return None

async def process_pasted_instagram_json(msg, url_key: str, user_data: dict, raw_text: str) -> None:
    """Shared logic for handling a pasted GraphQL/API JSON blob, whether it
    arrived by chat message or via the web paste page."""
    user_data[url_key] = {"raw_json": raw_text, "is_raw": True}
    img_c = max(raw_text.count('"display_url"'), raw_text.count('"image_versions2"'), raw_text.count('"display_resources"'))
    vid_c = max(raw_text.count('"video_url"'), raw_text.count('"video_versions"'), raw_text.count('"video_resources"'))
    if vid_c > 0 and img_c >= vid_c: img_c -= vid_c
    if img_c == 0 and vid_c == 0: img_c, vid_c = 1, 1
    await msg.edit_text(
        f"📊 **Instagram Layout Data Parsed**\nFound {img_c} photos and {vid_c} videos.",
        reply_markup=build_dynamic_instagram_keyboard(url_key, img_c, vid_c), parse_mode="Markdown"
    )

async def launch_paste_listener(msg, url_key: str, user_data: dict) -> None:
    """Background task: waits for the user to submit JSON via the web paste
    page, then processes it automatically — no need to come back and paste
    into chat manually."""
    raw_text = await wait_for_pasted_json(url_key)
    if not raw_text:
        try:
            await msg.edit_text("⌛ Paste link expired — send the URL again to retry.")
        except Exception:
            pass
        return
    try:
        await process_pasted_instagram_json(msg, url_key, user_data, raw_text)
    except Exception as e:
        try:
            await msg.edit_text(f"❌ Failed to parse pasted data: {e}")
        except Exception:
            pass

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
        await process_pasted_instagram_json(msg, url_key, ctx.user_data, text)
        return

    url = extract_url(text)
    if not url or not is_allowed_site(url): return

    site = detect_site(url)

    if site == "Instagram":
        msg = await update.message.reply_text("🔍 Fetching Instagram post...", parse_mode="Markdown")
        url_key = str(msg.message_id)
        ctx.user_data[url_key] = {"url": url, "is_raw": False}

        # Try direct fetch first (works for public posts without any manual steps)
        fetch_error = ""
        if not is_instagram_story_url(url):
            files, fetch_error = await asyncio.get_event_loop().run_in_executor(
                None, fetch_instagram_public, url, url_key
            )
            if files:
                images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
                videos = [f for f in files if f.suffix.lower() not in IMAGE_EXTS]
                if images and not videos:
                    ctx.user_data[url_key] = {"files": [str(p) for p in images]}
                    await msg.edit_text(
                        f"🖼 Found {len(images)} photo(s):",
                        reply_markup=build_photo_picker(url_key, len(images))
                    )
                elif videos and not images:
                    # single video — discard the raw-JSON copy and try the normal
                    # yt-dlp probe path instead, so the user gets a quality picker
                    for f in files:
                        f.unlink(missing_ok=True)
                    files = None
                else:
                    # mixed carousel
                    img_c = len(images)
                    vid_c = len(videos)
                    ctx.user_data[url_key] = {"raw_json": json.dumps({"_prefetched_files": [str(f) for f in files]}), "is_raw": False, "_files": [str(f) for f in files]}
                    await msg.edit_text(
                        f"📊 **Instagram post fetched**\nFound {img_c} photo(s) and {vid_c} video(s).",
                        reply_markup=build_dynamic_instagram_keyboard(url_key, img_c, vid_c),
                        parse_mode="Markdown"
                    )
                    return
                if files:
                    return

            # The web_info API either failed or (for single videos) we
            # deliberately want the quality picker — try yt-dlp's own
            # Instagram extractor next, same as every other supported site.
            try:
                info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
                if is_image_post(info):
                    img_files = await asyncio.get_event_loop().run_in_executor(None, download_images, url, url_key)
                    if img_files:
                        ctx.user_data[url_key] = {"files": [str(p) for p in img_files]}
                        await msg.edit_text(
                            f"🖼 Found {len(img_files)} photo(s):",
                            reply_markup=build_photo_picker(url_key, len(img_files))
                        )
                        return
                else:
                    heights = get_available_heights(info)
                    ctx.user_data[url_key] = {
                        "url": url, "type": "video",
                        "presets": [fmt for (_label, h_int, fmt) in QUALITY_PRESETS if any(hv <= h_int for hv in heights)]
                    }
                    await msg.edit_text(
                        "📺 *Instagram* stream targeted. Select output layout profiles:",
                        reply_markup=build_video_keyboard(url_key, heights), parse_mode="Markdown"
                    )
                    return
            except Exception as e:
                print(f"⚠️ yt-dlp Instagram probe also failed for {url}: {clean_errors(str(e))}")
                if not fetch_error:
                    fetch_error = clean_errors(str(e))

        # ── Stories: try automatic yt-dlp download first ──────────────────────
        if is_instagram_story_url(url):
            await msg.edit_text("👻 Attempting automatic Story download...")
            story_files, story_error = await asyncio.get_event_loop().run_in_executor(
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
            # Auto-download failed — fall back to the GraphQL link + paste page
            api_url, _, lookup_error = get_instagram_graphql_instructions(url)
            paste_url = generate_paste_link(url_key)
            if api_url:
                instructions = (
                    "👻 **Instagram Story Detected**\n\n"
                    f"Automatic download failed ({story_error or 'stories need a logged-in session'}).\n\n"
                    "**Steps:**\n"
                    f"1. Open this link: [Story GraphQL Data]({api_url})\n"
                    "   — open it in a browser where *you're* logged into an account "
                    "that already has access to this story (if it's private, an account following them).\n"
                    "2. Select all and copy the page contents.\n"
                    f"3. Paste it in the [web paste page]({paste_url}) — works on mobile — "
                    "and it'll continue automatically. (Pasting directly into this chat still works too.)"
                )
                asyncio.create_task(launch_paste_listener(msg, url_key, ctx.user_data))
            else:
                instructions = (
                    "👻 **Instagram Story Detected**\n\n"
                    f"Couldn't resolve this account to build a data link:\n`{lookup_error}`"
                )
            await msg.edit_text(instructions, parse_mode="Markdown", disable_web_page_preview=True)
            return

        # ── Non-story private/rate-limited fallback ────────────────────────────
        api_url, _, _ = get_instagram_graphql_instructions(url)
        paste_url = generate_paste_link(url_key)
        reason_line = f"`{fetch_error}`" if fetch_error else "this post is likely private or rate-limited."
        instructions = (
            "🔒 **Private/Blocked Instagram Post**\n\n"
            f"Automatic fetch failed: {reason_line}\n\n"
            f"1. Open this link: [GraphQL Payload]({api_url})\n"
            "2. Select all text and copy (**Ctrl+A**, **Ctrl+C**).\n"
            f"3. Paste it in the [web paste page]({paste_url}) — works on mobile — "
            "and it'll continue automatically. (Pasting directly into this chat still works too.)"
        )
        asyncio.create_task(launch_paste_listener(msg, url_key, ctx.user_data))
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

def main() -> None:
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, handle_input))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_photo_pick, pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(handle_instagram_choice, pattern=r"^ig_choice\|"))
    app.add_handler(CallbackQueryHandler(handle_instagram_pick, pattern=r"^ig_pick\|"))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
