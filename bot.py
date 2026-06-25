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
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
PORT         = int(os.environ.get("PORT", "10000"))
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdlp_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

_RENDER_COOKIES  = Path("/etc/secrets/youtube_cookies.txt")
_RUNTIME_COOKIES = DOWNLOAD_DIR / "youtube_cookies.txt"

MAX_FILESIZE_MB    = 500
TELEGRAM_MAX_BYTES = MAX_FILESIZE_MB * 1024 * 1024

KNOWN_SITES: dict[str, str] = {
    "youtube.com":   "YouTube",
    "youtu.be":      "YouTube",
    "tiktok.com":    "TikTok",
    "reddit.com":    "Reddit",
    "redgifs.com":   "RedGifs",
    "instagram.com": "Instagram",
}

ALLOWED_DOMAINS     = set(KNOWN_SITES.keys())
YOUTUBE_DOMAINS     = {"youtube.com", "youtu.be"}
IMAGE_CAPABLE_SITES = {"tiktok.com", "instagram.com"}
IMAGE_EXTS          = {".jpg", ".jpeg", ".png", ".webp"}

QUALITY_PRESETS = [
    ("4K (2160p)", "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"),
    ("1440p",      "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best"),
    ("1080p",      "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"),
    ("720p",       "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best"),
    ("480p",       "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best"),
    ("360p",       "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best"),
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
        capture_output=True, text=True, timeout=600,
    )
    return result.stdout, result.stderr, result.returncode

def run_gallerydl(url: str, out_dir: Path) -> tuple[str, str, int]:
    result = subprocess.run(
        ["gallery-dl", "--dest", str(out_dir), url],
        capture_output=True, text=True, timeout=300,
    )
    return result.stdout, result.stderr, result.returncode

def clean_errors(stderr: str) -> str:
    errors = [l for l in stderr.splitlines() if "ERROR" in l]
    return "\n".join(errors) if errors else stderr.strip()

def is_tiktok_photo_url(url: str) -> bool:
    return "tiktok.com" in url.lower() and "/photo/" in url.lower()

def probe_url(url: str) -> dict:
    """Return yt-dlp JSON info. TikTok /photo/ URLs go straight to gallery-dl."""
    if is_tiktok_photo_url(url):
        return {"_use_gallerydl": True, "url": url}
    stdout, stderr, code = run_ytdlp(base_args(url) + ["-J", url])
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
        if e.get("ext", "") in ("jpg", "jpeg", "png", "webp"):
            return True
        vcodec = e.get("vcodec", "none") or "none"
        return vcodec == "none" and not e.get("duration")
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
    """Find a single downloaded video file, waiting for yt-dlp to finish merging."""
    for _ in range(10):
        candidates = [
            p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
            if not p.name.endswith((".part", ".ytdl"))
        ]
        if candidates:
            return candidates[0]
        time.sleep(0.5)
    return None

def collect_image_files(url_key: str) -> list[Path]:
    """Return sorted list of downloaded image files for this url_key."""
    return sorted([
        p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
        if p.suffix.lower() in IMAGE_EXTS and not p.name.endswith((".part", ".ytdl"))
    ])

def download_images(url: str, url_key: str) -> list[Path]:
    """
    Downloads images/photos from supported sites (like Instagram or TikTok).
    """
    out = str(DOWNLOAD_DIR / f"{url_key}_%(index)s.%(ext)s")
    
    # Base arguments (includes --no-warnings and cookies if available)
    dl_args = base_args(url) + [
        "--skip-download",
        "--dump-json",
        "--no-playlist",
        url,
    ]
    
    try:
        stdout, stderr, code = run_ytdlp(dl_args)
        if code != 0:
            raise RuntimeError(stderr)
            
        # Parse the JSON to look for image entries
        urls = []
        if "\n" in stdout.strip():
            # Multiple lines (playlist/carousel items)
            for line in stdout.strip().split("\n"):
                if not line.strip():
                    continue
                data = json.loads(line)
                # Check for automatic captions/thumbnails or requested formats
                if "url" in data:
                    urls.append(data["url"])
        else:
            data = json.loads(stdout)
            if "url" in data:
                urls.append(data["url"])
                
    except Exception:
        # Fallback directly to downloading via yt-dlp with unplayable formats allowed
        pass

    # Core fix: Include --allow-unplayable-formats so yt-dlp grabs images
    dl_args = base_args(url) + [
        "--allow-unplayable-formats",  # <-- THIS IS THE CRITICAL FIX FOR INSTAGRAM PHOTOS
        "--no-playlist",
        "-o", out,
        url,
    ]
    
    stdout, stderr, code = run_ytdlp(dl_args)
    
    # Gather downloaded image files matching the pattern
    files = list(DOWNLOAD_DIR.glob(f"{url_key}_*"))
    # Filter only actual images
    files = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    
    if not files:
        raise RuntimeError("No photos found or post private on any link.")
        
    return files


# ── Keyboard builders ──────────────────────────────────────────────────────────

def build_video_keyboard(url_key: str, heights: list[int]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton("⭐ Best available", callback_data=f"dl|{url_key}|best")]]
    for i, (label, selector) in enumerate(QUALITY_PRESETS):
        m = re.search(r"height<=(\d+)", selector)
        if m and any(h <= int(m.group(1)) for h in heights):
            buttons.append([InlineKeyboardButton(label, callback_data=f"dl|{url_key}|{i}")])
    buttons.append([InlineKeyboardButton("🎵 Audio only (best)", callback_data=f"dl|{url_key}|audio")])
    return InlineKeyboardMarkup(buttons)

def build_photo_picker(url_key: str, count: int) -> InlineKeyboardMarkup:
    """Picker shown AFTER download, with actual file count."""
    buttons = [[InlineKeyboardButton("📸 All photos", callback_data=f"pick|{url_key}|all")]]
    for i in range(count):
        buttons.append([InlineKeyboardButton(f"Photo {i + 1}", callback_data=f"pick|{url_key}|{i}")])
    return InlineKeyboardMarkup(buttons)


# ── Telegram upload helpers ────────────────────────────────────────────────────

async def send_photos(message, files: list[Path]) -> None:
    if not files:
        return
    if len(files) == 1:
        with open(files[0], "rb") as f:
            await message.reply_photo(photo=f, read_timeout=120, write_timeout=120, connect_timeout=30)
        return
    for batch_start in range(0, len(files), 10):
        batch = files[batch_start:batch_start + 10]
        opened = [open(fp, "rb") for fp in batch]
        try:
            media = [InputMediaPhoto(media=fh) for fh in opened]
            await message.reply_media_group(media=media, read_timeout=120, write_timeout=120, connect_timeout=30)
        finally:
            for fh in opened:
                fh.close()


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Welcome!*\n\n"
        "Send me a link and I'll download it for you.\n\n"
        "\n"
        "Audio and images extraction is also available.\n"
        f"⚠️ Max file size: {MAX_FILESIZE_MB} MB",
        parse_mode="Markdown",
    )


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text("❌ No URL found in your message.")
        return

    if not is_allowed_site(url):
        await update.message.reply_text(
            "❌ *Unsupported site.*\n\nI only accept links from:\n"
            "• YouTube\n• TikTok\n• Reddit\n• RedGifs\n• Instagram",
            parse_mode="Markdown",
        )
        return

    site = detect_site(url)
    msg = await update.message.reply_text(f"🔍 Fetching info from *{site}*…", parse_mode="Markdown")

    # ── Image post: download immediately, then show picker ──
    if is_image_capable(url):
        try:
            info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
        except Exception as e:
            await msg.edit_text(f"❌ *Error:*\n`{clean_errors(str(e))}`", parse_mode="Markdown")
            return

        if is_image_post(info):
            url_key = str(msg.message_id)
            await msg.edit_text(f"⬇️ Downloading photos from *{site}*…", parse_mode="Markdown")

            try:
                files = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: download_images(url, url_key)
                )
            except Exception as e:
                await msg.edit_text(f"❌ *Download error:*\n`{str(e)}`", parse_mode="Markdown")
                return

            if not files:
                await msg.edit_text("❌ No photos found. The post may be private or unsupported.")
                return

            # Store file paths — they're already on disk, picker just sends them
            ctx.user_data[url_key] = {"files": [str(p) for p in files]}

            if len(files) == 1:
                # Only one photo: skip picker, send immediately
                await msg.edit_text("📤 Uploading…")
                try:
                    await send_photos(update.message, files)
                    await msg.delete()
                except (TimedOut, NetworkError):
                    await msg.edit_text("❌ Upload timed out. Please try again.")
                except Exception as e:
                    await msg.edit_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
                finally:
                    for fp in files:
                        fp.unlink(missing_ok=True)
                    ctx.user_data.pop(url_key, None)
            else:
                await msg.edit_text(
                    f"🖼 *{len(files)} photos* downloaded. Which do you want to send?",
                    reply_markup=build_photo_picker(url_key, len(files)),
                    parse_mode="Markdown",
                )
            return

        # Image-capable site but it's actually a video — fall through to video path
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
        return

    # ── Non-image-capable site: probe for video ──
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
    except Exception as e:
        await msg.edit_text(f"❌ *Error:*\n`{clean_errors(str(e))}`", parse_mode="Markdown")
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


async def handle_photo_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    User picked a photo (or all) from the post-download picker.
    callback_data: pick|<url_key>|all  or  pick|<url_key>|<0-based index>
    """
    query = update.callback_query
    await query.answer()

    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send the URL again.")
        return

    all_files = [Path(p) for p in data.get("files", [])]
    # Filter to files that still exist on disk
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
        # Clean up all files once user has made their pick (or on error)
        for fp in all_files:
            fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)


async def handle_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Video / audio quality button handler."""
    query = update.callback_query
    await query.answer()

    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send the URL again.")
        return

    url     = data["url"]
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
                await query.message.reply_audio(audio=f, filename=filepath.name,
                                                read_timeout=300, write_timeout=300, connect_timeout=60)
            else:
                await query.message.reply_video(video=f, filename=filepath.name,
                                                supports_streaming=True,
                                                read_timeout=300, write_timeout=300, connect_timeout=60)
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
        ("getUpdates",    f"{base}/getUpdates?offset=-1&limit=1&timeout=0"),
    ):
        try:
            urllib.request.urlopen(url, timeout=10)
            print(f"✅ Pre-start {label} OK")
        except Exception as e:
            print(f"⚠️  Pre-start {label}: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"✅ Health server on :{PORT}")

    cp = get_cookies_path()
    print(f"✅ Cookies: {cp}" if cp else "⚠️  No cookies file — some sites may require login")

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
    app.add_handler(CommandHandler("help",  start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_download,   pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_photo_pick, pattern=r"^pick\|"))

    print("🤖 Bot polling…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
