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
# Render external URL config (used for forming download links)
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdlp_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

_RENDER_COOKIES  = Path("/etc/secrets/youtube_cookies.txt")
_RUNTIME_COOKIES = DOWNLOAD_DIR / "youtube_cookies.txt"

# Standard Telegram Bot API Limit is strictly 50MB
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024 

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
    ("1080p",      "bestvideo
