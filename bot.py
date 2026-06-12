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
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
PORT         = int(os.environ.get("PORT", "10000"))
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdlp_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

COOKIES_FILE = Path("/etc/secrets/youtube_cookies.txt")

MAX_FILESIZE_MB    = 50
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

YOUTUBE_DOMAINS = {"youtube.com", "youtu.be"}

# Height → yt-dlp format selector that reliably works on YouTube
# Uses height filter, not specific format IDs, so it always resolves
QUALITY_PRESETS = [
    ("4K (2160p)",  "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"),
    ("1440p",       "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best"),
    ("1080p",       "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"),
    ("720p",        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best"),
    ("480p",        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best"),
    ("360p",        "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best"),
]


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


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    """Args added to every yt-dlp call."""
    args = ["--no-warnings"]
    if is_youtube(url) and COOKIES_FILE.exists():
        args += ["--cookies", str(COOKIES_FILE)]
    return args

def run_ytdlp(args: list[str]) -> tuple[str, str, int]:
    result = subprocess.run(
        ["yt-dlp"] + args,
        capture_output=True, text=True, timeout=180,
    )
    return result.stdout, result.stderr, result.returncode

def clean_errors(stderr: str) -> str:
    errors = [l for l in stderr.splitlines() if "ERROR" in l]
    return "\n".join(errors) if errors else stderr.strip()

def get_available_heights(url: str) -> list[int]:
    """
    Fetch video info and return the list of available heights,
    filtered to only those that have at least one downloadable format.
    """
    stdout, stderr, code = run_ytdlp(base_args(url) + [
        "-J", "--no-playlist", url
    ])
    if code != 0:
        raise RuntimeError(clean_errors(stderr))

    info = json.loads(stdout)
    heights: set[int] = set()
    for f in info.get("formats", []):
        h = f.get("height")
        # only real video formats
        if h and f.get("vcodec", "none") not in ("none", None, ""):
            heights.add(int(h))
    return sorted(heights, reverse=True)

def build_keyboard(url_key: str, heights: list[int]) -> InlineKeyboardMarkup:
    """
    Show one button per quality tier that the video actually has.
    Uses yt-dlp height-filter selectors — no specific format IDs involved.
    """
    buttons = []

    # Map each available height to the right preset label
    for label, selector in QUALITY_PRESETS:
        # Extract the height cap from the preset (e.g. 1080 from height<=1080)
        m = re.search(r"height<=(\d+)", selector)
        if not m:
            continue
        cap = int(m.group(1))
        # Show this tier if the video has at least one format at or below cap
        if any(h <= cap for h in heights):
            idx = len(buttons)
            buttons.append([InlineKeyboardButton(
                label, callback_data=f"dl|{url_key}|{idx}"
            )])

    # Always show "Best available" and "Audio only"
    buttons.insert(0, [InlineKeyboardButton(
        "⭐ Best available", callback_data=f"dl|{url_key}|best"
    )])
    buttons.append([InlineKeyboardButton(
        "🎵 Audio only (best)", callback_data=f"dl|{url_key}|audio"
    )])
    return InlineKeyboardMarkup(buttons)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cookies_ok = "✅ YouTube cookies loaded" if COOKIES_FILE.exists() \
                 else "⚠️ No YouTube cookies — use /setcookies if YouTube fails"
    await update.message.reply_text(
        "👋 *Welcome!*\n\n"
        "Send me any video URL and I'll let you pick the quality.\n"
        "🎵 Audio is always at best quality.\n"
        f"⚠️ Max file size: {MAX_FILESIZE_MB} MB\n\n{cookies_ok}",
        parse_mode="Markdown",
    )

async def handle_setcookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg.document and msg.document.file_name.endswith(".txt"):
        f = await msg.document.get_file()
        await f.download_to_drive(str(COOKIES_FILE))
        await msg.reply_text("✅ Cookies saved! YouTube should work now.")
    else:
        await msg.reply_text(
            "Send a `cookies.txt` file *as a document* and reply to it with `/setcookies`.\n\n"
            "Export with the *Get cookies.txt LOCALLY* Chrome extension on youtube.com.",
            parse_mode="Markdown",
        )

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text("❌ No URL found in your message.")
        return

    site = detect_site(url)
    msg  = await update.message.reply_text(
        f"🔍 Fetching quality options from *{site}*…", parse_mode="Markdown"
    )

    try:
        heights = await asyncio.get_event_loop().run_in_executor(
            None, get_available_heights, url
        )
    except Exception as e:
        err  = str(e)
        hint = "\n\n💡 Use /setcookies to upload YouTube cookies." \
               if ("Sign in" in err or "bot" in err.lower()) else ""
        await msg.edit_text(f"❌ *Error:*\n`{err}`{hint}", parse_mode="Markdown")
        return

    url_key = str(msg.message_id)
    ctx.user_data[url_key] = {"url": url, "heights": heights}

    # Also store the ordered preset selectors so handle_download can look them up
    preset_list = []
    for label, selector in QUALITY_PRESETS:
        m = re.search(r"height<=(\d+)", selector)
        if m and any(h <= int(m.group(1)) for h in heights):
            preset_list.append(selector)
    ctx.user_data[url_key]["presets"] = preset_list

    await msg.edit_text(
        f"📺 *{site}* — choose quality:\n_(audio always at best quality)_",
        reply_markup=build_keyboard(url_key, heights),
        parse_mode="Markdown",
    )

async def handle_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
        fmt_arg       = "bestaudio/best"
        is_audio_only = True
    elif choice == "best":
        fmt_arg       = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        is_audio_only = False
    else:
        idx = int(choice)
        if idx < len(presets):
            fmt_arg = presets[idx]
        else:
            fmt_arg = "bestvideo+bestaudio/best"
        is_audio_only = False

    await query.edit_message_text("⬇️ Downloading… please wait.")

    out_tmpl = str(DOWNLOAD_DIR / f"{url_key}_%(title).60s.%(ext)s")
    dl_args  = base_args(url) + [
        "-f", fmt_arg,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", out_tmpl,
        url,
    ]
    if is_audio_only:
        dl_args += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]

    try:
        _, stderr, code = await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ytdlp(dl_args)
        )
        if code != 0:
            raise RuntimeError(clean_errors(stderr))
    except Exception as e:
        err  = str(e)
        hint = "\n\n💡 Use /setcookies to upload YouTube cookies." \
               if ("Sign in" in err or "bot" in err.lower()) else ""
        await query.edit_message_text(
            f"❌ *Download error:*\n`{err}`{hint}", parse_mode="Markdown"
        )
        return

    files = list(DOWNLOAD_DIR.glob(f"{url_key}_*"))
    if not files:
        await query.edit_message_text("❌ File not found after download.")
        return

    filepath = files[0]
    if filepath.stat().st_size > TELEGRAM_MAX_BYTES:
        filepath.unlink(missing_ok=True)
        await query.edit_message_text(
            f"❌ File too large for Telegram (>{MAX_FILESIZE_MB} MB). Try a lower quality."
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

    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"✅ Health server on :{PORT}")
    print(f"{'✅ Cookies loaded' if COOKIES_FILE.exists() else '⚠️  No cookies file'}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("help",       start))
    app.add_handler(CommandHandler("setcookies", handle_setcookies))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(filters.Document.MimeType("text/plain"), handle_setcookies))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl\|"))

    print("🤖 Bot polling…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
