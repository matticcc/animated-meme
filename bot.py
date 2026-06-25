import os
import re
import json
import asyncio
import tempfile
import subprocess
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse
import urllib.request

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
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdlp_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

_RENDER_COOKIES = Path("/etc/secrets/youtube_cookies.txt")
_RUNTIME_COOKIES = DOWNLOAD_DIR / "youtube_cookies.txt"

MAX_FILESIZE_MB = 500
TELEGRAM_MAX_BYTES = MAX_FILESIZE_MB * 1024 * 1024

KNOWN_SITES: dict[str, str] = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "reddit.com": "Reddit",
    "redgifs.com": "RedGifs",
    "instagram.com": "Instagram",
}

ALLOWED_DOMAINS = set(KNOWN_SITES.keys())
YOUTUBE_DOMAINS = {"youtube.com", "youtu.be"}
IMAGE_CAPABLE_SITES = {"tiktok.com", "instagram.com"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

QUALITY_PRESETS = [
    ("4K (2160p)", "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"),
    ("1440p", "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best"),
    ("1080p", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"),
    ("720p", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best"),
    ("480p", "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best"),
    ("360p", "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best"),
]

# ── Cookie helpers ─────────────────────────────────────────────────────────────
def get_cookies_path() -> Path | None:
    if _RENDER_COOKIES.exists():
        return _RENDER_COOKIES
    if _RUNTIME_COOKIES.exists():
        return _RUNTIME_COOKIES
    return None

# ── Health-check server ───────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def run_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()

# ── Download helpers ───────────────────────────────────────────────────────────
def is_allowed_site(url: str) -> bool:
    if "graphql/query" in url or "doc_id=" in url:
        return True
    return any(d in url.lower() for d in ALLOWED_DOMAINS)

def is_image_capable(url: str) -> bool:
    if "graphql/query" in url or "doc_id=" in url:
        return True
    return any(d in url.lower() for d in IMAGE_CAPABLE_SITES)

def detect_site(url: str) -> str:
    if "graphql/query" in url or "doc_id=" in url:
        return "Instagram Private API"
    for domain, name in KNOWN_SITES.items():
        if domain in url.lower():
            return name
    return "Unknown site"

def extract_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s]+", text)
    return m.group(0) if m else None

def base_args(url: str) -> list[str]:
    args = ["--no-warnings"]
    cookies = get_cookies_path()
    if cookies:
        args += ["--cookies", str(cookies)]
    return args

def run_ytdlp(args: list[str]) -> tuple[str, str, int]:
    result = subprocess.run(
        ["yt-dlp"] + args,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.stdout, result.stderr, result.returncode

def clean_errors(err: str) -> str:
    lines = [line.strip() for line in err.splitlines() if line.strip()]
    cleaned = []
    for line in lines:
        if line.lower().startswith("error:"):
            cleaned.append(line)
    if not cleaned and lines:
        cleaned.append(lines[-1])
    return "\n".join(cleaned) if cleaned else "Unknown extraction error."

def probe_url(url: str) -> dict:
    if "graphql/query" in url or "doc_id=" in url:
        return {"_type": "playlist", "entries": [{"is_live": False}]}
    args = base_args(url) + ["-j", "--no-playlist", "--allow-unplayable-formats", url]
    stdout, stderr, code = run_ytdlp(args)
    if code != 0:
        raise RuntimeError(stderr)
    return json.loads(stdout)

def get_available_heights(info: dict) -> list[int]:
    formats = info.get("formats", [])
    heights = set()
    for f in formats:
        h = f.get("height")
        if h and isinstance(h, int):
            heights.add(h)
    return sorted(list(heights), reverse=True)

def find_downloaded_file(url_key: str) -> Path | None:
    for f in DOWNLOAD_DIR.glob(f"{url_key}_*"):
        if f.suffix.lower() not in IMAGE_EXTS:
            return f
    return None

# ── Dynamic Instagram/GraphQL Downloader Function ──────────────────────────────
def download_instagram_media_sync(url: str, url_key: str, choice: str = "all") -> list[Path]:
    out = str(DOWNLOAD_DIR / f"{url_key}_%(index)s.%(ext)s")
    
    # ── FEATURE: Direct Private Raw Data GraphQL link Handling ──
    if "graphql/query" in url or "doc_id=" in url:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*"
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            
            # Recursive helper to find any inner media structures safely inside the json block
            def extract_media_nodes(data):
                nodes = []
                if isinstance(data, dict):
                    # Check if this dictionary itself describes a single media asset or carousel item
                    if "video_url" in data or "display_url" in data or "image_versions2" in data:
                        # Ensure it's a leaf node containing actual media data, not a parent wrapper
                        if not any(k in data for k in ["shortcode_media", "xdt_api__v1__media__shortcode__web_info"]):
                            nodes.append(data)
                    
                    # Traverse down nested arrays or child layouts 
                    for k, v in data.items():
                        if k in ["carousel_media", "edge_sidecar_to_children", "edges"]:
                            if isinstance(v, list):
                                for item in v:
                                    nodes.extend(extract_media_nodes(item))
                            elif isinstance(v, dict):
                                nodes.extend(extract_media_nodes(v))
                        else:
                            nodes.extend(extract_media_nodes(v))
                elif isinstance(data, list):
                    for item in data:
                        nodes.extend(extract_media_nodes(item))
                return nodes

            # Extract every available media item block found in the JSON payload
            items_to_download = extract_media_nodes(payload)
            
            # Fallback if recursive extraction was too strict: try top level items list
            if not items_to_download:
                data_root = payload.get("data", {})
                web_info = data_root.get("xdt_api__v1__media__shortcode__web_info", {})
                items_to_download = web_info.get("items", []) or payload.get("items", [])

            downloaded_paths = []
            for index, media in enumerate(items_to_download):
                if isinstance(media, dict) and "node" in media:
                    media = media["node"]

                # Resolve video urls
                video_url = media.get("video_url")
                if not video_url and "video_versions" in media and media["video_versions"]:
                    video_url = media["video_versions"][0].get("url")
                
                # Resolve image urls
                image_url = media.get("display_url")
                if not image_url and "image_versions2" in media:
                    candidates = media["image_versions2"].get("candidates", [])
                    if candidates:
                        image_url = candidates[0].get("url")

                is_video = bool(video_url)
                if choice == "video" and not is_video:
                    continue
                if choice == "image" and is_video:
                    continue
                    
                target_cdn = video_url if is_video else image_url
                if not target_cdn:
                    continue
                    
                ext = "mp4" if is_video else "jpg"
                file_path = DOWNLOAD_DIR / f"{url_key}_{index}.{ext}"
                
                urllib.request.urlretrieve(target_cdn, file_path)
                downloaded_paths.append(file_path)
                
            if downloaded_paths:
                return downloaded_paths
                
        except Exception as e:
            raise RuntimeError(f"Private GraphQL data structural parsing failure: {e}")

    # Standard fallback path for public posts / carousel parameters using yt-dlp
    dl_args = base_args(url) + [
        "--allow-unplayable-formats",
        "--no-playlist",
        "-o", out,
    ]
    
    if choice == "video":
        dl_args += ["-f", "bv*+ba/b"]
    elif choice == "image":
        dl_args += ["-f", "worst"]
        
    dl_args.append(url)
    run_ytdlp(dl_args)
    
    files = list(DOWNLOAD_DIR.glob(f"{url_key}_*"))
    filtered_files = []
    
    for f in files:
        is_img = f.suffix.lower() in IMAGE_EXTS
        if choice == "image" and is_img:
            filtered_files.append(f)
        elif choice == "video" and not is_img:
            filtered_files.append(f)
        elif choice == "all":
            filtered_files.append(f)
        else:
            f.unlink(missing_ok=True)
            
    return filtered_files

def run_instagram_download(url: str, url_key: str, choice: str = "all") -> list[Path]:
    # Wrapper function for the event loop task runner executor
    return download_instagram_media_sync(url, url_key, choice)

def download_images_sync(url: str, url_key: str) -> list[Path]:
    # General image engine downloader
    return download_instagram_media_sync(url, url_key, "all")

# ── UI Builders ───────────────────────────────────────────────────────────────
def build_video_keyboard(url_key: str, heights: list[int]) -> InlineKeyboardMarkup:
    buttons = []
    for h in heights:
        label = f"{h}p"
        for preset_label, selector in QUALITY_PRESETS:
            if f"height<={h}" in selector:
                label = preset_label
                break
        idx = next((i for i, (_, s) in enumerate(QUALITY_PRESETS) if f"height<={h}" in s), None)
        if idx is not None:
            buttons.append(InlineKeyboardButton(label, callback_data=f"dl|{url_key}|{idx}"))
    buttons.append(InlineKeyboardButton("Best Quality", callback_data=f"dl|{url_key}|best"))
    buttons.append(InlineKeyboardButton("Audio Only (MP3)", callback_data=f"dl|{url_key}|audio"))
    
    grid = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(grid)

def build_photo_picker(url_key: str, count: int) -> InlineKeyboardMarkup:
    buttons = []
    for i in range(count):
        buttons.append(InlineKeyboardButton(f"Photo {i+1}", callback_data=f"pick|{url_key}|{i}"))
    grid = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    grid.append([InlineKeyboardButton("✨ Send All Photos", callback_data=f"pick|{url_key}|all")])
    return InlineKeyboardMarkup(grid)

def build_instagram_carousel_keyboard(url_key: str) -> InlineKeyboardMarkup:
    # Custom interface providing image vs video splits matching requested framework architecture
    buttons = [
        [InlineKeyboardButton("📸 Download Images Only", callback_data=f"ig_choice|{url_key}|image")],
        [InlineKeyboardButton("🎥 Download Videos Only", callback_data=f"ig_choice|{url_key}|video")],
        [InlineKeyboardButton("📦 Download Everything", callback_data=f"ig_choice|{url_key}|all")]
    ]
    return InlineKeyboardMarkup(buttons)

async def send_photos(message, files: list[Path]) -> None:
    if len(files) == 1:
        with open(files[0], "rb") as f:
            await message.reply_photo(photo=f)
    else:
        media_group = []
        opened_files = []
        try:
            for fp in files:
                f = open(fp, "rb")
                opened_files.append(f)
                media_group.append(InputMediaPhoto(media=f))
            
            # Send chunks of 10 max
            for i in range(0, len(media_group), 10):
                await message.reply_media_group(media=media_group[i : i + 10])
        finally:
            for f in opened_files:
                f.close()

# ── Telegram Handlers ──────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 **Welcome!** Send me a supported media link to download video or audio.\n\n"
        "✨ **Instagram Carousels:** Send any public post link and choose your preferred media split format!\n"
        "🔒 **Private Instagram Posts:** Send the raw `/graphql/query/` content link from your browser dev console to scrape private feeds directly.",
        parse_mode="Markdown"
    )

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    url = extract_url(text)
    
    if not url:
        return
        
    if not is_allowed_site(url):
        await update.message.reply_text("❌ Unsupported website link configuration.")
        return
        
    site = detect_site(url)
    msg = await update.message.reply_text(f"🔍 Analyzing **{site}** content link...", parse_mode="Markdown")
    
    # Intercept Instagram posts to offer filtering logic configurations
    if "instagram.com" in url or "graphql/query" in url or "doc_id=" in url:
        url_key = str(msg.message_id)
        ctx.user_data[url_key] = {"url": url}
        await msg.edit_text(
            f"📦 **{site} Post Detected**\nChoose how you want to download this carousel configuration choice:",
            reply_markup=build_instagram_carousel_keyboard(url_key),
            parse_mode="Markdown"
        )
        return

    # Standard public route logic matching initial script architecture profiles
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
    except Exception as e:
        await msg.edit_text(f"❌ *Error:*\n`{clean_errors(str(e))}`", parse_mode="Markdown")
        return
        
    if is_image_capable(url) and info.get("_type") == "playlist":
        await msg.edit_text("⬇️ Extracting static media components...")
        url_key = str(msg.message_id)
        try:
            files = await asyncio.get_event_loop().run_in_executor(
                None, download_images_sync, url, url_key
            )
        except Exception as e:
            await msg.edit_text(f"❌ *Extraction error:*\n`{str(e)}`", parse_mode="Markdown")
            return
            
        if not files:
            # Re-verify layout fallback configuration states
            pass
        else:
            await msg.edit_text("📤 Uploading assets…")
            try:
                await send_photos(update.message, files)
                await msg.delete()
            except (TimedOut, NetworkError):
                await msg.edit_text("❌ Upload task timed out. Try again.")
            except Exception as e:
                await msg.edit_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
            finally:
                for fp in files:
                    fp.unlink(missing_ok=True)
                ctx.user_data.pop(url_key, None)
            return

    url_key = str(msg.message_id)
    heights = get_available_heights(info)
    preset_selectors = [
        selector for label, selector in QUALITY_PRESETS 
        if (m := re.search(r"height<=(\d+)", selector)) and any(h <= int(m.group(1)) for h in heights)
    ]
    ctx.user_data[url_key] = {"url": url, "type": "video", "presets": preset_selectors}
    await msg.edit_text(
        f"📺 *{site}* — choose quality:\n_(audio always at best quality)_",
        reply_markup=build_video_keyboard(url_key, heights),
        parse_mode="Markdown",
    )

async def handle_instagram_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send the URL again.")
        return
        
    url = data["url"]
    await query.edit_message_text("⬇️ Extracting Instagram media profile choices...")
    
    try:
        files = await asyncio.get_event_loop().run_in_executor(
            None, run_instagram_download, url, url_key, choice
        )
    except Exception as e:
        await query.edit_message_text(f"❌ *Download error:*\n`{str(e)}`", parse_mode="Markdown")
        return
        
    if not files:
        await query.edit_message_text("❌ No media files found matching your criteria profile setup.")
        return
        
    # Classify file group allocations (Single mixed or image layout profiles)
    images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    videos = [f for f in files if f.suffix.lower() not in IMAGE_EXTS]
    
    await query.edit_message_text("📤 Dispatching media assets onto Telegram pipeline server...")
    
    try:
        if images:
            await send_photos(query.message, images)
        for vid in videos:
            with open(vid, "rb") as f:
                await query.message.reply_video(video=f, supports_streaming=True)
        await query.delete_message()
    except Exception as e:
        await query.edit_message_text(f"❌ Processing upload mismatch logic: `{e}`", parse_mode="Markdown")
    finally:
        for fp in files:
            fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)

async def handle_photo_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send the URL again.")
        return
        
    all_files = [Path(p) for p in data.get("files", [])]
    all_files = [p for p in all_files if p.exists()]
    if not all_files:
        await query.edit_message_text("❌ Files no longer available. Send the URL again.")
        return
        
    if choice == "all":
        selected = all_files
    else:
        idx = int(choice)
        selected = [all_files[idx]] if idx < len(all_files) else all_files
        
    await query.edit_message_text(f"📤 Uploading {len(selected)} photo(s)…")
    try:
        await send_photos(query.message, selected)
        await query.delete_message()
    except (TimedOut, NetworkError):
        await query.edit_message_text("❌ Upload timed out. Please try again.")
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        for fp in all_files:
            fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)

async def handle_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send the URL again.")
        return
        
    url = data["url"]
    presets = data.get("presets", [])
    
    if choice == "audio":
        fmt_arg, is_audio = "bestaudio/best", True
    elif choice == "best":
        fmt_arg, is_audio = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best", False
    else:
        idx = int(choice)
        fmt_arg = presets[idx] if idx < len(presets) else "bestvideo+bestaudio/best"
        is_audio = False
        
    await query.edit_message_text("⬇️ Downloading… please wait.")
    
    out = str(DOWNLOAD_DIR / f"{url_key}_%(title).60s.%(ext)s")
    dl_args = base_args(url) + [
        "-f", fmt_arg,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", out,
        url,
    ]
    if is_audio:
        dl_args += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
        
    try:
        _, stderr, code = await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ytdlp(dl_args)
        )
        if code != 0:
            raise RuntimeError(clean_errors(stderr))
    except Exception as e:
        await query.edit_message_text(f"❌ *Download error:*\n`{str(e)}`", parse_mode="Markdown")
        return
        
    filepath = await asyncio.get_event_loop().run_in_executor(None, find_downloaded_file, url_key)
    if not filepath:
        await query.edit_message_text("❌ File not found after download.")
        return
        
    size_bytes = filepath.stat().st_size
    if size_bytes > TELEGRAM_MAX_BYTES:
        filepath.unlink(missing_ok=True)
        await query.edit_message_text(
            f"❌ File too large ({size_bytes // 1024 // 1024} MB > {MAX_FILESIZE_MB} MB). Try a lower quality."
        )
        return
        
    await query.edit_message_text("📤 Uploading to Telegram…")
    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await query.message.reply_audio(audio=f, filename=filepath.name, read_timeout=300, write_timeout=300, connect_timeout=60)
            else:
                await query.message.reply_video(video=f, filename=filepath.name, supports_streaming=True, read_timeout=300, write_timeout=300, connect_timeout=60)
        await query.delete_message()
    except (TimedOut, NetworkError):
        await query.edit_message_text(
            f"❌ Upload timed out ({size_bytes // 1024 // 1024} MB). Try a lower quality or retry."
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        filepath.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)

# ── Session cleanup ────────────────────────────────────────────────────────────
def drop_existing_session() -> None:
    import urllib.request
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    for label, url in (
        ("deleteWebhook", f"{base}/deleteWebhook?drop_pending_updates=true"),
        ("getUpdates", f"{base}/getUpdates?offset=-1&limit=1&timeout=0"),
    ):
        try:
            urllib.request.urlopen(url, timeout=10)
            print(f"✅ Pre-start {label} OK")
        except Exception as e:
            print(f"⚠️ Pre-start {label}: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")
        
    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"✅ Health server on :{PORT}")
    
    cp = get_cookies_path()
    print(f"✅ Cookies: {cp}" if cp else "⚠️ No cookies file — some sites may require login")
    
    print("🔄 Dropping any existing session…")
    drop_existing_session()
    time.sleep(2)
    
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_photo_pick, pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(handle_instagram_choice, pattern=r"^ig_choice\|"))
    
    print("🤖 Bot polling…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )

if __name__ == "__main__":
    main()
