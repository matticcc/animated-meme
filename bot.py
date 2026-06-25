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
    # CRITICAL FIX: Added --allow-unplayable-formats to probe_url so TikTok photo and IG profiles pass safely
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

# ── Dynamic Instagram Parser (Both Public & Text-Pasted Private JSON) ───
def parse_and_download_instagram(target_data: str, url_key: str, choice: str = "all", is_raw_json: bool = False) -> list[Path]:
    downloaded_paths = []
    
    if is_raw_json:
        try:
            payload = json.loads(target_data)
            
            def find_media_blocks(data):
                blocks = []
                if isinstance(data, dict):
                    if "video_versions" in data or "image_versions2" in data or "video_url" in data or "display_url" in data:
                        if not any(k in data for k in ["shortcode_media", "xdt_api__v1__media__shortcode__web_info"]):
                            blocks.append(data)
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
                media_items = web_info.get("items", []) or payload.get("items", [])
                
            for idx, item in enumerate(media_items):
                if isinstance(item, dict) and "node" in item:
                    item = item["node"]
                    
                video_url = item.get("video_url")
                if not video_url and "video_versions" in item and item["video_versions"]:
                    video_url = item["video_versions"][0].get("url")
                    
                image_url = item.get("display_url")
                if not image_url and "image_versions2" in item:
                    cands = item["image_versions2"].get("candidates", [])
                    if cands:
                        image_url = cands[0].get("url")
                
                is_video = bool(video_url)
                if choice == "video" and not is_video:
                    continue
                if choice == "image" and is_video:
                    continue
                    
                final_url = video_url if is_video else image_url
                if not final_url:
                    continue
                    
                ext = "mp4" if is_video else "jpg"
                file_path = DOWNLOAD_DIR / f"{url_key}_{idx}.{ext}"
                
                urllib.request.urlretrieve(final_url, file_path)
                downloaded_paths.append(file_path)
                
            return downloaded_paths
        except Exception as e:
            raise RuntimeError(f"Failed to read paste payload: {e}")

    # Standard engine downloader path for Public links
    out = str(DOWNLOAD_DIR / f"{url_key}_%(index)s.%(ext)s")
    dl_args = base_args(target_data) + [
        "--allow-unplayable-formats",
        "--no-playlist",
        "-o", out,
    ]
    
    if choice == "video":
        dl_args += ["-f", "bestvideo+bestaudio/best"]
    elif choice == "image":
        dl_args += ["-f", "all"]
        
    dl_args.append(target_data)
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

def download_images_sync(url: str, url_key: str) -> list[Path]:
    out = str(DOWNLOAD_DIR / f"{url_key}_%(index)s.%(ext)s")
    dl_args = base_args(url) + [
        "--allow-unplayable-formats", 
        "--no-playlist",
        "-o", out,
        url,
    ]
    run_ytdlp(dl_args)
    files = list(DOWNLOAD_DIR.glob(f"{url_key}_*"))
    return [f for f in files if f.suffix.lower() in IMAGE_EXTS]

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
            for i in range(0, len(media_group), 10):
                await message.reply_media_group(media=media_group[i : i + 10])
        finally:
            for f in opened_files:
                f.close()

# ── Telegram Handlers ──────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 **Welcome!** Send me a public video or Instagram/TikTok link.\n\n"
        "🔒 **Private Posts Feature:**\n"
        "1. Open the private query URL on your logged-in browser.\n"
        "2. **Copy the whole page text** (the raw JSON layout code).\n"
        "3. Paste and send that text block directly into this chat!",
        parse_mode="Markdown"
    )
    
async def handle_instagram_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send your link/data again.")
        return
        
    is_raw = data.get("is_raw", False)
    target_payload = data["raw_json"] if is_raw else data["url"]
    
    await query.edit_message_text("⬇️ Compiling assets... please wait.")
    
    try:
        files = await asyncio.get_event_loop().run_in_executor(
            None, parse_and_download_instagram, target_payload, url_key, choice, is_raw
        )
    except Exception as e:
        await query.edit_message_text(f"❌ *Processing error:*\n`{str(e)}`", parse_mode="Markdown")
        return
        
    if not files:
        await query.edit_message_text("❌ No media matches found. If it's private, confirm your browser text block copy is complete.")
        return
        
    images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    videos = [f for f in files if f.suffix.lower() not in IMAGE_EXTS]
    
    await query.edit_message_text("📤 Uploading media files to Telegram...")
    
    try:
        if images:
            await send_photos(query.message, images)
        for vid in videos:
            with open(vid, "rb") as f:
                await query.message.reply_video(video=f, supports_streaming=True)
        await query.delete_message()
    except Exception as e:
        await query.edit_message_text(f"❌ Upload breakdown error: `{e}`", parse_mode="Markdown")
    finally:
        for fp in files:
            fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)
        
async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    
    if text.startswith("{") and ("xdt_api" in text or "shortcode_media" in text or '"data"' in text):
        msg = await update.message.reply_text("⚙️ **Valid raw data layout matched.** parsing structure options...", parse_mode="Markdown")
        url_key = str(msg.message_id)
        ctx.user_data[url_key] = {"raw_json": text, "is_raw": True}
        await msg.edit_text(
            "🔒 **Private JSON Content Extracted**\nSelect media components to fetch from your browser session payload:",
            reply_markup=build_instagram_carousel_keyboard(url_key),
            parse_mode="Markdown"
        )
        return

    url = extract_url(text)
    if not url:
        return
        
    if not is_allowed_site(url):
        await update.message.reply_text("❌ Unsupported website link layout.")
        return
        
    site = detect_site(url)
    msg = await update.message.reply_text(f"🔍 Analyzing **{site}** content link...", parse_mode="Markdown")
    
    if "instagram.com" in url:
        url_key = str(msg.message_id)
        ctx.user_data[url_key] = {"url": url, "is_raw": False}
        await msg.edit_text(
            f"📦 **{site} Media Formats Discovered**\nChoose how you want to handle this layout profile split:",
            reply_markup=build_instagram_carousel_keyboard(url_key),
            parse_mode="Markdown"
        )
        return

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
    except Exception as e:
        await msg.edit_text(f"❌ *Error:*\n`{clean_errors(str(e))}`", parse_mode="Markdown")
        return

    # Image-capable sites like TikTok /photo/ structures handler route
    if is_image_capable(url) and (info.get("_type") == "playlist" or "formats" not in info or not info.get("formats")):
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
            await msg.edit_text("❌ No photos discovered or media extraction failed.")
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

# ... [Keep your imports, config, and helper functions identical] ...

def get_instagram_api_url(url: str) -> str | None:
    """Extracts the shortcode from an Instagram URL and returns the clean API endpoint."""
    # Matches /p/abcde/, /reel/abcde/, /tv/abcde/, or share links
    match = re.search(r"instagram\.com/(?:p|reel|tv|share/v)/([^/?#&]+)", url)
    if not match:
        return None
    shortcode = match.group(1)
    # This endpoint returns the clean JSON data layout for the post
    return f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"

# ── Telegram Handlers ──────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 **Welcome!** Send me any public video link (YouTube, TikTok, Reddit).\n\n"
        "🔒 **For Private Instagram Posts:**\n"
        "Just send me the private Instagram link first! I will generate a secure data link for you to open in your browser.",
        parse_mode="Markdown"
    )

async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    
    # 1. Check if user is pasting raw JSON data back to us
    if text.startswith("{") and ("xdt_api" in text or "shortcode_media" in text or '"data"' in text or "items" in text):
        msg = await update.message.reply_text("⚙️ **Valid JSON payload matched.** Parsing structure options...", parse_mode="Markdown")
        url_key = str(msg.message_id)
        ctx.user_data[url_key] = {"raw_json": text, "is_raw": True}
        await msg.edit_text(
            "🔒 **Private Payload Processed**\nSelect what media components you want to extract from this data:",
            reply_markup=build_instagram_carousel_keyboard(url_key),
            parse_mode="Markdown"
        )
        return

    # 2. Otherwise, treat it as a standard URL extraction
    url = extract_url(text)
    if not url:
        return
        
    if not is_allowed_site(url):
        await update.message.reply_text("❌ Unsupported website link layout.")
        return
        
    site = detect_site(url)
    
    # Custom intercept block for Instagram (handling both Public and Private easily)
    if "instagram.com" in url:
        msg = await update.message.reply_text("🔍 Processing Instagram link...", parse_mode="Markdown")
        url_key = str(msg.message_id)
        ctx.user_data[url_key] = {"url": url, "is_raw": False}
        
        api_url = get_instagram_api_url(url)
        
        instructions = (
            f"📦 **Instagram Link Identified**\n\n"
            f"🔹 **If it's a PUBLIC post:**\n"
            f"Just choose an extraction type below to download directly.\n\n"
            f"🔒 **If it's a PRIVATE post:**\n"
            f"1. Click and open this link in your logged-in browser:\n`{api_url}`\n"
            f"2. Copy **everything** you see on that page (Ctrl+A then Ctrl+C).\n"
            f"3. **Paste that raw text block** right here into this chat."
        )
        
        await msg.edit_text(
            instructions,
            reply_markup=build_instagram_carousel_keyboard(url_key),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return

    # 3. Standard parsing fallback for TikTok, YouTube, etc.
    msg = await update.message.reply_text(f"🔍 Analyzing **{site}** content link...", parse_mode="Markdown")
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
    except Exception as e:
        await msg.edit_text(f"❌ *Error:*\n`{clean_errors(str(e))}`", parse_mode="Markdown")
        return

    # Image-capable sites handler route (e.g., TikTok photos)
    if is_image_capable(url) and (info.get("_type") == "playlist" or "formats" not in info or not info.get("formats")):
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
            await msg.edit_text("❌ No photos discovered or media extraction failed.")
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

# ... [Keep everything else below this exactly the same] ...

async def handle_photo_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired.")
        return
        
    all_files = [Path(p) for p in data.get("files", []) if Path(p).exists()]
    if not all_files:
        await query.edit_message_text("❌ Files no longer available.")
        return
        
    selected = all_files if choice == "all" else [all_files[int(choice)]] if int(choice) < len(all_files) else all_files
    await query.edit_message_text(f"📤 Uploading {len(selected)} image(s)...")
    try:
        await send_photos(query.message, selected)
        await query.delete_message()
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error: `{e}`")
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
        await query.edit_message_text("❌ Session expired.")
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
        
    await query.edit_message_text("⬇️ Downloading video stream…")
    
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
        await query.edit_message_text("❌ File not found after streaming compilation.")
        return
        
    size_bytes = filepath.stat().st_size
    if size_bytes > TELEGRAM_MAX_BYTES:
        filepath.unlink(missing_ok=True)
        await query.edit_message_text(f"❌ File too large ({size_bytes // 1024 // 1024} MB). Pick a lower layout quality.")
        return
        
    await query.edit_message_text("📤 Dispatching media track payload...")
    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await query.message.reply_audio(audio=f, filename=filepath.name, read_timeout=300, write_timeout=300)
            else:
                await query.message.reply_video(video=f, filename=filepath.name, supports_streaming=True, read_timeout=300, write_timeout=300)
        await query.delete_message()
    except Exception as e:
        await query.edit_message_text(f"❌ Upload handling error: `{e}`")
    finally:
        filepath.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)

def drop_existing_session() -> None:
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    for label, url in (
        ("deleteWebhook", f"{base}/deleteWebhook?drop_pending_updates=true"),
        ("getUpdates", f"{base}/getUpdates?offset=-1&limit=1&timeout=0"),
    ):
        try:
            urllib.request.urlopen(url, timeout=10)
            print(f"✅ Pre-start {label} OK")
        except Exception:
            pass

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")
        
    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"✅ Health server on :{PORT}")
    
    cp = get_cookies_path()
    print(f"✅ Cookies file loaded: {cp}" if cp else "⚠️ No cookies file supplied")
    
    drop_existing_session()
    time.sleep(1)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_photo_pick, pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(handle_instagram_choice, pattern=r"^ig_choice\|"))
    
    print("🤖 Bot online and ready...")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
