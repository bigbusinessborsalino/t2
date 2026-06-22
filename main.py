"""
main.py
=======
Telegram video downloader bot — Render Free Tier ready.

What changed vs. the original tgbot.py:
  • Bolted on a tiny aiohttp HTTP server on $PORT so UptimeRobot can ping it
    every 5 minutes and keep the free-tier service warm.
  • Added graceful SIGTERM handling (Render sends SIGTERM before killing).
  • Added /stats, /ping, /health endpoints with JSON status.
  • Tgcrypto auto-detected and used when available (faster MTProto).
  • Periodic temp-dir cleanup so /tmp doesn't fill up.
  • Same tap-driven UX, same parallel chunked downloader, same quality picker.

Deploy on Render (free tier):
  1. Push this repo to GitHub (don't commit .env).
  2. Render → New → Web Service → pick the repo.
  3. Runtime: Python 3.11.x
  4. Build command:  pip install --upgrade pip && pip install -r requirements.txt
  5. Start command:  python main.py
  6. Environment:
        API_ID        = <from my.telegram.org>
        API_HASH      = <from my.telegram.org>
        BOT_TOKEN     = <from @BotFather>
        MAX_PARALLEL  = 2            (optional, keep small on free tier)
  7. Health check path: /health
  8. Free instance type. Done.
  9. UptimeRobot → New Monitor → HTTP(s) → https://<your-service>.onrender.com/health
        Interval: 5 minutes. That's it.

The first request after a cold start may take ~20-30s on free tier — the
UptimeRobot ping prevents that from happening for real users.
"""

from __future__ import annotations

# --- asyncio event-loop bootstrap (BEFORE any third-party imports) --------
# Pyrogram 2.0.106 calls `asyncio.get_event_loop()` at import time inside
# pyrogram/sync.py. On Python 3.12+ that emits a DeprecationWarning; on
# Python 3.14 it raises RuntimeError because no loop is bound to MainThread.
# We can't avoid that import-time call, so we make sure a loop exists FIRST.
import asyncio as _asyncio  # noqa: E402

try:
    _asyncio.get_event_loop()
except RuntimeError:
    _asyncio.set_event_loop(_asyncio.new_event_loop())

# --- normal imports --------------------------------------------------------

import asyncio
import json
import logging
import mimetypes
import os
import re
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp
import cloudscraper
from aiohttp import web
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# tgcrypto is optional but makes Pyrogram's MTProto layer noticeably faster.
try:
    import tgcrypto  # noqa: F401
    TGCRYPTO_AVAILABLE = True
except Exception:
    TGCRYPTO_AVAILABLE = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_ID = int(os.environ.get("API_ID", "0") or "0")
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()

# On Render free tier the only writable dir is /tmp. Pin DOWNLOAD_DIR there.
_DEFAULT_TMP = Path(tempfile.gettempdir()) / "tg_dl"
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(_DEFAULT_TMP)))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

VIDEOS_PER_PAGE = 8
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
DL_WORKERS = 8
CHUNK_SIZE = 256 * 1024
PROGRESS_UPDATE_SEC = 2.0
SITE_BASE = "https://www.freepornvideos.xxx"
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()

# HTTP server port — Render sets $PORT automatically. Local fallback = 10000.
HTTP_PORT = int(os.environ.get("PORT", "10000"))

# Bot-level config
SERVICE_START_TIME = time.time()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    stream=sys.stdout,  # Render captures stdout
)
log = logging.getLogger("tgbot")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("aiohttp.server").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Scraper primitives
# ---------------------------------------------------------------------------

USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]

QUALITY_RE = re.compile(r"(\d{3,4}p|2160p|1440p|1080p|720p|480p|360p|240p|4k|2k|hd|sd)", re.I)
VIDEO_EXT_RE = re.compile(r"\.(?:mp4|m3u8|m3u|m4v|webm|mov|mpd)(?:\?|$)", re.I)
VIDEO_URL_RE = re.compile(
    r"""(?P<url>(?:https?:)?//[^\s'"]+?\.(?:mp4|m3u8|m3u|m4v|webm|mov|mpd)(?:\?[^\s'"]*)?)""",
    re.I,
)

QUALITY_RANK = {
    "4k": 2160, "2160p": 2160,
    "2k": 1440, "1440p": 1440,
    "1080p": 1080, "hd": 1080,
    "720p": 720,
    "480p": 480, "sd": 480,
    "360p": 360,
    "240p": 240,
}


@dataclass
class VideoEntry:
    title: str
    url: str
    quality: Optional[str] = None
    source: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _abs(base: str, maybe: Optional[str]) -> Optional[str]:
    if not maybe:
        return None
    maybe = maybe.strip()
    if not maybe:
        return None
    return urljoin(base, maybe)


def _detect_quality(text: str) -> Optional[str]:
    if not text:
        return None
    m = QUALITY_RE.search(text)
    return m.group(1).lower() if m else None


def _clean_title(raw: Optional[str]) -> str:
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


class VideoExtractor:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def _extract_title(self, soup: BeautifulSoup) -> str:
        for getter in (
            lambda: soup.find("meta", property="og:title"),
            lambda: soup.find("meta", attrs={"name": "twitter:title"}),
            lambda: soup.find("title"),
            lambda: soup.find("h1"),
        ):
            try:
                el = getter()
                if not el:
                    continue
                if el.name == "meta":
                    v = el.get("content")
                    if v:
                        return _clean_title(v)
                else:
                    txt = el.get_text(" ", strip=True)
                    if txt:
                        return _clean_title(txt)
            except Exception:
                continue
        return ""

    def _from_og_video(self, soup: BeautifulSoup) -> List[VideoEntry]:
        out: List[VideoEntry] = []
        try:
            for meta in soup.find_all("meta"):
                prop = (meta.get("property") or "").lower()
                if prop in ("og:video", "og:video:url", "og:video:secure_url"):
                    url = _abs(self.base_url, meta.get("content"))
                    if not url:
                        continue
                    out.append(
                        VideoEntry(
                            title="", url=url,
                            quality=_detect_quality(url) or "sd",
                            source="og:video",
                        )
                    )
        except Exception as e:
            log.warning("og:video extractor: %s", e)
        return out

    def _from_twitter_player(self, soup: BeautifulSoup) -> List[VideoEntry]:
        out: List[VideoEntry] = []
        try:
            for meta in soup.find_all("meta"):
                name = (meta.get("name") or "").lower()
                if name in ("twitter:player:stream", "twitter:player", "twitter:player:url"):
                    url = _abs(self.base_url, meta.get("content"))
                    if not url:
                        continue
                    out.append(
                        VideoEntry(title="", url=url,
                                   quality=_detect_quality(url), source=name)
                    )
        except Exception as e:
            log.warning("twitter extractor: %s", e)
        return out

    def _from_video_src(self, soup: BeautifulSoup) -> List[VideoEntry]:
        out: List[VideoEntry] = []
        try:
            for tag in soup.find_all(["video", "source", "iframe", "embed"]):
                try:
                    src = tag.get("src") or tag.get("data-src") or tag.get("data-video-src")
                    if not src:
                        continue
                    url = _abs(self.base_url, src)
                    if not url:
                        continue
                    quality = (
                        _detect_quality(tag.get("title", ""))
                        or _detect_quality(tag.get("label", ""))
                        or _detect_quality(tag.get("res", ""))
                        or _detect_quality(url)
                    )
                    out.append(
                        VideoEntry(
                            title="", url=url, quality=quality, source=tag.name,
                            extra={"type": tag.get("type"),
                                   "label": tag.get("label"),
                                   "res": tag.get("res")},
                        )
                    )
                except Exception as inner:
                    log.warning("  video src skip: %s", inner)
        except Exception as e:
            log.warning("video src extractor: %s", e)
        return out

    def _from_inline_json(self, soup: BeautifulSoup) -> List[VideoEntry]:
        out: List[VideoEntry] = []
        try:
            for script in soup.find_all("script"):
                try:
                    text = script.get_text() or ""
                    if not text:
                        continue
                    for m in VIDEO_URL_RE.finditer(text):
                        url = m.group("url")
                        if url.startswith("//"):
                            url = "https:" + url
                        url = _abs(self.base_url, url)
                        if not url:
                            continue
                        out.append(
                            VideoEntry(title="", url=url,
                                       quality=_detect_quality(url),
                                       source="inline-script")
                        )
                except Exception as inner:
                    log.warning("  script scan skip: %s", inner)
        except Exception as e:
            log.warning("inline JSON extractor: %s", e)
        return out

    def _from_link_rel(self, soup: BeautifulSoup) -> List[VideoEntry]:
        out: List[VideoEntry] = []
        try:
            for link in soup.find_all("link", rel=True):
                rel = " ".join(link.get("rel") or []).lower()
                if "video" in rel or "player" in rel or "enclosure" in rel:
                    url = _abs(self.base_url, link.get("href"))
                    if url and VIDEO_EXT_RE.search(url):
                        out.append(
                            VideoEntry(title="", url=url,
                                       quality=_detect_quality(url),
                                       source=f"link[{rel}]")
                        )
        except Exception as e:
            log.warning("link extractor: %s", e)
        return out

    def extract(self, html: str) -> List[VideoEntry]:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as e:
            log.error("parser fatal: %s", e)
            return []
        title = self._extract_title(soup)
        extracted: List[VideoEntry] = []
        extracted.extend(self._from_og_video(soup))
        extracted.extend(self._from_twitter_player(soup))
        extracted.extend(self._from_video_src(soup))
        extracted.extend(self._from_inline_json(soup))
        extracted.extend(self._from_link_rel(soup))
        seen = set()
        unique: List[VideoEntry] = []
        for entry in extracted:
            key = entry.url.split("?")[0].lower()
            if key in seen:
                continue
            seen.add(key)
            if not entry.title:
                entry.title = title
            unique.append(entry)
        return unique


# ---------------------------------------------------------------------------
# Pick / ranking
# ---------------------------------------------------------------------------

def _ext_from_url(u: str) -> str:
    p = urlparse(u).path.lower()
    if ".m3u8" in p:
        return "m3u8"
    if ".mpd" in p:
        return "mpd"
    if ".webm" in p:
        return "webm"
    if ".mp4" in p or ".m4v" in p or ".mov" in p:
        return "mp4"
    return "other"


@dataclass
class Pick:
    url: str
    quality: str
    score: int
    ext: str


def _score_url(url: str, quality: Optional[str], source: str) -> int:
    ext = _ext_from_url(url)
    q = (quality or "").lower()
    s = QUALITY_RANK.get(q, 0)
    if ext == "mp4":
        s += 100_000
    elif ext == "webm":
        s += 90_000
    elif ext in ("m3u8", "mpd"):
        s += 10_000
    if source in ("iframe", "embed"):
        s = -1
    u = url.lower()
    if "preview" in u or "screenshot" in u or "thumb" in u or "/previews/" in u:
        s -= 200_000
    if "library" in u and "bkcdn" in u:
        s += 50_000
    return s


def pick_best(entries: List[VideoEntry]) -> Optional[Pick]:
    if not entries:
        return None
    best: Optional[Pick] = None
    for e in entries:
        s = _score_url(e.url, e.quality, e.source)
        if best is None or s > best.score:
            best = Pick(
                url=e.url,
                quality=(e.quality or "unknown").lower(),
                score=s,
                ext=_ext_from_url(e.url),
            )
    return best if (best and best.score >= 0) else None


def rank_qualities(entries: List[VideoEntry]) -> List[VideoEntry]:
    return sorted(
        entries,
        key=lambda e: _score_url(e.url, e.quality, e.source),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------

class ProgressTracker:
    def __init__(self, message: Message, label: str = "Downloading") -> None:
        self.message = message
        self.label = label
        self.last_edit = 0.0
        self.start = time.time()

    async def update(self, downloaded: int, total: Optional[int]) -> None:
        now = time.time()
        if now - self.last_edit < PROGRESS_UPDATE_SEC:
            return
        self.last_edit = now
        elapsed = max(now - self.start, 0.001)
        speed = downloaded / elapsed
        if total and total > 0:
            pct = downloaded / total * 100
            bar = _bar(pct)
            text = (
                f"⏬ {self.label}\n"
                f"{bar} {pct:.1f}%\n"
                f"{_fmt(downloaded)} / {_fmt(total)}\n"
                f"Speed: {_fmt(speed)}/s"
            )
        else:
            text = (
                f"⏬ {self.label}\n"
                f"{_fmt(downloaded)} downloaded\n"
                f"Speed: {_fmt(speed)}/s"
            )
        try:
            await self.message.edit_text(text)
        except Exception:
            pass


def _bar(pct: float, width: int = 20) -> str:
    fill = int(pct / 100 * width)
    return "█" * fill + "░" * (width - fill)


def _fmt(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}"


_DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
    "Origin": "https://www.freepornvideos.xxx",
}
_DL_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)


async def _probe(
    session: aiohttp.ClientSession,
    url: str,
    headers: Optional[dict] = None,
) -> Tuple[Optional[int], bool]:
    hdrs = {**_DL_HEADERS, **(headers or {})}
    try:
        async with session.head(url, headers=hdrs, timeout=_DL_TIMEOUT, allow_redirects=True) as r:
            cl = r.headers.get("Content-Length")
            total = int(cl) if cl and cl.isdigit() else None
            ranges = r.headers.get("Accept-Ranges", "").lower() == "bytes"
            return total, ranges
    except Exception:
        return None, False


async def _download_chunk(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    offset: int,
    length: int,
    counter: "list[int]",
    headers: Optional[dict] = None,
) -> None:
    end = offset + length - 1
    hdrs = {**_DL_HEADERS, **(headers or {}), "Range": f"bytes={offset}-{end}"}
    async with session.get(url, headers=hdrs, timeout=_DL_TIMEOUT, allow_redirects=True) as r:
        r.raise_for_status()
        with dest.open("r+b") as f:
            f.seek(offset)
            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    counter[0] += len(chunk)


async def _warm_download_url(url: str, page_url: str) -> Tuple[str, dict]:
    """
    Use cloudscraper to GET the download URL, solve any Cloudflare challenge,
    follow redirects, and return (final_url, cookies_dict) for aiohttp to use.

    The CDN often requires:
      - Cloudflare clearance cookies (__cf_bm, cf_clearance) — solved by cloudscraper
      - A proper Referer header pointing to the page that linked the file
      - A matching User-Agent (we use the same one as _DL_HEADERS)
    """
    def _do() -> Tuple[str, dict]:
        scraper = _get_scraper()
        # Force the same UA so the cookies match what aiohttp will send
        scraper.headers.update({
            "User-Agent": _DL_HEADERS["User-Agent"],
            "Accept": _DL_HEADERS["Accept"],
            "Accept-Language": _DL_HEADERS["Accept-Language"],
            "Referer": page_url or "https://www.freepornvideos.xxx/",
            "Origin": "https://www.freepornvideos.xxx",
        })
        try:
            resp = scraper.get(
                url,
                timeout=30,
                allow_redirects=True,
                stream=False,  # we just want headers + cookies, not the body
            )
            cookies = dict(scraper.cookies.get_dict())
            return str(resp.url), cookies
        except Exception as e:
            log.warning("cloudscraper warmup failed for %s: %s", url, e)
            return url, {}

    return await asyncio.to_thread(_do)


async def download_to_file(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    progress: Optional[ProgressTracker] = None,
    referer: str = "",
) -> int:
    dl_headers: dict = {"Referer": referer or "https://www.freepornvideos.xxx/"}

    total, accepts_ranges = await _probe(session, url, headers=dl_headers)

    if accepts_ranges and total and total > 2 * 1024 * 1024:
        workers = min(DL_WORKERS, max(1, total // (2 * 1024 * 1024)))
        chunk_sz = total // workers
        with dest.open("wb") as f:
            f.seek(total - 1)
            f.write(b"\x00")

        counter: list[int] = [0]

        async def _tick() -> None:
            while True:
                await asyncio.sleep(PROGRESS_UPDATE_SEC)
                if progress:
                    await progress.update(counter[0], total)

        ticker = asyncio.create_task(_tick())
        try:
            tasks = []
            for i in range(workers):
                off = i * chunk_sz
                ln = chunk_sz if i < workers - 1 else total - off
                tasks.append(_download_chunk(session, url, dest, off, ln, counter, dl_headers))
            await asyncio.gather(*tasks)
        finally:
            ticker.cancel()
        if progress:
            await progress.update(total, total)
        return total

    async with session.get(url, headers={**_DL_HEADERS, **dl_headers}, timeout=_DL_TIMEOUT, allow_redirects=True) as r:
        r.raise_for_status()
        total_hdr = r.headers.get("Content-Length")
        total = int(total_hdr) if total_hdr and total_hdr.isdigit() else None
        downloaded = 0
        with dest.open("wb") as f:
            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if progress:
                    await progress.update(downloaded, total)
        return downloaded


# ---------------------------------------------------------------------------
# Site catalog
# ---------------------------------------------------------------------------

VIDEO_PAGE_RE = re.compile(
    r"^https?://(?:www\.)?freepornvideos\.xxx/(?:[a-z]{2}/)?videos/(\d+)/([a-z0-9\-_]+)/?$",
    re.I,
)


def is_video_page_url(url: str) -> bool:
    return bool(VIDEO_PAGE_RE.match(url))


def video_id_from_url(url: str) -> Optional[str]:
    m = VIDEO_PAGE_RE.match(url)
    return m.group(1) if m else None


@dataclass
class VideoRecord:
    video_id: str
    title: str
    page_url: str
    thumbnail: Optional[str] = None
    duration: Optional[str] = None
    best_url: Optional[str] = None
    best_quality: Optional[str] = None
    direct_urls: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Catalog:
    def __init__(self, max_pages: int = 50, max_videos_total: int = 1500) -> None:
        self.max_pages = max_pages
        self.max_videos_total = max_videos_total
        self.pages: Dict[int, List[VideoRecord]] = {}
        self.total_pages: int = 0
        self._lock = asyncio.Lock()

    async def get_page(self, n: int) -> Optional[List[VideoRecord]]:
        async with self._lock:
            return self.pages.get(n)

    async def store_page(self, n: int, recs: List[VideoRecord], is_last: bool) -> None:
        async with self._lock:
            self.pages[n] = recs
            if is_last or n > self.total_pages:
                self.total_pages = max(self.total_pages, n)


# ---------------------------------------------------------------------------
# cloudscraper-based fetcher
# ---------------------------------------------------------------------------

_scraper_lock = asyncio.Lock()
_scraper: Optional[cloudscraper.CloudScraper] = None


def _get_scraper() -> cloudscraper.CloudScraper:
    global _scraper
    if _scraper is None:
        _scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    return _scraper


def _fetch_html(url: str) -> str:
    scraper = _get_scraper()
    resp = scraper.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


async def fetch_listing_page(page_num: int) -> Tuple[List[VideoRecord], bool]:
    url = f"{SITE_BASE}/latest-updates/" if page_num == 1 else f"{SITE_BASE}/latest-updates/{page_num}/"
    log.info("Listing fetch: %s", url)
    try:
        html = await asyncio.to_thread(_fetch_html, url)
    except Exception as e:
        log.warning("listing fetch %s: %s", url, e)
        return [], True

    soup = BeautifulSoup(html, "html.parser")
    seen: Set[str] = set()
    records: List[VideoRecord] = []
    for a in soup.find_all("a", href=True):
        try:
            full = urljoin(SITE_BASE, a["href"]).split("#")[0].split("?")[0]
            if not is_video_page_url(full):
                continue
            vid = video_id_from_url(full)
            if not vid or vid in seen:
                continue
            seen.add(vid)
            text = _clean_title(a.get_text(" ", strip=True))
            records.append(
                VideoRecord(
                    video_id=vid,
                    title=text or f"Video {vid}",
                    page_url=full,
                )
            )
        except Exception:
            continue

    return records, len(records) == 0


async def fetch_video_details(page_url: str) -> Tuple[str, List[VideoEntry], Optional[str]]:
    try:
        html = await asyncio.to_thread(_fetch_html, page_url)
    except Exception as e:
        log.warning("video page fetch %s: %s", page_url, e)
        return "", [], None

    soup = BeautifulSoup(html, "html.parser")

    title = ""
    for sel, attr in (
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
        ("h1", None),
        ("title", None),
    ):
        try:
            el = soup.select_one(sel)
            if el:
                v = el.get(attr) if attr else el.get_text(" ", strip=True)
                if v:
                    title = _clean_title(str(v))
                    break
        except Exception:
            continue

    thumb = None
    for sel in ('meta[property="og:image"]', 'meta[name="twitter:image"]'):
        try:
            el = soup.select_one(sel)
            if el and el.get("content"):
                thumb = el.get("content")
                break
        except Exception:
            continue

    entries = VideoExtractor(base_url=page_url).extract(html)
    return title, entries, thumb


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------

def _short(text: str, n: int = 36) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def listing_keyboard(page_num: int, total_pages: int, recs: List[VideoRecord]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for i, r in enumerate(recs):
        label = f"{i + 1}. {_short(r.title, 38)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"VID|{r.video_id}")])

    nav: List[InlineKeyboardButton] = []
    if page_num > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"PAGE|{page_num - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page_num}/{max(total_pages, page_num)}", callback_data="PAGE|NOOP"))
    if page_num < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"PAGE|{page_num + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"PAGE|{page_num}")])
    return InlineKeyboardMarkup(rows)


def quality_keyboard(video_id: str, entries: List[VideoEntry]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for i, e in enumerate(entries[:10]):
        ext = _ext_from_url(e.url)
        q = (e.quality or "?").upper()
        if ext not in ("mp4", "webm"):
            continue
        label = f"⬇️ {q} · {ext.upper()}"
        rows.append([InlineKeyboardButton(label, callback_data=f"DL|{video_id}|{i}")])
    if not rows:
        rows.append([InlineKeyboardButton("❌ No direct file qualities", callback_data="DL|NONE")])
    rows.append([InlineKeyboardButton("⬅️ Back to listings", callback_data="BACK|1")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Catalog state
# ---------------------------------------------------------------------------

catalogs: Dict[int, Catalog] = {}
video_index: Dict[str, VideoRecord] = {}
user_last_request: Dict[int, float] = {}  # simple per-user throttle


def get_catalog(chat_id: int) -> Catalog:
    if chat_id not in catalogs:
        catalogs[chat_id] = Catalog()
    return catalogs[chat_id]


# ---------------------------------------------------------------------------
# Pyrogram bot
# ---------------------------------------------------------------------------

app = Client(
    name="tgbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=MAX_PARALLEL * 2,
)

dl_sem = asyncio.Semaphore(MAX_PARALLEL)
USER_COOLDOWN_SEC = float(os.environ.get("USER_COOLDOWN_SEC", "1.5"))


HELP_TEXT = (
    "🎬 **Video Downloader Bot**\n\n"
    "Tap-driven — no URL drops needed.\n\n"
    "**Commands**\n"
    "• `/start` — open the first page of videos\n"
    "• `/refresh` — clear cache and reload the catalog\n"
    "• `/help` — this message\n"
    "• `/stats` — runtime + cache stats\n\n"
    "**Flow**\n"
    "1. Pick a video from the listing\n"
    "2. Pick a quality\n"
    "3. Bot downloads and sends the file (up to **2 GB**)"
)


def _is_throttled(user_id: int) -> bool:
    now = time.time()
    last = user_last_request.get(user_id, 0.0)
    if now - last < USER_COOLDOWN_SEC:
        return True
    user_last_request[user_id] = now
    return False


@app.on_message(filters.command("start"))
async def cmd_start(_, message: Message):
    if _is_throttled(message.from_user.id if message.from_user else 0):
        return
    await send_listing(message.chat.id, page_num=1, edit=None)


@app.on_message(filters.command("refresh"))
async def cmd_refresh(_, message: Message):
    catalogs.pop(message.chat.id, None)
    await send_listing(message.chat.id, page_num=1, edit=None)


@app.on_message(filters.command("help"))
async def cmd_help(_, message: Message):
    await message.reply_text(HELP_TEXT)


@app.on_message(filters.command("stats"))
async def cmd_stats(_, message: Message):
    uptime = time.time() - SERVICE_START_TIME
    hours, rem = divmod(int(uptime), 3600)
    minutes, seconds = divmod(rem, 60)
    text = (
        "📊 **Bot stats**\n"
        f"• Uptime: {hours}h {minutes}m {seconds}s\n"
        f"• Catalogs cached: {len(catalogs)}\n"
        f"• Video index size: {len(video_index)}\n"
        f"• Download dir: `{DOWNLOAD_DIR}`\n"
        f"• Parallel slots: {MAX_PARALLEL}\n"
        f"• tgcrypto: {'✅' if TGCRYPTO_AVAILABLE else '❌'}\n"
        f"• Render URL: `{RENDER_EXTERNAL_URL or 'n/a'}`"
    )
    await message.reply_text(text)


# ---------------------------------------------------------------------------
# Listing renderer
# ---------------------------------------------------------------------------

async def send_listing(chat_id: int, page_num: int, edit: Optional[Message]) -> Optional[Message]:
    cat = get_catalog(chat_id)
    cached = await cat.get_page(page_num)
    if cached is None:
        loading = await (edit.edit_text(f"📜 Loading page {page_num}…") if edit else app.send_message(chat_id, f"📜 Loading page {page_num}…"))
        recs, is_last = await fetch_listing_page(page_num)
        await cat.store_page(page_num, recs, is_last)
        cached = recs
        if not recs:
            await loading.edit_text("❌ No videos found on that page.")
            return loading
        edit = loading

    if cat.total_pages < page_num:
        await cat.store_page(page_num, cached, is_last=False)
    if not cached:
        await edit.edit_text("❌ Empty page.")
        return edit

    for r in cached:
        video_index[r.video_id] = r

    text = (
        f"🎬 **Videos — page {page_num}**\n"
        f"_{len(cached)} videos on this page. Tap one to see qualities._"
    )
    kb = listing_keyboard(page_num, cat.total_pages, cached)
    try:
        await edit.edit_text(text, reply_markup=kb)
    except Exception:
        return await app.send_message(chat_id, text, reply_markup=kb)
    return edit


# ---------------------------------------------------------------------------
# Callback query router
# ---------------------------------------------------------------------------

@app.on_callback_query()
async def on_callback(_, query: CallbackQuery):
    if query.from_user and _is_throttled(query.from_user.id):
        await query.answer("Slow down, boss man.", show_alert=False)
        return
    await query.answer()
    data = query.data or ""
    parts = data.split("|")
    kind = parts[0]

    if kind == "PAGE":
        sub = parts[1] if len(parts) > 1 else "1"
        if sub == "NOOP":
            return
        try:
            n = int(sub)
        except ValueError:
            return
        await send_listing(query.message.chat.id, n, edit=query.message)
        return

    if kind == "BACK":
        try:
            n = int(parts[1])
        except (ValueError, IndexError):
            n = 1
        await send_listing(query.message.chat.id, n, edit=query.message)
        return

    if kind == "VID":
        vid = parts[1] if len(parts) > 1 else ""
        await show_qualities(query.message, vid)
        return

    if kind == "DL":
        if len(parts) < 3 or parts[1] == "NONE":
            return
        vid = parts[1]
        try:
            idx = int(parts[2])
        except ValueError:
            return
        await start_download(query.message, vid, idx)
        return


async def show_qualities(message: Message, video_id: str) -> None:
    rec = video_index.get(video_id)
    if not rec:
        await message.edit_text("❌ Lost that video. Go back to listings.")
        return

    if not rec.direct_urls:
        status = await message.edit_text(f"🔎 Reading page for *{_short(rec.title, 50)}*…")
        title, entries, thumb = await fetch_video_details(rec.page_url)
        if title:
            rec.title = title
        rec.thumbnail = thumb
        rec.direct_urls = [e.to_dict() for e in entries]
        if not entries:
            await status.edit_text("❌ No direct video URLs on that page.")
            return
        video_index[video_id] = rec
    else:
        status = message
        try:
            await message.edit_text(f"🎬 *{_short(rec.title, 50)}*\n🔎 Loading qualities…")
        except Exception:
            pass
        entries = [VideoEntry(**d) for d in rec.direct_urls]

    direct = [e for e in entries if _ext_from_url(e.url) in ("mp4", "webm")]
    direct = rank_qualities(direct)

    if not direct:
        await status.edit_text("❌ No direct file qualities — only HLS/iframe.")
        return

    caption = (
        f"🎬 **{_short(rec.title, 60)}**\n"
        f"_{len(direct)} quality option(s). Tap one to download._"
    )
    kb = quality_keyboard(video_id, direct)

    if rec.thumbnail:
        try:
            await status.delete()
        except Exception:
            pass
        try:
            await app.send_photo(
                message.chat.id,
                photo=rec.thumbnail,
                caption=caption,
                reply_markup=kb,
            )
            return
        except Exception:
            pass

    try:
        await status.edit_text(caption, reply_markup=kb)
    except Exception:
        await app.send_message(message.chat.id, caption, reply_markup=kb)


async def start_download(message: Message, video_id: str, entry_index: int) -> None:
    rec = video_index.get(video_id)
    if not rec or not rec.direct_urls:
        await message.edit_text("❌ Lost that video. Go back to listings.")
        return
    try:
        entry_dict = rec.direct_urls[entry_index]
    except (IndexError, KeyError):
        await message.edit_text("❌ Quality not available anymore.")
        return

    entry = VideoEntry(**entry_dict)
    url = entry.url
    if _ext_from_url(url) not in ("mp4", "webm"):
        await message.edit_text("❌ That URL isn't a direct file.")
        return

    parsed = urlparse(url)
    fname = os.path.basename(parsed.path) or "video.mp4"
    if "." not in fname:
        mime, _ = mimetypes.guess_type(url)
        ext = ".mp4" if not mime else (mimetypes.guess_extension(mime) or ".mp4")
        fname += ext
    dest = DOWNLOAD_DIR / f"{int(time.time())}_{video_id}_{fname}"

    status = await message.edit_text(
        f"⏬ Starting download…\n🎬 {rec.title}\n📄 {fname}"
    )

    # --- Cloudflare / CDN warmup ----------------------------------------
    # The CDN behind these files often requires:
    #   1. Cloudflare clearance cookies (solved by cloudscraper)
    #   2. A proper Referer header (we use the page that linked the file)
    #   3. A matching User-Agent (cloudscraper uses ours)
    # We do one GET through cloudscraper to solve the challenge, harvest
    # cookies + final URL, then hand them to aiohttp for the actual transfer.
    status = await message.edit_text(
        f"⏬ Starting download…\n🎬 {rec.title}\n📄 {fname}\n🔐 Solving CDN challenge…"
    )
    try:
        final_url, cookies = await _warm_download_url(url, rec.page_url)
    except Exception as e:
        log.warning("warmup exception: %s", e)
        final_url, cookies = url, {}

    if final_url != url:
        log.info("redirect resolved: %s -> %s", url, final_url)
    if cookies:
        log.info("got %d cookies from warmup: %s", len(cookies), list(cookies.keys()))

    async with dl_sem:
        async with aiohttp.ClientSession(cookies=cookies or None) as session:
            progress = ProgressTracker(status, label="Downloading")
            try:
                size = await download_to_file(
                    session, final_url, dest,
                    progress=progress,
                    referer=rec.page_url,
                )
            except aiohttp.ClientResponseError as e:
                # 403/503 from the CDN — log so we can debug, then surface to user
                log.warning("download %s: HTTP %s", final_url, e.status)
                await status.edit_text(
                    f"❌ HTTP {e.status} from CDN.\n"
                    f"Try `/refresh` and pick a different quality — the source may be cold."
                )
                return
            except Exception as e:
                await status.edit_text(f"❌ Download failed: `{e}`")
                return

        if size <= 0:
            await status.edit_text("❌ Empty file.")
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            return

        size_mb = size / 1024 / 1024
        await status.edit_text(f"📤 Uploading **{size_mb:.1f} MB**…")

        caption = f"🎬 {rec.title}\n📄 {fname}\n🔗 {rec.page_url}"[:1024]

        try:
            upload_prog = UploadProgress(status, fname, size)
            await message.reply_document(
                document=str(dest),
                caption=caption,
                progress=upload_prog,
            )
        except Exception as e:
            log.exception("upload failed")
            await app.send_message(
                message.chat.id,
                f"❌ Upload failed: `{e}`",
            )
        finally:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass

        try:
            await status.edit_text("✅ Done, boss man.")
        except Exception:
            pass


class UploadProgress:
    def __init__(self, message: Message, fname: str, total: int) -> None:
        self.message = message
        self.fname = fname
        self.total = total
        self.start = time.time()
        self.last_edit = 0.0

    async def __call__(self, current: int, total: int) -> None:
        now = time.time()
        if now - self.last_edit < PROGRESS_UPDATE_SEC:
            return
        self.last_edit = now
        elapsed = max(now - self.start, 0.001)
        speed = current / elapsed
        if total and total > 0:
            pct = current / total * 100
            bar = _bar(pct)
            text = (
                f"📤 Uploading\n"
                f"{bar} {pct:.1f}%\n"
                f"{_fmt(current)} / {_fmt(total)}\n"
                f"Speed: {_fmt(speed)}/s"
            )
        else:
            text = f"📤 Uploading\n{_fmt(current)} sent\nSpeed: {_fmt(speed)}/s"
        try:
            await self.message.edit_text(text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP server (for UptimeRobot)
# ---------------------------------------------------------------------------

async def _root_handler(_request: web.Request) -> web.Response:
    return web.Response(
        text=(
            "🤖 Telegram Video Downloader Bot is running.\n\n"
            "Open the bot in Telegram to use it. This HTTP endpoint exists "
            "only to keep the Render free-tier service warm via UptimeRobot."
        ),
        content_type="text/plain",
    )


async def _health_handler(_request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "telegram-video-bot",
        "uptime_seconds": round(time.time() - SERVICE_START_TIME, 1),
        "tgcrypto": TGCRYPTO_AVAILABLE,
        "render_url": RENDER_EXTERNAL_URL or None,
        "download_dir": str(DOWNLOAD_DIR),
    })


def _build_web_app() -> web.Application:
    web_app = web.Application(client_max_size=0)
    web_app.router.add_get("/", _root_handler)
    web_app.router.add_get("/health", _health_handler)
    web_app.router.add_get("/ping", _health_handler)
    web_app.router.add_get("/alive", _health_handler)
    return web_app


def _run_web_server() -> None:
    """Blocking; meant to be called inside a daemon thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def serve() -> None:
        runner = web.AppRunner(_build_web_app())
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
        await site.start()
        log.info("HTTP server listening on 0.0.0.0:%d (UptimeRobot target)", HTTP_PORT)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    try:
        loop.run_until_complete(serve())
    except Exception as e:
        log.exception("web server crashed: %s", e)
    finally:
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Periodic temp cleanup
# ---------------------------------------------------------------------------

async def _cleanup_loop() -> None:
    """Wipe stale downloads older than 1 hour. Runs in the bot's event loop."""
    while True:
        try:
            await asyncio.sleep(600)  # every 10 min
            if not DOWNLOAD_DIR.exists():
                continue
            cutoff = time.time() - 3600
            removed = 0
            for p in DOWNLOAD_DIR.iterdir():
                try:
                    if p.is_file() and p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
                        removed += 1
                except Exception:
                    continue
            if removed:
                log.info("cleanup: removed %d stale files from %s", removed, DOWNLOAD_DIR)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("cleanup error: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_SHUTDOWN = threading.Event()


def _install_signal_handlers() -> None:
    def _h(signum, _frame):
        log.info("Received signal %d, shutting down", signum)
        _SHUTDOWN.set()
    try:
        signal.signal(signal.SIGTERM, _h)
        signal.signal(signal.SIGINT, _h)
    except Exception:
        # Some environments (e.g. non-main threads) can't install handlers
        pass


def main() -> None:
    if not (API_ID and API_HASH and BOT_TOKEN):
        raise SystemExit(
            "Missing env vars. Set API_ID, API_HASH, BOT_TOKEN.\n"
            "API_ID/HASH from https://my.telegram.org — BOT_TOKEN from @BotFather."
        )

    log.info("=" * 60)
    log.info("Telegram Video Downloader Bot — Render Free Tier")
    log.info("DOWNLOAD_DIR = %s", DOWNLOAD_DIR)
    log.info("HTTP_PORT    = %d", HTTP_PORT)
    log.info("MAX_PARALLEL = %d", MAX_PARALLEL)
    log.info("tgcrypto     = %s", TGCRYPTO_AVAILABLE)
    log.info("RENDER URL   = %s", RENDER_EXTERNAL_URL or "(unset)")
    log.info("=" * 60)

    _install_signal_handlers()

    # Start HTTP health server in a daemon thread.
    web_thread = threading.Thread(
        target=_run_web_server, name="http-health", daemon=True
    )
    web_thread.start()

    # Schedule periodic temp-dir cleanup on the bot's loop once it starts.
    @app.on_message(filters.command("start"))
    async def _start_cleanup_once(*_a, **_kw):
        # Run-once via a short-lived task
        async def _kick():
            await asyncio.sleep(2)
            asyncio.create_task(_cleanup_loop())
        asyncio.create_task(_kick())

    log.info("Bot starting…")
    try:
        app.run()
    finally:
        log.info("Bot stopped. Exiting.")
        _SHUTDOWN.set()


if __name__ == "__main__":
    main()
