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
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout, result.stderr, result.returncode

def clean_errors(stderr: str) -> str:
    errors = [l for l in stderr.splitlines() if "ERROR" in l]
    return "\n".join(errors) if errors else stderr.strip()

def is_tiktok_photo_url(url: str) -> bool:
    return "tiktok.com" in url.lower() and "/photo/" in url.lower()

def get_instagram_api_url(url: str) -> str | None:
    match = re.search(r"instagram\.com/(?:p|reel|tv|share/v)/([^/?#&]+)", url)
    if not match:
        return None
    shortcode = match.group(1)
    variables = {
        "shortcode": shortcode,
        "fetch_tagged_user_count": None,
        "hoisted_comment_id": None,
        "hoisted_reply_id": None
    }
    encoded_vars = urllib.parse.quote(json.dumps(variables))
    return f"https://www.instagram.com/graphql/query/?doc_id=8845758582119845&variables={encoded_vars}"

def combine_video_audio(video_path: Path, audio_path: Path, output_path: Path) -> None:
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac",
            str(output_path)
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception:
        if video_path.exists() and not output_path.exists():
            os.rename(video_path, output_path)

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
                is_video = bool(video_url)
                
                if dynamic_target_idx is not None and dynamic_target_idx != "all":
                    if int(dynamic_target_idx) != idx:
                        continue
                else:
                    if choice == "video" and not is_video:
                        continue
                    if choice == "image" and is_video:
                        continue

                if is_video:
                    file_path = DOWNLOAD_DIR / f"{url_key}_{idx}.mp4"
                    urllib.request.urlretrieve(video_url, file_path)
                    downloaded_paths.append(file_path)
                else:
                    if image_url:
                        file_path = DOWNLOAD_DIR / f"{url_key}_{idx}.jpg"
                        urllib.request.urlretrieve(image_url, file_path)
                        downloaded_paths.append(file_path)
                        
            return downloaded_paths
        except Exception as e:
            raise RuntimeError(f"Error compiling layout content stream: {e}")

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
    return sorted([
        p for p in DOWNLOAD_DIR.glob(f"{url_key}_*")
        if p.suffix.lower() in IMAGE_EXTS and not p.name.endswith((".part", ".ytdl"))
    ])

def download_images(url: str, url_key: str) -> list[Path]:
    out_tpl = str(DOWNLOAD_DIR / f"{url_key}_%(autonumber)03d.%(ext)s")

    if not is_tiktok_photo_url(url):
        _, stderr, code = run_ytdlp(base_args(url) + ["-o", out_tpl, url])
        files = collect_image_files(url_key)
        if files:
            return files

    sub = DOWNLOAD_DIR / url_key
    sub.mkdir(exist_ok=True)
    run_gallerydl(url, sub)

    gdl_files = sorted([
        p for p in sub.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ])
    moved = []
    for i, fp in enumerate(gdl_files):
        dest = DOWNLOAD_DIR / f"{url_key}_{i:03d}{fp.suffix}"
        fp.rename(dest)
        moved.append(dest)
    try:
        sub.rmdir()
    except Exception:
        pass

    return moved if moved else collect_image_files(url_key)

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
    buttons = [[InlineKeyboardButton("📸 All photos", callback_data=f"pick|{url_key}|all")]]
    for i in range(count):
        buttons.append([InlineKeyboardButton(f"Photo {i + 1}", callback_data=f"pick|{url_key}|{i}")])
    return InlineKeyboardMarkup(buttons)

def build_dynamic_instagram_keyboard(url_key: str, img_count: int, vid_count: int) -> InlineKeyboardMarkup:
    buttons = []
    total_assets = img_count + vid_count
    
    if img_count > 0 and vid_count > 0:
        buttons.append([InlineKeyboardButton(f"📸 Images Only ({img_count})", callback_data=f"ig_choice|{url_key}|image")])
        buttons.append([InlineKeyboardButton(f"🎥 Videos Only ({vid_count})", callback_data=f"ig_choice|{url_key}|video")])
        buttons.append([InlineKeyboardButton("📦 Download Everything Combined", callback_data=f"ig_choice|{url_key}|all")])
    elif img_count > 1:
        buttons.append([InlineKeyboardButton(f"📸 Download All Images ({img_count})", callback_data=f"ig_choice|{url_key}|image")])
    elif vid_count > 1:
        buttons.append([InlineKeyboardButton(f"🎥 Download All Videos ({vid_count})", callback_data=f"ig_choice|{url_key}|video")])

    if total_assets > 1:
        row = []
        for i in range(total_assets):
            label = f"Item {i+1}"
            row.append(InlineKeyboardButton(label, callback_data=f"ig_pick|{url_key}|{i}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    else:
        buttons.append([InlineKeyboardButton("⬇️ Extract Content Asset", callback_data=f"ig_choice|{url_key}|all")])
        
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
        "*Supported sites:*\n"
        "• YouTube\n"
        "• TikTok _(videos & photo slideshows)_\n"
        "• Reddit\n"
        "• RedGifs\n"
        "• Instagram _(videos & photo carousels)_\n\n"
        "🎵 Audio extraction is also available.\n"
        f"⚠️ Max file size: {MAX_FILESIZE_MB} MB",
        parse_mode="Markdown",
    )

async def handle_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = ""

    if update.message.document:
        doc = update.message.document
        if doc.file_name and doc.file_name.lower() in ["message.txt", "document.txt", "file.txt"] or doc.mime_type == "text/plain":
            msg = await update.message.reply_text("📥 **Processing data file...**", parse_mode="Markdown")
            try:
                tg_file = await ctx.bot.get_file(doc.file_id)
                file_bytes = await tg_file.download_as_bytearray()
                text = file_bytes.decode("utf-8").strip()
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"❌ Failed to process payload data: `{e}`")
                return

    if not text and update.message.text:
        text = update.message.text.strip()

    if not text:
        return

    if text.startswith("{") and ("xdt_api" in text or "shortcode_media" in text or '"data"' in text or "items" in text):
        msg = await update.message.reply_text("⚙ *Analyzing layout content...*", parse_mode="Markdown")
        url_key = str(msg.message_id)
        
        try:
            vid_c = max(text.count('"video_url"'), text.count('"video_versions"'))
            img_c = max(text.count('"display_url"'), text.count('"image_versions2"'))
            
            if vid_c > 0 and img_c >= vid_c:
                img_c = img_c - vid_c
            if img_c == 0 and vid_c == 0:
                img_c, vid_c = 1, 1 
                
            ctx.user_data[url_key] = {"raw_json": text, "is_raw": True}
            await msg.edit_text(
                f"🔒 **Instagram Data Parsed Successfully**\nFound {img_c} images and {vid_c} videos.\n\nSelect extraction targets:",
                reply_markup=build_dynamic_instagram_keyboard(url_key, img_c, vid_c),
                parse_mode="Markdown"
            )
        except Exception as e:
            await msg.edit_text(f"❌ JSON parsing error: `{e}`")
        return

    url = extract_url(text)
    if not url: return

    if not is_allowed_site(url):
        await update.message.reply_text("❌ Unsupported link format.")
        return
        
    site = detect_site(url)
    msg = await update.message.reply_text(f"🔍 Checking **{site}** link...", parse_mode="Markdown")
    url_key = str(msg.message_id)

    if is_tiktok_photo_url(url):
        await msg.edit_text("⬇️ Extracting photo gallery elements...")
        try:
            files = await asyncio.get_event_loop().run_in_executor(None, download_images, url, url_key)
        except Exception as e:
            await msg.edit_text(f"❌ *Extraction error:* `{str(e)}`")
            return
            
        if not files:
            await msg.edit_text("❌ Failed to resolve photos. Ensure the post is public.")
            return

        files = sorted(files, key=lambda p: p.name)
        ctx.user_data[url_key] = {"files": [str(f) for f in files]}
        
        await msg.edit_text(
            f"📸 **TikTok Gallery Discovered ({len(files)} Photos)**\nChoose which photo you would like to receive:",
            reply_markup=build_photo_picker(url_key, len(files)),
            parse_mode="Markdown"
        )
        return

    try:
        info = await asyncio.get_event_loop().run_in_executor(None, probe_url, url)
    except Exception as e:
        if "instagram.com" in url:
            ctx.user_data[url_key] = {"url": url, "is_raw": False}
            api_url = get_instagram_api_url(url)
            instructions = (
                f"🔒 **Instagram Protected Content**\n"
                f"Direct access blocked. Route session through your browser:\n\n"
                f"1. Open link:\n`{api_url}`\n\n"
                f"2. Select all and copy (**Ctrl+A** then **Ctrl+C**).\n"
                f"3. **Paste/Upload text data output** straight back to this chat."
            )
            await msg.edit_text(instructions, parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await msg.edit_text(f"❌ *Scraping failure:* `{clean_errors(str(e))}`", parse_mode="Markdown")
        return

    if is_image_post(info):
        await msg.edit_text(f"⬇️ Downloading photos from *{site}*…", parse_mode="Markdown")
        try:
            files = await asyncio.get_event_loop().run_in_executor(None, download_images, url, url_key)
        except Exception as e:
            await msg.edit_text(f"❌ *Download error:*\n`{str(e)}`", parse_mode="Markdown")
            return

        if not files:
            await msg.edit_text("❌ No photos discovered or media extraction failed.")
            return

        ctx.user_data[url_key] = {"files": [str(p) for p in files]}

        if len(files) == 1:
            await msg.edit_text("📤 Uploading…")
            try:
                await send_photos(update.message, files)
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"❌ Upload error:\n`{e}`")
            finally:
                for fp in files: fp.unlink(missing_ok=True)
                ctx.user_data.pop(url_key, None)
        else:
            await msg.edit_text(
                f"🖼 *{len(files)} photos* downloaded. Which do you want to send?",
                reply_markup=build_photo_picker(url_key, len(files)),
                parse_mode="Markdown",
            )
        return

    heights = get_available_heights(info)
    if "instagram.com" in url:
        ctx.user_data[url_key] = {"url": url, "is_raw": False}
        await msg.edit_text(
            f"📦 **{site} Data Layout Verified**\nSelect your download target option:",
            reply_markup=build_dynamic_instagram_keyboard(url_key, 0, 1),
            parse_mode="Markdown"
        )
        return

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
        if choice == "all" or len(all_files) <= 1:
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

async def handle_instagram_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    _, url_key, choice = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired. Send your link again.")
        return
        
    is_raw = data.get("is_raw", False)
    target_payload = data["raw_json"] if is_raw else data["url"]
    await query.edit_message_text("⬇ Compiling assets... please wait.")
    
    try:
        files = await asyncio.get_event_loop().run_in_executor(
            None, parse_and_download_instagram, target_payload, url_key, choice, is_raw
        )
    except Exception as e:
        await query.edit_message_text(f"❌ *Processing error:*\n`{str(e)}`", parse_mode="Markdown")
        return
        
    if not files:
        await query.edit_message_text("❌ No media matches found.")
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
        await query.edit_message_text(f"❌ Upload error: `{e}`", parse_mode="Markdown")
    finally:
        for fp in files: fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)

async def handle_instagram_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    _, url_key, target_idx = query.data.split("|", 2)
    data = ctx.user_data.get(url_key)
    if not data:
        await query.edit_message_text("❌ Session expired.")
        return
        
    is_raw = data.get("is_raw", False)
    target_payload = data["raw_json"] if is_raw else data["url"]
    await query.edit_message_text(f"📥 Extracting item index [{int(target_idx)+1}]...")
    
    try:
        files = await asyncio.get_event_loop().run_in_executor(
            None, parse_and_download_instagram, target_payload, url_key, "all", is_raw, target_idx
        )
        if not files:
            await query.edit_message_text("❌ Target index extraction yielded no downloadable tracks.")
            return
            
        await query.edit_message_text("📤 Uploading asset element...")
        if files[0].suffix.lower() in IMAGE_EXTS:
            await send_photos(query.message, files)
        else:
            with open(files[0], "rb") as f:
                await query.message.reply_video(video=f, supports_streaming=True)
        await query.delete_message()
    except Exception as e:
        await query.edit_message_text(f"❌ Extraction error: `{e}`")
    finally:
        for fp in files: fp.unlink(missing_ok=True)
        ctx.user_data.pop(url_key, None)

# ── Session cleanup ────────────────────────────────────────────────────────────

def drop_existing_session() -> None:
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    for label, url in (
        ("deleteWebhook", f"{base}/deleteWebhook?drop_pending_updates=true"),
        ("getUpdates",    f"{base}/getUpdates?offset=-1&limit=1&timeout=0"),
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
    app.add_handler(CommandHandler("help",  start))
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, handle_input))
    
    app.add_handler(CallbackQueryHandler(handle_download,         pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_photo_pick,       pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(handle_instagram_choice, pattern=r"^ig_choice\|"))
    app.add_handler(CallbackQueryHandler(handle_instagram_pick,   pattern=r"^ig_pick\|"))

    print("🤖 Bot polling…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )

if __name__ == "__main__":
    main()
