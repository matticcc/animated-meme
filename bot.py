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

ALLOWED_DOMAINS    = set(KNOWN_SITES.keys())
YOUTUBE_DOMAINS    = {"youtube.com", "youtu.be"}
IMAGE_CAPABLE_SITES = {"tiktok.com", "instagram.com"}
IMAGE_EXTS         = {".jpg", ".jpeg", ".png", ".webp"}

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


# ── yt-dlp / gallery-dl helpers ───────────────────────────────────────────────

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
    """Download images with gallery-dl into out_dir."""
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
    """
    Return yt-dlp JSON info dict. For TikTok /photo/ URLs, yt-dlp can't probe
    them, so we return a synthetic dict flagged for gallery-dl handling.
    """
    if is_tiktok_photo_url(url):
        # yt-dlp doesn't support TikTok photo slideshows — mark for gallery-dl
        return {"_use_gallerydl": True, "url": url}

    stdout, stderr, code = run_ytdlp(base_args(url) + ["-J", url])
    if code != 0:
        err = clean_errors(stderr)
        # If yt-dlp itself says unsupported, flag for gallery-dl if image-capable site
        if "Unsupported URL" in err and is_image_capable(url):
            return {"_use_gallerydl": True, "url": url}
        raise RuntimeError(err)
    return json.loads(stdout)

def extract_image_entries(info: dict) -> list[dict]:
    """
    Return a flat list of image entry dicts from a yt-dlp info blob.
    Each entry has at minimum: url (direct image URL), ext, index (1-based).
    """
    entries = []

    def _add(e: dict, idx: int):
        # yt-dlp image posts store the direct URL in 'url' field
        direct = e.get("url") or e.get("webpage_url", "")
        ext = e.get("ext", "jpg")
        title = e.get("title") or e.get("id") or f"image_{idx}"
        entries.append({"url": direct, "ext": ext, "title": title, "index": idx})

    top_entries = info.get("entries")
    if top_entries:
        for i, e in enumerate(top_entries, 1):
            _add(e, i)
    else:
        _add(info, 1)

    return entries

def is_image_post(info: dict) -> bool:
    """True when the probed info represents a photo/slideshow post."""
    if info.get("_use_gallerydl"):
        return True

    def _entry_is_image(e: dict) -> bool:
        ext = e.get("ext", "")
        if ext in ("jpg", "jpeg", "png", "webp"):
            return True
        # no video codec and no duration → treat as image
        vcodec = e.get("vcodec", "none") or "none"
        if vcodec == "none" and not e.get("duration"):
            return True
        return False

    entries = info.get("entries")
    if entries:
        return all(_entry_is_image(e) for e in entries)
    return _entry_is_image(info)

def get_available_heights(info: dict) -> list[int]:
    heights: set[int] = set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and f.get("vcodec", "none") not in ("none", None, ""):
            heights.add(int(h))
    return sorted(heights, reverse=True)

def find_downloaded_file(url_key: str) -> Path | None:
    for _ in range(10):
        candidates = [
            p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
            if p.suffix not in (".part", ".ytdl") and not p.name.endswith(".part")
        ]
        if candidates:
            return candidates[0]
        time.sleep(0.5)
    return None

def collect_image_files(url_key: str) -> list[Path]:
    """Collect all non-temp image files downloaded for url_key, sorted."""
    files = sorted([
        p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
        if p.suffix.lower() in IMAGE_EXTS and not p.name.endswith((".part", ".ytdl"))
    ])
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

def build_image_picker_keyboard(url_key: str, count: int) -> InlineKeyboardMarkup:
    """
    Show one button per photo + an 'All photos' button.
    callback_data: img|<url_key>|all  or  img|<url_key>|<0-based-index>
    """
    buttons = [[InlineKeyboardButton("📸 All photos", callback_data=f"img|{url_key}|all")]]
    for i in range(count):
        buttons.append([InlineKeyboardButton(f"Photo {i + 1}", callback_data=f"img|{url_key}|{i}")])
    return InlineKeyboardMarkup(buttons)


# ── Download helpers ───────────────────────────────────────────────────────────

def download_images_gallerydl(url: str, url_key: str) -> list[Path]:
    """
    Use gallery-dl to download a TikTok/Instagram photo post.
    Files are saved into a per-key subdirectory then moved up.
    """
    sub = DOWNLOAD_DIR / url_key
    sub.mkdir(exist_ok=True)
    run_gallerydl(url, sub)
    files = sorted([
        p for p in sub.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ])
    # Move files into DOWNLOAD_DIR with url_key prefix
    moved = []
    for i, fp in enumerate(files):
        dest = DOWNLOAD_DIR / f"{url_key}_{i:03d}{fp.suffix}"
        fp.rename(dest)
        moved.append(dest)
    # Clean up empty subdir
    try:
        sub.rmdir()
    except Exception:
        pass
    return moved

def download_image_by_index_ytdlp(url: str, url_key: str, entry: dict) -> Path | None:
    """Download a single image entry already probed by yt-dlp."""
    img_url = entry.get("url", "")
    if not img_url:
        return None
    ext = entry.get("ext", "jpg")
    dest = DOWNLOAD_DIR / f"{url_key}_single.{ext}"
    # Use yt-dlp to re-download by direct URL, or just wget it
    result = subprocess.run(
        ["yt-dlp", "--no-warnings", "-o", str(dest), img_url],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0 or not dest.exists():
        # Try plain wget/curl as last resort
        subprocess.run(["curl", "-sL", "-o", str(dest), img_url], timeout=60)
    return dest if dest.exists() else None


# ── Telegram upload helpers ────────────────────────────────────────────────────

async def send_photos(message, files: list[Path]) -> None:
    """Send one or multiple photos to a Telegram message, handling batching."""
    if not files:
        return
    if len(files) == 1:
        with open(files[0], "rb") as f:
            await message.reply_photo(photo=f, read_timeout=120, write_timeout=120, connect_timeout=30)
        return
    # Send in batches of 10 (Telegram media group limit)
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

    # ── Image / slideshow path ──
    if is_image_capable(url) and is_image_post(info):
        use_gdl = info.get("_use_gallerydl", False)

        if use_gdl:
            # gallery-dl path: we don't know count yet, download all at click time
            ctx.user_data[url_key] = {"url": url, "type": "gdl_image"}
            await msg.edit_text(
                f"🖼 *{site}* — photo slideshow detected.\n\nChoose what to download:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📸 Download all photos", callback_data=f"gdl|{url_key}|all")
                ]]),
                parse_mode="Markdown",
            )
        else:
            # yt-dlp path: we have entries, show per-photo picker
            entries = extract_image_entries(info)
            ctx.user_data[url_key] = {"url": url, "type": "yt_image", "entries": entries}
            count = len(entries)
            await msg.edit_text(
                f"🖼 *{site}* — {count} photo{'s' if count != 1 else ''} found.\n\nChoose what to download:",
                reply_markup=build_image_picker_keyboard(url_key, count),
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
    """Video / audio download handler."""
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
            f"❌ File too large ({size_bytes // 1024 // 1024} MB > {MAX_FILESIZE_MB} MB limit). Try a lower quality."
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
            f"❌ Upload timed out — file was {size_bytes // 1024 // 1024} MB. Try a lower quality or retry."
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        filepath.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)


async def handle_image_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles photo picker buttons for yt-dlp image posts (Instagram carousels,
    TikTok slideshows that yt-dlp supports).
    callback_data: img|<url_key>|all  or  img|<url_key>|<0-based-index>
    """
    query = update.callback_query
    await query.answer()

    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send the URL again.")
        return

    url     = data["url"]
    entries = data.get("entries", [])

    if choice == "all":
        targets = entries
    else:
        idx = int(choice)
        if idx >= len(entries):
            await query.edit_message_text("❌ Invalid selection.")
            return
        targets = [entries[idx]]

    await query.edit_message_text(f"⬇️ Downloading {len(targets)} photo(s)…")

    # Download each selected entry
    out_tpl = str(DOWNLOAD_DIR / f"{url_key}_%(autonumber)03d.%(ext)s")
    dl_args = base_args(url) + [
        "--no-warnings",
        "-o", out_tpl,
        url,
    ]

    try:
        _, stderr, code = await asyncio.get_event_loop().run_in_executor(
            None, lambda: run_ytdlp(dl_args)
        )
        if code != 0:
            raise RuntimeError(clean_errors(stderr))
    except Exception as e:
        await query.edit_message_text(f"❌ *Download error:*\n`{str(e)}`", parse_mode="Markdown")
        return

    all_files = collect_image_files(url_key)

    # If the user picked specific photos, filter to just those (1-based indices)
    if choice != "all":
        idx = int(choice)
        # Files are sorted; pick the one at position idx
        if idx < len(all_files):
            selected = [all_files[idx]]
        else:
            selected = all_files  # fallback: send all
    else:
        selected = all_files

    if not selected:
        # Maybe yt-dlp saved a video file (slideshow with bgm) — try video path
        video_files = [
            p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
            if p.suffix.lower() not in IMAGE_EXTS and not p.name.endswith((".part", ".ytdl"))
        ]
        if video_files:
            fp = video_files[0]
            await query.edit_message_text("📤 Uploading…")
            try:
                with open(fp, "rb") as f:
                    await query.message.reply_video(video=f, filename=fp.name, supports_streaming=True,
                                                    read_timeout=300, write_timeout=300, connect_timeout=60)
                await query.delete_message()
            except Exception as e:
                await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
            finally:
                fp.unlink(missing_ok=True)
                ctx.user_data.pop(url_key, None)
            return
        await query.edit_message_text("❌ No image files found after download.")
        return

    await query.edit_message_text(f"📤 Uploading {len(selected)} photo(s)…")
    try:
        await send_photos(query.message, selected)
        await query.delete_message()
    except (TimedOut, NetworkError):
        await query.edit_message_text("❌ Upload timed out. Please try again.")
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        for fp in collect_image_files(url_key):
            fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)


async def handle_gdl_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    gallery-dl handler for TikTok /photo/ URLs and any other URL yt-dlp can't probe.
    callback_data: gdl|<url_key>|all
    (individual photo selection is not possible before download with gallery-dl,
    but after download we show per-photo buttons if there are multiple)
    """
    query = update.callback_query
    await query.answer()

    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send the URL again.")
        return

    url = data["url"]

    await query.edit_message_text("⬇️ Downloading photos…")

    try:
        files = await asyncio.get_event_loop().run_in_executor(
            None, lambda: download_images_gallerydl(url, url_key)
        )
    except Exception as e:
        await query.edit_message_text(f"❌ *Download error:*\n`{str(e)}`", parse_mode="Markdown")
        return

    if not files:
        await query.edit_message_text("❌ No photos found. The post may be private or unsupported.")
        return

    # If multiple photos, ask user which one(s) they want
    if len(files) > 1:
        # Store file paths in user_data for the picker
        ctx.user_data[url_key]["gdl_files"] = [str(p) for p in files]
        buttons = [[InlineKeyboardButton("📸 All photos", callback_data=f"gdlpick|{url_key}|all")]]
        for i in range(len(files)):
            buttons.append([InlineKeyboardButton(f"Photo {i + 1}", callback_data=f"gdlpick|{url_key}|{i}")])
        await query.edit_message_text(
            f"🖼 {len(files)} photos downloaded. Choose which to send:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Single photo: send immediately
    await query.edit_message_text("📤 Uploading…")
    try:
        await send_photos(query.message, files)
        await query.delete_message()
    except (TimedOut, NetworkError):
        await query.edit_message_text("❌ Upload timed out. Please try again.")
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        for fp in files:
            fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)


async def handle_gdl_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles photo picker after gallery-dl has already downloaded all files.
    callback_data: gdlpick|<url_key>|all  or  gdlpick|<url_key>|<index>
    """
    query = update.callback_query
    await query.answer()

    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired.")
        return

    all_paths = [Path(p) for p in data.get("gdl_files", [])]
    if not all_paths:
        await query.edit_message_text("❌ Files no longer available. Send the URL again.")
        return

    if choice == "all":
        selected = all_paths
    else:
        idx = int(choice)
        selected = [all_paths[idx]] if idx < len(all_paths) else all_paths

    await query.edit_message_text(f"📤 Uploading {len(selected)} photo(s)…")
    try:
        await send_photos(query.message, selected)
        await query.delete_message()
    except (TimedOut, NetworkError):
        await query.edit_message_text("❌ Upload timed out. Please try again.")
    except Exception as e:
        await query.edit_message_text(f"❌ Upload error:\n`{e}`", parse_mode="Markdown")
    finally:
        for fp in all_paths:
            fp.unlink(missing_ok=True)
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
    print(f"✅ Cookies: {cp}" if cp else "⚠️  No cookies file found — some sites may require login")

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
    app.add_handler(CallbackQueryHandler(handle_download,    pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_image_pick,  pattern=r"^img\|"))
    app.add_handler(CallbackQueryHandler(handle_gdl_download, pattern=r"^gdl\|"))
    app.add_handler(CallbackQueryHandler(handle_gdl_pick,    pattern=r"^gdlpick\|"))

    print("🤖 Bot polling…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
