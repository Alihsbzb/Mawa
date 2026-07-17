# main.py
# ==============================================================================
# Manhwa Bot + Web Viewer — Single File System
# Telegram Bot (Pyrogram) + Web Reader (FastAPI) in one asyncio event loop.
# Deployed on Railway with a Volume mounted at /data.
# ==============================================================================

import os
import re
import sys
import time
import shutil
import zipfile
import asyncio
import logging
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

# ---- Third-party ----
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, ForeignKey, BigInteger,
    UniqueConstraint, func
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, scoped_session

from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from pyrogram.errors import RPCError

# ==============================================================================
# Configuration & Logging
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("manhwa")
# Quiet down noisy libraries
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# --- Environment Variables (set these in Railway) ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Public base URL of your Railway deployment, e.g. https://myapp.up.railway.app
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))

# --- Storage paths (Railway Volume mounted at /data) ---
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DOWNLOADS_DIR = DATA_DIR / "downloads"
DB_PATH = DATA_DIR / "manhwa.db"
SESSION_DIR = DATA_DIR / "session"

# --- Cleanup settings ---
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60      # run every 24h
CLEANUP_MAX_AGE_SECONDS = 48 * 60 * 60       # delete folders idle > 48h

# --- Allowed archive extensions ---
ARCHIVE_EXTS = {".zip", ".cbz"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
IGNORED_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}

# Ensure directories exist early
for _d in (DATA_DIR, DOWNLOADS_DIR, SESSION_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # pragma: no cover
        log.error("Could not create directory %s: %s", _d, e)


# ==============================================================================
# Database (SQLAlchemy + SQLite)
# ==============================================================================

Base = declarative_base()

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
# scoped_session is thread-safe; we run blocking DB calls in executor threads.
DBSession = scoped_session(SessionFactory)


class Manhwa(Base):
    __tablename__ = "manhwas"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False, unique=True)

    chapters = relationship(
        "Chapter", back_populates="manhwa",
        cascade="all, delete-orphan", order_by="Chapter.chapter_number"
    )


class Chapter(Base):
    __tablename__ = "chapters"
    id = Column(Integer, primary_key=True)
    manhwa_id = Column(Integer, ForeignKey("manhwas.id", ondelete="CASCADE"), nullable=False)
    chapter_number = Column(Float, nullable=False)   # supports 10.5 style chapters
    folder_name = Column(String, nullable=False)     # relative folder under downloads/<manhwa>/

    manhwa = relationship("Manhwa", back_populates="chapters")
    pages = relationship(
        "Page", back_populates="chapter",
        cascade="all, delete-orphan", order_by="Page.page_number"
    )

    __table_args__ = (
        UniqueConstraint("manhwa_id", "chapter_number", name="uq_manhwa_chapter"),
    )


class Page(Base):
    __tablename__ = "pages"
    id = Column(Integer, primary_key=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False)
    page_number = Column(Integer, nullable=False)
    file_name = Column(String, nullable=False)       # relative path stored on disk

    chapter = relationship("Chapter", back_populates="pages")


class UserProgress(Base):
    __tablename__ = "user_progress"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    manhwa_id = Column(Integer, ForeignKey("manhwas.id", ondelete="CASCADE"), nullable=False)
    last_chapter_id = Column(Integer, ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True)
    scroll_percentage = Column(Float, default=0.0)

    __table_args__ = (
        UniqueConstraint("user_id", "manhwa_id", name="uq_user_manhwa"),
    )


def init_db():
    """Create all tables. Enable foreign keys for SQLite."""
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_con, con_record):
        cur = dbapi_con.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    Base.metadata.create_all(engine)
    log.info("Database initialized at %s", DB_PATH)


# ==============================================================================
# Helper Utilities
# ==============================================================================

def natural_key(s: str):
    """Natural sort key: '10' > '2', so 1,2,...,10 sort correctly."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", str(s))]


def safe_filename(name: str) -> str:
    """Sanitize a string to be a safe folder/file name."""
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] if len(name) > 180 else name or "unnamed"


# --- Metadata regex parser ---
# Tries to detect a manhwa title and chapter number from a filename.
_CHAPTER_PATTERNS = [
    r"(?:chapter|chap|ch|ep|episode|فصل|قسمت)\s*[\._\-]?\s*(\d+(?:\.\d+)?)",
    r"[\-_\s]#?(\d+(?:\.\d+)?)\s*(?:$|\.)",
    r"\b(\d+(?:\.\d+)?)\b",
]


def parse_metadata(filename: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Returns (title, chapter_number). Either may be None if not confidently parsed.
    """
    stem = Path(filename).stem
    stem = re.sub(r"[\[\(].*?[\]\)]", " ", stem)  # strip [tags] and (groups)
    stem = stem.replace("_", " ").strip()

    chapter = None
    title = None

    for pat in _CHAPTER_PATTERNS:
        m = re.search(pat, stem, re.IGNORECASE)
        if m:
            try:
                chapter = float(m.group(1))
            except ValueError:
                chapter = None
            # Title = everything before the matched chapter token
            title_part = stem[: m.start()].strip(" -_.#")
            title_part = re.sub(
                r"(?:chapter|chap|ch|ep|episode|فصل|قسمت)$",
                "", title_part, flags=re.IGNORECASE
            ).strip(" -_.#")
            if len(title_part) >= 2:
                title = title_part
            break

    if title:
        title = re.sub(r"\s+", " ", title).strip()
    return (title or None, chapter)


# ==============================================================================
# Recursive Archive Extraction
# ==============================================================================

def _is_ignored(path_parts) -> bool:
    return any(part in IGNORED_NAMES or part.startswith("._") for part in path_parts)


def extract_archive_recursive(archive_path: Path, dest_dir: Path) -> List[Path]:
    """
    Recursively extract a ZIP/CBZ into dest_dir, preserving folder structure,
    ignoring system files, and recursing into nested archives.

    Returns a list of image file Paths (relative to dest_dir), in the original
    stored order of the archive.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted_images: List[Path] = []

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            # bad file test
            bad = zf.testzip()
            if bad is not None:
                raise zipfile.BadZipFile(f"Corrupt entry: {bad}")

            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = Path(info.filename).parts
                if _is_ignored(parts):
                    continue

                inner_name = Path(info.filename).name
                if not inner_name:
                    continue

                out_path = dest_dir / info.filename
                # Prevent Zip Slip path traversal
                try:
                    out_path.resolve().relative_to(dest_dir.resolve())
                except ValueError:
                    log.warning("Skipping unsafe path in zip: %s", info.filename)
                    continue

                ext = out_path.suffix.lower()

                if ext in ARCHIVE_EXTS:
                    # Nested archive — extract into a subfolder named after stem
                    tmp_nested = dest_dir / (out_path.stem + "__nested")
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    try:
                        nested_imgs = extract_archive_recursive(out_path, tmp_nested)
                        extracted_images.extend(nested_imgs)
                    finally:
                        try:
                            out_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                elif ext in IMAGE_EXTS:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted_images.append(out_path)
                # else: ignore non-image, non-archive files

    except zipfile.BadZipFile as e:
        raise RuntimeError(f"فایل آرشیو خراب است: {e}")
    except Exception as e:
        raise RuntimeError(f"خطا در استخراج فایل: {e}")

    return extracted_images


def order_images(images: List[Path], base_dir: Path, smart_sort: bool) -> List[Path]:
    """
    Order images either by original archive order (as given) or by natural sort
    of their relative path when smart_sort is enabled.
    """
    if not smart_sort:
        return images
    return sorted(images, key=lambda p: natural_key(str(p.relative_to(base_dir))))


# ==============================================================================
# Database access helpers (run in executor to avoid blocking the loop)
# ==============================================================================

def db_get_or_create_manhwa(title: str) -> int:
    s = DBSession()
    try:
        m = s.query(Manhwa).filter(func.lower(Manhwa.title) == title.lower()).first()
        if not m:
            m = Manhwa(title=title)
            s.add(m)
            s.commit()
        return m.id
    finally:
        DBSession.remove()


def db_add_chapter(manhwa_id: int, chapter_number: float, folder_name: str,
                   pages: List[str]) -> int:
    """Insert (or replace) a chapter and its pages. Returns chapter id."""
    s = DBSession()
    try:
        existing = s.query(Chapter).filter_by(
            manhwa_id=manhwa_id, chapter_number=chapter_number
        ).first()
        if existing:
            # replace pages
            for p in list(existing.pages):
                s.delete(p)
            existing.folder_name = folder_name
            s.flush()
            ch = existing
        else:
            ch = Chapter(
                manhwa_id=manhwa_id,
                chapter_number=chapter_number,
                folder_name=folder_name,
            )
            s.add(ch)
            s.flush()

        for idx, fname in enumerate(pages, start=1):
            s.add(Page(chapter_id=ch.id, page_number=idx, file_name=fname))
        s.commit()
        return ch.id
    except Exception:
        s.rollback()
        raise
    finally:
        DBSession.remove()


def db_next_chapter_number(manhwa_id: int) -> float:
    s = DBSession()
    try:
        mx = s.query(func.max(Chapter.chapter_number)).filter_by(
            manhwa_id=manhwa_id).scalar()
        return float((mx or 0) + 1)
    finally:
        DBSession.remove()


def db_list_manhwas() -> List[dict]:
    s = DBSession()
    try:
        rows = s.query(Manhwa).order_by(Manhwa.title).all()
        out = []
        for m in rows:
            count = s.query(func.count(Chapter.id)).filter_by(manhwa_id=m.id).scalar()
            out.append({"id": m.id, "title": m.title, "chapters": count})
        return out
    finally:
        DBSession.remove()


def db_get_manhwa(manhwa_id: int) -> Optional[dict]:
    s = DBSession()
    try:
        m = s.query(Manhwa).get(manhwa_id)
        if not m:
            return None
        chapters = [
            {"id": c.id, "number": c.chapter_number, "folder": c.folder_name}
            for c in sorted(m.chapters, key=lambda x: x.chapter_number)
        ]
        return {"id": m.id, "title": m.title, "chapters": chapters}
    finally:
        DBSession.remove()


def db_get_chapter(chapter_id: int) -> Optional[dict]:
    s = DBSession()
    try:
        c = s.query(Chapter).get(chapter_id)
        if not c:
            return None
        pages = [{"n": p.page_number, "file": p.file_name}
                 for p in sorted(c.pages, key=lambda x: x.page_number)]
        # neighbors
        siblings = sorted(c.manhwa.chapters, key=lambda x: x.chapter_number)
        idx = next((i for i, x in enumerate(siblings) if x.id == c.id), None)
        prev_id = siblings[idx - 1].id if idx not in (None, 0) else None
        next_id = siblings[idx + 1].id if (idx is not None and idx < len(siblings) - 1) else None
        return {
            "id": c.id,
            "number": c.chapter_number,
            "manhwa_id": c.manhwa_id,
            "manhwa_title": c.manhwa.title,
            "folder": c.folder_name,
            "pages": pages,
            "prev_id": prev_id,
            "next_id": next_id,
        }
    finally:
        DBSession.remove()


def db_save_progress(user_id: int, manhwa_id: int, chapter_id: int, scroll: float):
    s = DBSession()
    try:
        row = s.query(UserProgress).filter_by(
            user_id=user_id, manhwa_id=manhwa_id).first()
        if row:
            row.last_chapter_id = chapter_id
            row.scroll_percentage = scroll
        else:
            row = UserProgress(
                user_id=user_id, manhwa_id=manhwa_id,
                last_chapter_id=chapter_id, scroll_percentage=scroll
            )
            s.add(row)
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        DBSession.remove()


def db_get_progress(user_id: int, manhwa_id: int) -> Optional[dict]:
    s = DBSession()
    try:
        row = s.query(UserProgress).filter_by(
            user_id=user_id, manhwa_id=manhwa_id).first()
        if not row:
            return None
        return {
            "last_chapter_id": row.last_chapter_id,
            "scroll_percentage": row.scroll_percentage or 0.0,
        }
    finally:
        DBSession.remove()


async def run_db(func_, *args):
    """Run a blocking DB function in the default threadpool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func_(*args))


# ==============================================================================
# Telegram Bot (Pyrogram)
# ==============================================================================

bot = Client(
    name="manhwa_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(SESSION_DIR),
)

# In-memory per-user state
# user_state[user_id] = {
#   "smart_sort": bool,
#   "pending_title": Optional[str],   # last known manhwa title to attach chapters
#   "awaiting_title_for": Optional[dict]  # job waiting for a manual title
# }
user_state: dict = {}

# Per-user processing queue: user_id -> asyncio.Queue
user_queues: dict = {}
user_workers: dict = {}


def get_state(uid: int) -> dict:
    if uid not in user_state:
        user_state[uid] = {
            "smart_sort": False,
            "pending_title": None,
            "awaiting_title_for": None,
        }
    return user_state[uid]


def T(key: str, **kw) -> str:
    """Persian text templates."""
    texts = {
        "start": (
            "سلام! 👋\n\n"
            "به ربات مدیریت مانهوا خوش اومدی.\n"
            "کافیه فایل‌های ZIP یا CBZ مانهوا رو برام بفرستی تا استخراج و "
            "توی وب‌ریدر آماده‌شون کنم. 📚\n\n"
            "می‌تونی چند فایل رو با هم بفرستی؛ به‌ترتیب پردازش می‌شن.\n\n"
            "دستورات:\n"
            "• /library — کتابخانه و لینک مطالعه\n"
            "• /settings — تنظیمات مرتب‌سازی هوشمند\n"
        ),
        "queued": "📥 «{name}» به صف اضافه شد. جایگاه در صف: {pos}",
        "processing": "⚙️ در حال پردازش «{name}» ...",
        "extracting": "📦 در حال استخراج تصاویر «{name}» ...",
        "done": (
            "✅ «{title}» - فصل {chapter} با {pages} صفحه آماده شد!\n\n"
            "📖 مطالعه: {url}"
        ),
        "corrupt": "❌ فایل «{name}» خراب یا نامعتبر است و پردازش نشد.",
        "no_images": "⚠️ در فایل «{name}» هیچ تصویری پیدا نشد.",
        "ask_title": (
            "🤔 نتونستم اسم مانهوا رو از فایل «{name}» تشخیص بدم.\n"
            "لطفاً اسم مانهوا رو تایپ کن تا این فصل و فصل‌های بعدی بهش وصل بشن."
        ),
        "title_saved": "📝 اسم «{title}» ثبت شد. حالا در حال ادامه پردازش...",
        "settings": "⚙️ تنظیمات:\n\nمرتب‌سازی هوشمند (Natural Sort): {status}",
        "smart_on": "روشن ✅",
        "smart_off": "خاموش ❌",
        "smart_toggled": "مرتب‌سازی هوشمند {status} شد.",
        "empty_library": "کتابخونه‌ات خالیه. یه فایل ZIP/CBZ بفرست تا شروع کنیم! 📚",
        "library_header": "📚 کتابخانه شما:\n",
        "no_public_url": (
            "⚠️ آدرس عمومی (PUBLIC_URL) تنظیم نشده. "
            "لطفاً در تنظیمات Railway مقدارش رو وارد کن."
        ),
        "error": "❌ خطایی رخ داد: {err}",
    }
    return texts.get(key, key).format(**kw)


def build_url(path: str) -> str:
    base = PUBLIC_URL if PUBLIC_URL else f"http://localhost:{PORT}"
    return f"{base}{path}"


@bot.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: Message):
    get_state(message.from_user.id)
    await message.reply_text(T("start"))


@bot.on_message(filters.command("settings") & filters.private)
async def cmd_settings(client: Client, message: Message):
    st = get_state(message.from_user.id)
    status = T("smart_on") if st["smart_sort"] else T("smart_off")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔄 تغییر مرتب‌سازی هوشمند", callback_data="toggle_smart"
        )
    ]])
    await message.reply_text(T("settings", status=status), reply_markup=kb)


@bot.on_message(filters.command("library") & filters.private)
async def cmd_library(client: Client, message: Message):
    manhwas = await run_db(db_list_manhwas)
    if not manhwas:
        await message.reply_text(T("empty_library"))
        return
    if not PUBLIC_URL:
        await message.reply_text(T("no_public_url"))

    rows = []
    text = T("library_header")
    for m in manhwas:
        text += f"\n• {m['title']} ({m['chapters']} فصل)"
        rows.append([InlineKeyboardButton(
            f"📖 {m['title']}",
            url=build_url(f"/m/{m['id']}?uid={message.from_user.id}")
        )])
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


@bot.on_callback_query(filters.regex("^toggle_smart$"))
async def cb_toggle_smart(client: Client, cq: CallbackQuery):
    st = get_state(cq.from_user.id)
    st["smart_sort"] = not st["smart_sort"]
    status = T("smart_on") if st["smart_sort"] else T("smart_off")
    await cq.answer(T("smart_toggled", status=status), show_alert=False)
    try:
        await cq.message.edit_text(
            T("settings", status=status),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 تغییر مرتب‌سازی هوشمند",
                                     callback_data="toggle_smart")
            ]])
        )
    except RPCError:
        pass


@bot.on_message(filters.document & filters.private)
async def on_document(client: Client, message: Message):
    doc = message.document
    fname = doc.file_name or "archive.zip"
    ext = Path(fname).suffix.lower()
    if ext not in ARCHIVE_EXTS:
        await message.reply_text(
            "⚠️ فقط فایل‌های ZIP یا CBZ پشتیبانی می‌شن."
        )
        return

    uid = message.from_user.id
    get_state(uid)

    # enqueue job
    if uid not in user_queues:
        user_queues[uid] = asyncio.Queue()
        user_workers[uid] = asyncio.create_task(queue_worker(uid))

    q = user_queues[uid]
    pos = q.qsize() + 1
    job = {
        "message": message,
        "file_name": fname,
        "client": client,
    }
    await q.put(job)
    await message.reply_text(T("queued", name=fname, pos=pos))


@bot.on_message(filters.text & filters.private & ~filters.command(["start", "settings", "library"]))
async def on_text(client: Client, message: Message):
    """Handle manual manhwa title input when awaiting."""
    uid = message.from_user.id
    st = get_state(uid)
    if st.get("awaiting_title_for"):
        title = safe_filename(message.text.strip())
        st["pending_title"] = title
        pending_job = st["awaiting_title_for"]
        st["awaiting_title_for"] = None
        # resume the paused job by resolving its future
        fut = pending_job.get("title_future")
        if fut and not fut.done():
            fut.set_result(title)
        await message.reply_text(T("title_saved", title=title))
    else:
        await message.reply_text(
            "برای شروع یه فایل ZIP/CBZ بفرست یا از /library استفاده کن. 🙂"
        )


async def queue_worker(uid: int):
    """Process one user's jobs sequentially."""
    q = user_queues[uid]
    while True:
        job = await q.get()
        try:
            await process_job(uid, job)
        except Exception as e:
            log.exception("Job failed for user %s", uid)
            try:
                await job["message"].reply_text(T("error", err=str(e)))
            except Exception:
                pass
        finally:
            q.task_done()


async def process_job(uid: int, job: dict):
    message: Message = job["message"]
    fname: str = job["file_name"]
    st = get_state(uid)

    status_msg = await message.reply_text(T("processing", name=fname))

    # 1) Download the archive to a temp location
    tmp_download = DOWNLOADS_DIR / "_tmp" / f"{uid}_{secrets.token_hex(4)}"
    tmp_download.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_download / safe_filename(fname)

    try:
        await message.download(file_name=str(archive_path))
    except Exception as e:
        await status_msg.edit_text(T("corrupt", name=fname))
        shutil.rmtree(tmp_download, ignore_errors=True)
        return

    # 2) Parse metadata
    title, chapter = parse_metadata(fname)

    # If no title parsed, try pending title, else ask the user.
    if not title:
        if st.get("pending_title"):
            title = st["pending_title"]
        else:
            title = await ask_for_title(uid, message, fname)
            if not title:
                await status_msg.edit_text(T("corrupt", name=fname))
                shutil.rmtree(tmp_download, ignore_errors=True)
                return

    st["pending_title"] = title  # remember for subsequent files

    # 3) Extract recursively
    await status_msg.edit_text(T("extracting", name=fname))
    extract_root = tmp_download / "extracted"
    try:
        images = await run_db(extract_archive_recursive, archive_path, extract_root)
    except RuntimeError as e:
        await status_msg.edit_text(T("corrupt", name=fname))
        shutil.rmtree(tmp_download, ignore_errors=True)
        return

    if not images:
        await status_msg.edit_text(T("no_images", name=fname))
        shutil.rmtree(tmp_download, ignore_errors=True)
        return

    images = order_images(images, extract_root, st["smart_sort"])

    # 4) Determine chapter number
    manhwa_id = await run_db(db_get_or_create_manhwa, title)
    if chapter is None:
        chapter = await run_db(db_next_chapter_number, manhwa_id)

    # 5) Move images into final destination:
    #    downloads/<manhwa_id>/<chapter_folder>/pageNNN.ext
    safe_title_dir = safe_filename(title)
    chapter_folder = f"chapter_{chapter:g}"
    final_dir = DOWNLOADS_DIR / str(manhwa_id) / chapter_folder
    if final_dir.exists():
        shutil.rmtree(final_dir, ignore_errors=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    page_files = []
    for idx, img in enumerate(images, start=1):
        ext = img.suffix.lower()
        new_name = f"page_{idx:04d}{ext}"
        dest = final_dir / new_name
        try:
            shutil.move(str(img), str(dest))
        except Exception:
            shutil.copy2(str(img), str(dest))
        # store path relative to DOWNLOADS_DIR
        page_files.append(str(dest.relative_to(DOWNLOADS_DIR)))

    # touch the folder to mark access time
    final_dir.touch(exist_ok=True)

    # 6) Save to DB
    chapter_id = await run_db(
        db_add_chapter, manhwa_id, chapter, chapter_folder, page_files
    )

    # cleanup temp
    shutil.rmtree(tmp_download, ignore_errors=True)

    # 7) Notify user
    url = build_url(f"/read/{chapter_id}?uid={uid}")
    await status_msg.edit_text(
        T("done", title=title, chapter=f"{chapter:g}",
          pages=len(page_files), url=url)
    )


async def ask_for_title(uid: int, message: Message, fname: str) -> Optional[str]:
    """Ask the user to type a title and wait for their reply."""
    st = get_state(uid)
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    st["awaiting_title_for"] = {"file_name": fname, "title_future": fut}
    await message.reply_text(T("ask_title", name=fname))
    try:
        title = await asyncio.wait_for(fut, timeout=300)  # 5 min
        return safe_filename(title)
    except asyncio.TimeoutError:
        st["awaiting_title_for"] = None
        await message.reply_text("⏱️ زمان وارد کردن اسم تموم شد. دوباره فایل رو بفرست.")
        return None


# ==============================================================================
# Background Cleanup Task
# ==============================================================================

async def cleanup_task():
    """Every 24h, delete chapter folders idle for > 48h to save space."""
    while True:
        try:
            now = time.time()
            deleted = 0
            if DOWNLOADS_DIR.exists():
                for manhwa_dir in DOWNLOADS_DIR.iterdir():
                    if manhwa_dir.name == "_tmp":
                        # clean stale temp
                        for tmp in manhwa_dir.glob("*"):
                            try:
                                if now - tmp.stat().st_mtime > CLEANUP_MAX_AGE_SECONDS:
                                    shutil.rmtree(tmp, ignore_errors=True)
                            except Exception:
                                pass
                        continue
                    if not manhwa_dir.is_dir():
                        continue
                    for chapter_dir in manhwa_dir.iterdir():
                        if not chapter_dir.is_dir():
                            continue
                        try:
                            # use the most recent access/modify time in folder
                            last = chapter_dir.stat().st_atime
                            for f in chapter_dir.rglob("*"):
                                try:
                                    last = max(last, f.stat().st_atime)
                                except Exception:
                                    pass
                            if now - last > CLEANUP_MAX_AGE_SECONDS:
                                shutil.rmtree(chapter_dir, ignore_errors=True)
                                deleted += 1
                                log.info("Cleanup removed idle folder: %s", chapter_dir)
                        except Exception as e:
                            log.warning("Cleanup error on %s: %s", chapter_dir, e)
            log.info("Cleanup cycle complete. Removed %d folder(s).", deleted)
        except Exception:
            log.exception("Cleanup task error")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


# ==============================================================================
# FastAPI Web Viewer
# ==============================================================================

app = FastAPI(title="Manhwa Web Viewer")

# Serve raw images directly from the downloads folder.
app.mount("/img", StaticFiles(directory=str(DOWNLOADS_DIR)), name="img")


# ---------- HTML templates (embedded) ----------

BASE_HEAD = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes"/>
<title>{title}</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  html, body { margin:0; padding:0; background:#0a0a0a; color:#e5e5e5;
               font-family: Tahoma, "Vazirmatn", sans-serif; }
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-thumb { background:#333; border-radius:3px; }
  .reader-img { width:100%; height:auto; display:block; }
</style>
</head>
"""


def render_library(manhwas: List[dict], uid: str) -> str:
    cards = ""
    if not manhwas:
        cards = '<p class="text-center text-neutral-500 mt-20">کتابخانه خالی است.</p>'
    for m in manhwas:
        cards += f"""
        <a href="/m/{m['id']}?uid={uid}"
           class="block bg-neutral-900 hover:bg-neutral-800 transition rounded-2xl p-4 mb-3 border border-neutral-800">
          <div class="flex items-center justify-between">
            <span class="text-lg font-bold">{m['title']}</span>
            <span class="text-sm text-emerald-400">{m['chapters']} فصل</span>
          </div>
        </a>"""
    head = BASE_HEAD.format(title="کتابخانه مانهوا")
    return f"""{head}
<body>
  <div class="max-w-2xl mx-auto p-4">
    <h1 class="text-2xl font-extrabold mb-6 text-center">📚 کتابخانه مانهوا</h1>
    {cards}
  </div>
</body></html>"""


def render_manhwa(m: dict, uid: str, resume_chapter: Optional[int]) -> str:
    chapters = ""
    for c in m["chapters"]:
        resume_badge = ""
        if resume_chapter and c["id"] == resume_chapter:
            resume_badge = '<span class="text-xs bg-emerald-600 px-2 py-0.5 rounded-full">ادامه</span>'
        chapters += f"""
        <a href="/read/{c['id']}?uid={uid}"
           class="flex items-center justify-between bg-neutral-900 hover:bg-neutral-800 transition rounded-xl p-3 mb-2 border border-neutral-800">
          <span>فصل {c['number']:g}</span>
          {resume_badge}
        </a>"""
    if not m["chapters"]:
        chapters = '<p class="text-center text-neutral-500 mt-10">هنوز فصلی اضافه نشده.</p>'

    resume_btn = ""
    if resume_chapter:
        resume_btn = f"""
        <a href="/read/{resume_chapter}?uid={uid}"
           class="block text-center bg-emerald-600 hover:bg-emerald-500 transition rounded-xl p-3 mb-4 font-bold">
           ▶️ ادامه مطالعه
        </a>"""

    head = BASE_HEAD.format(title=m["title"])
    return f"""{head}
<body>
  <div class="max-w-2xl mx-auto p-4">
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-extrabold">{m['title']}</h1>
      <a href="/?uid={uid}" class="text-sm text-neutral-400">بازگشت</a>
    </div>
    {resume_btn}
    {chapters}
  </div>
</body></html>"""


def render_reader(chapter: dict, uid: str, resume_scroll: float) -> str:
    imgs = ""
    for p in chapter["pages"]:
        src = f"/img/{p['file']}"
        imgs += (f'<img class="reader-img" loading="lazy" '
                 f'src="{src}" alt="page {p["n"]}"/>')

    prev_btn = (
        f'<a href="/read/{chapter["prev_id"]}?uid={uid}" '
        f'class="flex-1 text-center bg-neutral-800 hover:bg-neutral-700 rounded-xl py-3 mx-1">'
        f'فصل قبلی</a>'
        if chapter["prev_id"] else
        '<span class="flex-1 text-center bg-neutral-900 text-neutral-600 rounded-xl py-3 mx-1">فصل قبلی</span>'
    )
    next_btn = (
        f'<a href="/read/{chapter["next_id"]}?uid={uid}" '
        f'class="flex-1 text-center bg-emerald-600 hover:bg-emerald-500 rounded-xl py-3 mx-1">'
        f'فصل بعدی</a>'
        if chapter["next_id"] else
        '<span class="flex-1 text-center bg-neutral-900 text-neutral-600 rounded-xl py-3 mx-1">فصل بعدی</span>'
    )

    head = BASE_HEAD.format(title=f"{chapter['manhwa_title']} - فصل {chapter['number']:g}")

    # JS: pinch-to-zoom + scroll progress persistence + auto-resume
    js = f"""
<script>
const CHAPTER_ID = {chapter['id']};
const MANHWA_ID = {chapter['manhwa_id']};
const UID = "{uid}";
const RESUME_SCROLL = {resume_scroll};

// ---------- Auto-resume scroll ----------
window.addEventListener('load', () => {{
  // wait a moment for images to begin loading, then restore scroll
  setTimeout(() => {{
    const docH = document.documentElement.scrollHeight - window.innerHeight;
    if (docH > 0 && RESUME_SCROLL > 0) {{
      window.scrollTo(0, docH * (RESUME_SCROLL / 100));
    }}
  }}, 600);
}});

// ---------- Throttled progress reporting ----------
let lastSent = 0;
function reportProgress() {{
  const now = Date.now();
  if (now - lastSent < 2000) return;   // throttle 2s
  lastSent = now;
  const docH = document.documentElement.scrollHeight - window.innerHeight;
  const pct = docH > 0 ? Math.min(100, (window.scrollY / docH) * 100) : 0;
  fetch('/api/progress', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      user_id: UID, manhwa_id: MANHWA_ID,
      chapter_id: CHAPTER_ID, scroll: pct
    }}),
    keepalive: true
  }}).catch(()=>{{}});
}}
window.addEventListener('scroll', reportProgress, {{passive:true}});
window.addEventListener('beforeunload', () => {{ lastSent = 0; reportProgress(); }});

// ---------- Lightweight pinch-to-zoom & pan ----------
(function(){{
  const container = document.getElementById('pages');
  let scale = 1, panX = 0, panY = 0;
  let startDist = 0, startScale = 1;
  let startX = 0, startY = 0, startPanX = 0, startPanY = 0;
  let isPanning = false;

  function apply() {{
    container.style.transform =
      `translate(${{panX}}px, ${{panY}}px) scale(${{scale}})`;
    container.style.transformOrigin = 'center top';
  }}
  function dist(t) {{
    const dx = t[0].clientX - t[1].clientX;
    const dy = t[0].clientY - t[1].clientY;
    return Math.hypot(dx, dy);
  }}

  container.addEventListener('touchstart', (e) => {{
    if (e.touches.length === 2) {{
      startDist = dist(e.touches);
      startScale = scale;
    }} else if (e.touches.length === 1 && scale > 1) {{
      isPanning = true;
      startX = e.touches[0].clientX; startY = e.touches[0].clientY;
      startPanX = panX; startPanY = panY;
    }}
  }}, {{passive:true}});

  container.addEventListener('touchmove', (e) => {{
    if (e.touches.length === 2) {{
      const d = dist(e.touches);
      scale = Math.min(5, Math.max(1, startScale * (d / startDist)));
      if (scale === 1) {{ panX = 0; panY = 0; }}
      apply();
      e.preventDefault();
    }} else if (isPanning && e.touches.length === 1 && scale > 1) {{
      panX = startPanX + (e.touches[0].clientX - startX);
      panY = startPanY + (e.touches[0].clientY - startY);
      apply();
      e.preventDefault();
    }}
  }}, {{passive:false}});

  container.addEventListener('touchend', (e) => {{
    if (e.touches.length === 0) {{
      isPanning = false;
      if (scale <= 1.02) {{ scale = 1; panX = 0; panY = 0; apply(); }}
    }}
  }});

  // double-tap to reset zoom
  let lastTap = 0;
  container.addEventListener('touchend', (e) => {{
    const now = Date.now();
    if (now - lastTap < 300) {{
      scale = 1; panX = 0; panY = 0; apply();
    }}
    lastTap = now;
  }});
}})();
</script>
"""

    return f"""{head}
<body class="bg-black">
  <div class="sticky top-0 z-20 bg-neutral-950/90 backdrop-blur border-b border-neutral-800 px-4 py-2 flex items-center justify-between">
    <a href="/m/{chapter['manhwa_id']}?uid={uid}" class="text-sm text-neutral-300">← فهرست فصل‌ها</a>
    <span class="text-sm font-bold">{chapter['manhwa_title']} — فصل {chapter['number']:g}</span>
  </div>

  <div id="pages" class="mx-auto max-w-3xl select-none">
    {imgs}
  </div>

  <div class="sticky bottom-0 z-20 bg-neutral-950/90 backdrop-blur border-t border-neutral-800 p-2 flex">
    {prev_btn}
    {next_btn}
  </div>
  {js}
</body></html>"""


# ---------- Routes ----------

@app.get("/", response_class=HTMLResponse)
async def web_index(uid: str = "0"):
    manhwas = await run_db(db_list_manhwas)
    return HTMLResponse(render_library(manhwas, uid))


@app.get("/m/{manhwa_id}", response_class=HTMLResponse)
async def web_manhwa(manhwa_id: int, uid: str = "0"):
    m = await run_db(db_get_manhwa, manhwa_id)
    if not m:
        raise HTTPException(status_code=404, detail="مانهوا پیدا نشد")
    resume_chapter = None
    try:
        prog = await run_db(db_get_progress, int(uid), manhwa_id)
        if prog:
            resume_chapter = prog.get("last_chapter_id")
    except (ValueError, TypeError):
        pass
    return HTMLResponse(render_manhwa(m, uid, resume_chapter))


@app.get("/read/{chapter_id}", response_class=HTMLResponse)
async def web_read(chapter_id: int, uid: str = "0"):
    chapter = await run_db(db_get_chapter, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="فصل پیدا نشد")

    # touch chapter folder to update access time (prevents premature cleanup)
    try:
        folder = DOWNLOADS_DIR / str(chapter["manhwa_id"]) / chapter["folder"]
        if folder.exists():
            os.utime(folder, None)
    except Exception:
        pass

    resume_scroll = 0.0
    try:
        prog = await run_db(db_get_progress, int(uid), chapter["manhwa_id"])
        if prog and prog.get("last_chapter_id") == chapter_id:
            resume_scroll = prog.get("scroll_percentage", 0.0)
    except (ValueError, TypeError):
        pass

    return HTMLResponse(render_reader(chapter, uid, resume_scroll))


@app.post("/api/progress")
async def api_progress(request: Request):
    try:
        data = await request.json()
        user_id = int(data.get("user_id", 0))
        manhwa_id = int(data["manhwa_id"])
        chapter_id = int(data["chapter_id"])
        scroll = float(data.get("scroll", 0.0))
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "bad payload"}, status_code=400)

    if user_id <= 0:
        # anonymous viewers: nothing to persist
        return JSONResponse({"ok": True, "saved": False})

    try:
        await run_db(db_save_progress, user_id, manhwa_id, chapter_id, scroll)
        return JSONResponse({"ok": True, "saved": True})
    except Exception as e:
        log.warning("progress save failed: %s", e)
        return JSONResponse({"ok": False}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ==============================================================================
# Application Bootstrap — run Bot + Web + Cleanup in one event loop
# ==============================================================================

async def main():
    # sanity checks
    missing = [k for k, v in {
        "API_ID": API_ID, "API_HASH": API_HASH, "BOT_TOKEN": BOT_TOKEN
    }.items() if not v]
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        log.error("Set them in Railway Variables before deploying.")
        # Still start the web server so /health works, but bot won't run.

    init_db()

    # Configure uvicorn to run inside the current event loop
    config = uvicorn.Config(
        app, host="0.0.0.0", port=PORT, log_level="info", loop="asyncio"
    )
    server = uvicorn.Server(config)

    tasks = [asyncio.create_task(server.serve()),
             asyncio.create_task(cleanup_task())]

    # Start the bot only if credentials exist
    if not missing:
        await bot.start()
        me = await bot.get_me()
        log.info("Bot started as @%s", me.username)
    else:
        log.warning("Bot NOT started (missing credentials). Web server only.")

    log.info("Web server running on port %s (public: %s)",
             PORT, PUBLIC_URL or "not set")

    try:
        await asyncio.gather(*tasks)
    finally:
        if not missing:
            try:
                await bot.stop()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
