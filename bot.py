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

# Render Secret File path (set via dashboard → Secret Files)
_RENDER_COOKIES  = Path("/etc/secrets/youtube_cookies.txt")
# Writable fallback used when you upload via /setcookies
_RUNTIME_COOKIES = DOWNLOAD_DIR / "youtube_cookies.txt"

MAX_FILESIZE_MB    = 500
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

QUALITY_PRESETS = [
    ("4K (2160p)", "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best"),
    ("1440p",      "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best"),
    ("1080p",      "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best"),
    ("720p",       "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best"),
    ("480p",       "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best"),
    ("360p",       "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best"),
]


# ── Cookie helpers ────────────────────────────────────────────────────────────

def get_cookies_path() -> Path | None:
    """Return the active cookies file, preferring the Render Secret File."""
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
    if is_youtube(url) and cookies:
        args += ["--cookies", str(cookies)]
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
    stdout, stderr, code = run_ytdlp(base_args(url) + ["-J", "--no-playlist", url])
    if code != 0:
        raise RuntimeError(clean_errors(stderr))
    info = json.loads(stdout)
    heights: set[int] = set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and f.get("vcodec", "none") not in ("none", None, ""):
            heights.add(int(h))
    return sorted(heights, reverse=True)

def build_keyboard(url_key: str, heights: list[int]) -> InlineKeyboardMarkup:
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


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cp = get_cookies_path()
    cookies_status = f"✅ YouTube cookies active: `{cp.name}`" if cp \
                     else "⚠️ No YouTube cookies — use /setcookies if YouTube fails"
    await update.message.reply_text(
        "👋 *Welcome!*\n\n"
        "Send me any video URL and I'll let you pick the quality.\n"
        "🎵 Audio is always at best quality.\n"
        f"⚠️ Max file size: {MAX_FILESIZE_MB} MB\n\n"
        f"{cookies_status}",
        parse_mode="Markdown",
    )

async def handle_cookiestatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show exactly which cookies files exist and which is being used."""
    lines = ["*Cookie file status:*\n"]
    for label, p in [("Render Secret File", _RENDER_COOKIES),
                     ("Runtime upload (/setcookies)", _RUNTIME_COOKIES)]:
        if p.exists():
            try:
                first_line = p.read_text(errors="replace").strip().splitlines()[0]
            except Exception as e:
                first_line = f"(read error: {e})"
            size = p.stat().st_size
            lines.append(f"✅ *{label}*\nPath: `{p}`\nSize: {size} bytes\nFirst line: `{first_line}`")
        else:
            lines.append(f"❌ *{label}*\nNot found at: `{p}`")

    active = get_cookies_path()
    lines.append(f"\n*Will use:* `{active}`" if active else "\n*Will use:* nothing (YouTube will likely fail)")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def handle_setcookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    doc = msg.document
    if doc and doc.file_name.endswith(".txt"):
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(_RUNTIME_COOKIES))
        # Validate Netscape format
        try:
            text = _RUNTIME_COOKIES.read_text(errors="replace").lstrip()
            if "Netscape" not in text[:100]:
                _RUNTIME_COOKIES.unlink(missing_ok=True)
                await msg.reply_text(
                    "❌ *Invalid format.* The file must start with `# Netscape HTTP Cookie File`.\n\n"
                    "Export using the *Get cookies.txt LOCALLY* Chrome extension on youtube.com.\n"
                    "Open the file in a text editor first to verify the first line.",
                    parse_mode="Markdown",
                )
                return
        except Exception as e:
            await msg.reply_text(f"⚠️ Saved but couldn't validate: {e}")
            return
        size = _RUNTIME_COOKIES.stat().st_size
        await msg.reply_text(
            f"✅ *Cookies saved!* ({size} bytes)\n"
            f"Path: `{_RUNTIME_COOKIES}`\n\n"
            "YouTube should work now. Note: resets on redeploy — "
            "for permanent cookies use Render Secret Files.",
            parse_mode="Markdown",
        )
    else:
        await msg.reply_text(
            "Send a `cookies.txt` file *as a document* with `/setcookies`.\n\n"
            "How to export:\n"
            "1. Install *Get cookies.txt LOCALLY* on Chrome\n"
            "2. Go to youtube.com while logged in\n"
            "3. Click the extension → Export\n"
            "4. Send that file here\n\n"
            "Use /cookiestatus to check what's loaded.",
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
        hint = "\n\n💡 Use /setcookies to upload YouTube cookies, or /cookiestatus to debug." \
               if ("Sign in" in err or "bot" in err.lower()) else ""
        await msg.edit_text(f"❌ *Error:*\n`{err}`{hint}", parse_mode="Markdown")
        return

    url_key = str(msg.message_id)
    # Store selectors by index so callback_data stays short
    preset_selectors = []
    for label, selector in QUALITY_PRESETS:
        m = re.search(r"height<=(\d+)", selector)
        if m and any(h <= int(m.group(1)) for h in heights):
            preset_selectors.append(selector)
    ctx.user_data[url_key] = {"url": url, "presets": preset_selectors}

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
            f"❌ File too large (>{MAX_FILESIZE_MB} MB). Try a lower quality."
        )
        return

    await query.edit_message_text("📤 Uploading to Telegram…")
    try:
        with open(filepath, "rb") as f:
            if is_audio:
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

    cp = get_cookies_path()
    if cp:
        print(f"✅ Cookies: {cp}")
        try:
            first = cp.read_text(errors="replace").strip().splitlines()[0]
            print(f"   First line: {first}")
        except Exception as e:
            print(f"   (could not read: {e})")
    else:
        print("⚠️  No cookies file found — YouTube will likely fail")
        print(f"   Checked: {_RENDER_COOKIES}")
        print(f"   Checked: {_RUNTIME_COOKIES}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("help",         start))
    app.add_handler(CommandHandler("setcookies",   handle_setcookies))
    app.add_handler(CommandHandler("cookiestatus", handle_cookiestatus))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(filters.Document.MimeType("text/plain"), handle_setcookies))
    app.add_handler(CallbackQueryHandler(handle_download, pattern=r"^dl\|"))

    print("🤖 Bot polling…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
