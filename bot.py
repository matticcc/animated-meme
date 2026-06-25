import os
import re
import json
import asyncio
import tempfile
import subprocess
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import FileSizeLimit
from telegram.error import TimedOut, NetworkError

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
PORT         = int(os.environ.get("PORT", "10000"))
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdlp_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Render Secret File path (set via dashboard → Secret Files) — internal only
_RENDER_COOKIES  = Path("/etc/secrets/youtube_cookies.txt")
_RUNTIME_COOKIES = DOWNLOAD_DIR / "youtube_cookies.txt"

MAX_FILESIZE_MB    = 500
TELEGRAM_MAX_BYTES = MAX_FILESIZE_MB * 1024 * 1024

# Sites users are allowed to submit
KNOWN_SITES: dict[str, str] = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "tiktok.com":  "TikTok",
    "reddit.com":  "Reddit",
    "redgifs.com": "RedGifs",
    "instagram.com": "Instagram",
}

ALLOWED_DOMAINS = set(KNOWN_SITES.keys())
YOUTUBE_DOMAINS = {"youtube.com", "youtu.be"}

# Sites that can return images instead of video
IMAGE_CAPABLE_SITES = {"tiktok.com", "instagram.com"}

QUALITY_PRESETS = [
    ("4K (2160p)", "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"),
    ("1440p",      "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best"),
    ("1080p",      "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"),
    ("720p",       "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best"),
    ("480p",       "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best"),
    ("360p",       "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best"),
]

# Image quality options (resolution label → yt-dlp format selector)
IMAGE_QUALITY_PRESETS = [
    ("Best image quality", "best"),
    ("Worst image quality", "worst"),
]


# ── Cookie helpers (internal, never shown to users) ───────────────────────────

def get_cookies_path() -> Path | None:
    if _RENDER_COOKIES.exists():
        return _RENDER_COOKIES
    if _RUNTIME_COOKIES.exists():
        return _RUNTIME_COOKIES
    return None


# ── Health-check server (Render requires an open port) ───────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def run_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


# ── yt-dlp helpers ────────────────────────────────────────────────────────────

def is_allowed_site(url: str) -> bool:
    return any(d in url.lower() for d in ALLOWED_DOMAINS)

def is_image_capable(url: str) -> bool:
    return any(d in url.lower() for d in IMAGE_CAPABLE_SITES)

def detect_site(url: str) -> str:
    for domain, name in KNOWN_SITES.items():
        if domain in url.lower():
            return name
    return "Unknown site"

def is_youtube(url: str) -> bool:
    return any(d in url.lower() for d in YOUTUBE_DOMAINS)

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
        capture_output=True, text=True,
        timeout=600,  # 10 minutes — large files need time
    )
    return result.stdout, result.stderr, result.returncode

def clean_errors(stderr: str) -> str:
    errors = [l for l in stderr.splitlines() if "ERROR" in l]
    return "\n".join(errors) if errors else stderr.strip()

def probe_url(url: str) -> dict:
    """Return yt-dlp JSON info for a URL. Raises RuntimeError on failure."""
    stdout, stderr, code = run_ytdlp(base_args(url) + ["-J", "--no-playlist", url])
    if code != 0:
        raise RuntimeError(clean_errors(stderr))
    return json.loads(stdout)

def is_image_post(info: dict) -> bool:
    """Return True when yt-dlp reports this as a still-image / photo post."""
    ext = info.get("ext", "")
    # yt-dlp marks image posts with ext=jpg/png/webp and no duration
    if ext in ("jpg", "jpeg", "png", "webp") and not info.get("duration"):
        return True
    # carousel / multi-photo: entries all image
    entries = info.get("entries", [])
    if entries and all(
        e.get("ext", "") in ("jpg", "jpeg", "png", "webp") and not e.get("duration")
        for e in entries
    ):
        return True
    return False

def get_available_heights(info: dict) -> list[int]:
    heights: set[int] = set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and f.get("vcodec", "none") not in ("none", None, ""):
            heights.add(int(h))
    return sorted(heights, reverse=True)

def find_downloaded_file(url_key: str) -> Path | None:
    """
    Glob for the output file, ignoring .part / .ytdl temp files.
    yt-dlp may take a moment to rename after merging, so retry briefly.
    """
    for _ in range(10):
        candidates = [
            p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
            if not p.suffix.endswith((".part", ".ytdl"))
        ]
        if candidates:
            return candidates[0]
        import time; time.sleep(0.5)
    return None

def build_video_keyboard(url_key: str, heights: list[int]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        "⭐ Best available", callback_data=f"dl|{url_key}|best"
    )]]
    for i, (label, selector) in enumerate(QUALITY_PRESETS):
        m = re.search(r"height<=(\d+)", selector)
        if m and any(h <= int(m.group(1)) for h in heights):
            buttons.append([InlineKeyboardButton(label, callback_data=f"dl|{url_key}|{i}")])
    buttons.append([InlineKeyboardButton(
        "🎵 Audio only (best)", callback_data=f"dl|{url_key}|audio"
    )])
    return InlineKeyboardMarkup(buttons)

def build_image_keyboard(url_key: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"img|{url_key}|{i}")]
        for i, (label, _) in enumerate(IMAGE_QUALITY_PRESETS)
    ]
    return InlineKeyboardMarkup(buttons)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Welcome!*\n\n"
        "Send me a link and I'll download it for you.\n\n"
        "*Supported sites:*\n"
        "• YouTube\n"
        "• TikTok _(videos & images)_\n"
        "• Reddit\n"
        "• RedGifs\n"
        "• Instagram _(videos & images)_\n\n"
        "🎵 Audio extraction is also available.\n"
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
            "❌ *Unsupported site.*\n\n"
            "I only accept links from:\n"
            "• YouTube\n"
            "• TikTok\n"
            "• Reddit\n"
            "• RedGifs\n"
            "• Instagram",
            parse_mode="Markdown",
        )
        return

    site = detect_site(url)
    msg = await update.message.reply_text(
        f"🔍 Fetching info from *{site}*…", parse_mode="Markdown"
    )

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
    except Exception as e:
        await msg.edit_text(f"❌ *Error:*\n`{clean_errors(str(e))}`", parse_mode="Markdown")
        return

    url_key = str(msg.message_id)

    # ── Image post path ──
    if is_image_capable(url) and is_image_post(info):
        ctx.user_data[url_key] = {"url": url, "type": "image"}
        await msg.edit_text(
            f"🖼 *{site}* — image post detected. Choose quality:",
            reply_markup=build_image_keyboard(url_key),
            parse_mode="Markdown",
        )
        return

    # ── Video path ──
    heights = get_available_heights(info)
    preset_selectors = []
    for label, selector in QUALITY_PRESETS:
        m = re.search(r"height<=(\d+)", selector)
        if m and any(h <= int(m.group(1)) for h in heights):
            preset_selectors.append(selector)

    ctx.user_data[url_key] = {"url": url, "type": "video", "presets": preset_selectors}
    await msg.edit_text(
        f"📺 *{site}* — choose quality:\n_(audio always at best quality)_",
        reply_markup=build_video_keyboard(url_key, heights),
        parse_mode="Markdown",
    )


async def handle_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles video/audio quality button presses."""
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
        await query.edit_message_text(
            f"❌ *Download error:*\n`{str(e)}`", parse_mode="Markdown"
        )
        return

    filepath = await asyncio.get_event_loop().run_in_executor(None, find_downloaded_file, url_key)
    if not filepath:
        await query.edit_message_text("❌ File not found after download.")
        return

    size_bytes = filepath.stat().st_size
    if size_bytes > TELEGRAM_MAX_BYTES:
        filepath.unlink(missing_ok=True)
        await query.edit_message_text(
            f"❌ File too large ({size_bytes // 1024 // 1024} MB > {MAX_FILESIZE_MB} MB limit). Try a lower quality."
        )
        return

    await query.edit_message_text("📤 Uploading to Telegram…")
    try:
        with open(filepath, "rb") as f:
            if is_audio:
                await query.message.reply_audio(
                    audio=f,
                    filename=filepath.name,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )
            else:
                await query.message.reply_video(
                    video=f,
                    filename=filepath.name,
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )
        await query.delete_message()
    except (TimedOut, NetworkError) as e:
        await query.edit_message_text(
            f"❌ Upload timed out — file was {size_bytes // 1024 // 1024} MB.\n"
            "Try a lower quality or try again.",
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        filepath.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)


async def handle_image_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles image quality button presses for TikTok/Instagram image posts."""
    query = update.callback_query
    await query.answer()

    _, url_key, choice_idx = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send the URL again.")
        return

    url = data["url"]
    _, fmt_selector = IMAGE_QUALITY_PRESETS[int(choice_idx)]

    await query.edit_message_text("⬇️ Downloading image(s)… please wait.")

    out = str(DOWNLOAD_DIR / f"{url_key}_%(title).60s_%(autonumber)s.%(ext)s")
    dl_args = base_args(url) + [
        "-f", fmt_selector,
        "--no-playlist",
        "-o", out,
        url,
    ]

    try:
        _, stderr, code = await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ytdlp(dl_args)
        )
        if code != 0:
            raise RuntimeError(clean_errors(stderr))
    except Exception as e:
        await query.edit_message_text(
            f"❌ *Download error:*\n`{str(e)}`", parse_mode="Markdown"
        )
        return

    # Collect all downloaded image files for this url_key
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted([
        p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
        if p.suffix.lower() in image_exts and not p.suffix.endswith((".part", ".ytdl"))
    ])

    if not files:
        # Fallback: maybe yt-dlp saved a video despite being an image post — handle gracefully
        files = [p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
                 if not p.suffix.endswith((".part", ".ytdl"))]

    if not files:
        await query.edit_message_text("❌ No files found after download.")
        return

    await query.edit_message_text(f"📤 Uploading {len(files)} file(s)…")
    try:
        from telegram import InputMediaPhoto, InputMediaVideo

        media_group = []
        video_files = []
        photo_files = []

        for fp in files:
            if fp.suffix.lower() in image_exts:
                photo_files.append(fp)
            else:
                video_files.append(fp)

        # Send photos as media group if multiple, single photo otherwise
        if photo_files:
            if len(photo_files) == 1:
                with open(photo_files[0], "rb") as f:
                    await query.message.reply_photo(
                        photo=f,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                    )
            else:
                # Telegram media group: max 10 per batch
                for batch_start in range(0, len(photo_files), 10):
                    batch = photo_files[batch_start:batch_start + 10]
                    opened = [open(fp, "rb") for fp in batch]
                    try:
                        media = [InputMediaPhoto(media=f) for f in opened]
                        await query.message.reply_media_group(
                            media=media,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
                        )
                    finally:
                        for f in opened:
                            f.close()

        for fp in video_files:
            with open(fp, "rb") as f:
                await query.message.reply_video(
                    video=f,
                    filename=fp.name,
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )

        await query.delete_message()
    except (TimedOut, NetworkError):
        await query.edit_message_text("❌ Upload timed out. Please try again.")
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        for fp in files:
            fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"✅ Health server on :{PORT}")

    cp = get_cookies_path()
    if cp:
        print(f"✅ Cookies: {cp}")
    else:
        print("⚠️  No cookies file found — some sites may require login")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_download,       pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_image_download, pattern=r"^img\|"))

    print("🤖 Bot polling…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
