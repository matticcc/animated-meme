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

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "10000"))   # Render injects $PORT
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdlp_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_FILESIZE_MB = 50
TELEGRAM_MAX_BYTES = MAX_FILESIZE_MB * 1024 * 1024

KNOWN_SITES: dict[str, str] = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "vimeo.com": "Vimeo",
    "twitter.com": "Twitter/X", "x.com": "Twitter/X",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "facebook.com": "Facebook", "fb.watch": "Facebook",
    "twitch.tv": "Twitch",
    "dailymotion.com": "Dailymotion",
    "reddit.com": "Reddit",
    "bilibili.com": "Bilibili",
    "soundcloud.com": "SoundCloud",
    "bandcamp.com": "Bandcamp",
    "rumble.com": "Rumble",
}


# ── Tiny health-check server (required by Render Web Service) ─────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # silence access logs


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()


# ── Helpers ───────────────────────────────────────────────────────────────────

def detect_site(url: str) -> str:
    url_lower = url.lower()
    for domain, name in KNOWN_SITES.items():
        if domain in url_lower:
            return name
    return "Unknown site"


def extract_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s]+", text)
    return match.group(0) if match else None


def run_ytdlp(args: list[str]) -> tuple[str, str, int]:
    result = subprocess.run(
        ["yt-dlp"] + args,
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout, result.stderr, result.returncode


def get_formats(url: str) -> list[dict]:
    stdout, stderr, code = run_ytdlp(["-J", "--no-playlist", url])
    if code != 0:
        raise RuntimeError(stderr.strip() or "yt-dlp failed to fetch info")

    info = json.loads(stdout)
    formats_raw = info.get("formats", [])

    seen_labels: set[str] = set()
    formats: list[dict] = []

    for f in formats_raw:
        vcodec = f.get("vcodec", "none")
        if vcodec in (None, "none", ""):
            continue
        height = f.get("height")
        if not height:
            continue

        fps = f.get("fps") or 0
        ext = f.get("ext", "mp4")
        fid = f.get("format_id", "")
        filesize = f.get("filesize") or f.get("filesize_approx") or 0

        fps_str = f"+{int(fps)}fps" if fps and fps > 30 else ""
        label = f"{height}p{fps_str}"

        if label in seen_labels:
            continue
        seen_labels.add(label)

        formats.append({
            "format_id": fid,
            "ext": ext,
            "height": height,
            "fps": fps,
            "vcodec": vcodec,
            "filesize": filesize,
            "label": label,
        })

    formats.sort(key=lambda x: (x["height"], x["fps"]), reverse=True)
    formats.insert(0, {
        "format_id": "bestvideo+bestaudio/best",
        "ext": "mp4",
        "height": 9999,
        "fps": 0,
        "label": "⭐ Best available",
    })
    return formats


def build_keyboard(formats: list[dict], url_key: str) -> InlineKeyboardMarkup:
    buttons = []
    for fmt in formats:
        callback = f"dl|{url_key}|{fmt['format_id']}"
        buttons.append([InlineKeyboardButton(fmt["label"], callback_data=callback)])
    buttons.append([InlineKeyboardButton(
        "🎵 Audio only (best)", callback_data=f"dl|{url_key}|bestaudio"
    )])
    return InlineKeyboardMarkup(buttons)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Welcome!*\n\n"
        "Send me any video URL (YouTube, TikTok, Instagram, Twitter, Vimeo…) "
        "and I'll let you pick the quality before downloading.\n\n"
        "🎵 Audio is always merged at best quality.\n"
        f"⚠️ Max file size: {MAX_FILESIZE_MB} MB (Telegram limit).",
        parse_mode="Markdown",
    )


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    url = extract_url(text)
    if not url:
        await update.message.reply_text("❌ I couldn't find a URL in your message.")
        return

    site = detect_site(url)
    status_msg = await update.message.reply_text(
        f"🔍 Fetching quality options from *{site}*…", parse_mode="Markdown"
    )

    try:
        formats = await asyncio.get_event_loop().run_in_executor(None, get_formats, url)
    except Exception as e:
        await status_msg.edit_text(f"❌ Error fetching formats:\n`{e}`", parse_mode="Markdown")
        return

    url_key = str(status_msg.message_id)
    ctx.user_data[url_key] = url
    keyboard = build_keyboard(formats, url_key)
    await status_msg.edit_text(
        f"📺 *{site}* — choose quality:\n_(audio always at best quality)_",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def handle_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, url_key, format_id = query.data.split("|", 2)
    url = ctx.user_data.get(url_key)
    if not url:
        await query.edit_message_text("❌ Session expired. Please send the URL again.")
        return

    await query.edit_message_text("⬇️ Downloading… please wait.")

    if format_id == "bestaudio":
        fmt_arg = "bestaudio/best"
        is_audio_only = True
    else:
        fmt_arg = format_id if ("+" in format_id or format_id.startswith("best")) \
                  else f"{format_id}+bestaudio/best"
        is_audio_only = False

    output_template = str(DOWNLOAD_DIR / f"{url_key}_%(title).60s.%(ext)s")
    ydl_args = [
        "-f", fmt_arg,
        "--merge-output-format", "mp3" if is_audio_only else "mp4",
        "--no-playlist", "--no-warnings",
        "-o", output_template,
        url,
    ]
    if is_audio_only:
        ydl_args += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]

    try:
        stdout, stderr, code = await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ytdlp(ydl_args)
        )
        if code != 0:
            raise RuntimeError(stderr.strip() or "Download failed")
    except Exception as e:
        await query.edit_message_text(f"❌ Download error:\n`{e}`", parse_mode="Markdown")
        return

    files = list(DOWNLOAD_DIR.glob(f"{url_key}_*"))
    if not files:
        await query.edit_message_text("❌ File not found after download.")
        return

    filepath = files[0]
    if filepath.stat().st_size > TELEGRAM_MAX_BYTES:
        filepath.unlink(missing_ok=True)
        size_mb = filepath.stat().st_size // (1024 * 1024) if filepath.exists() else "?"
        await query.edit_message_text(
            f"❌ File is too large. Telegram bots can only send up to {MAX_FILESIZE_MB} MB.\n"
            "Try a lower quality."
        )
        return

    await query.edit_message_text("📤 Uploading to Telegram…")
    try:
        with open(filepath, "rb") as f:
            if is_audio_only:
                await query.message.reply_audio(audio=f, filename=filepath.name)
            else:
                await query.message.reply_video(video=f, filename=filepath.name,
                                                supports_streaming=True)
        await query.delete_message()
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        filepath.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    # Start health-check server in a background thread
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print(f"✅ Health server listening on port {PORT}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl\|"))

    print("🤖 Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
