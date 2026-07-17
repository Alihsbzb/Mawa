# main.py
# ==============================================================================
# سیستم یکپارچه مانهواخوان - نسخه سازگار با پایتون 3.13
# ربات تلگرام (Pyrogram) + وب‌ریدر (FastAPI) در یک فایل پایتون
# طراحی شده برای استقرار روی Railway با دیسک ماندگار Volume در مسیر /data
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
from datetime import datetime
from typing import Optional, List, Tuple

# ---- کتابخانه‌های جانبی ----
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
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
# تنظیمات اولیه و لاگ‌ها
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("manhwa_system")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# --- متغیرهای محیطی Railway ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))

# --- مسیرهای ذخیره‌سازی روی Volume ماندگار ---
DATA_DIR = Path("/data")
DOWNLOADS_DIR = DATA_DIR / "downloads"
DB_PATH = DATA_DIR / "manhwa.db"
SESSION_DIR = DATA_DIR / "session"

# --- تنظیمات حذف خودکار فایل‌ها (براساس ثانیه) ---
CLEANUP_INTERVAL = 24 * 60 * 60      # هر ۲۴ ساعت یکبار اجرا می‌شود
MAX_IDLE_TIME = 48 * 60 * 60         # مانهواهایی که ۴۸ ساعت خوانده نشده‌اند حذف می‌شوند

# --- فرمت‌های مجاز ---
ARCHIVE_EXTS = {".zip", ".cbz"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
IGNORED_FILES = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}

# اطمینان از وجود پوشه‌ها
for path in (DATA_DIR, DOWNLOADS_DIR, SESSION_DIR):
    path.mkdir(parents=True, exist_ok=True)

# ==============================================================================
# پایگاه داده (SQLAlchemy + SQLite)
# ==============================================================================

Base = declarative_base()
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
DBSession = scoped_session(SessionFactory)

class Manhwa(Base):
    __tablename__ = "manhwas"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False, unique=True)
    chapters = relationship("Chapter", back_populates="manhwa", cascade="all, delete-orphan")

class Chapter(Base):
    __tablename__ = "chapters"
    id = Column(Integer, primary_key=True)
    manhwa_id = Column(Integer, ForeignKey("manhwas.id", ondelete="CASCADE"), nullable=False)
    chapter_number = Column(Float, nullable=False)
    folder_name = Column(String, nullable=False)

    manhwa = relationship("Manhwa", back_populates="chapters")
    pages = relationship("Page", back_populates="chapter", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("manhwa_id", "chapter_number", name="uq_manhwa_chapter"),)

class Page(Base):
    __tablename__ = "pages"
    id = Column(Integer, primary_key=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False)
    page_number = Column(Integer, nullable=False)
    file_name = Column(String, nullable=False)

    chapter = relationship("Chapter", back_populates="pages")

class UserProgress(Base):
    __tablename__ = "user_progress"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    manhwa_id = Column(Integer, nullable=False)
    last_chapter_id = Column(Integer, nullable=True)
    scroll_percentage = Column(Float, default=0.0)
    __table_args__ = (UniqueConstraint("user_id", "manhwa_id", name="uq_user_manhwa"),)

def init_db():
    from sqlalchemy import event
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
    Base.metadata.create_all(engine)
    log.info("Database active at %s", DB_PATH)

# ==============================================================================
# توابع کاربردی و کمکی پایتون
# ==============================================================================

def natural_sort_key(s: str):
    """مرتب‌سازی طبیعی اعداد: قرار گرفتن عدد ۲ قبل از ۱۰"""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", str(s))]

def sanitize_folder_name(name: str) -> str:
    """ایمن‌سازی نام پوشه‌ها برای جلوگیری از ارور سیستم‌عامل"""
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] if len(name) > 150 else name or "unnamed"

def extract_chapter_info(filename: str) -> Tuple[Optional[str], Optional[float]]:
    """استخراج هوشمند اسم مانهوا و شماره چپتر با الگوهای مختلف"""
    stem = Path(filename).stem
    stem = re.sub(r"[\[\(].*?[\]\)]", " ", stem)  # حذف تگ‌ها داخل [] و ()
    stem = stem.replace("_", " ").strip()

    chapter_num = None
    manhwa_title = None

    patterns = [
        r"(?:chapter|chap|ch|ep|episode|فصل|قسمت)\s*[\._\-]?\s*(\d+(?:\.\d+)?)",
        r"[\-_\s]#?(\d+(?:\.\d+)?)\s*(?:$|\.)",
        r"\b(\d+(?:\.\d+)?)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, stem, re.IGNORECASE)
        if match:
            try:
                chapter_num = float(match.group(1))
            except ValueError:
                continue
            title_part = stem[:match.start()].strip(" -_.#")
            title_part = re.sub(r"(?:chapter|chap|ch|ep|episode|فصل|قسمت)$", "", title_part, flags=re.IGNORECASE).strip(" -_.#")
            if len(title_part) >= 2:
                manhwa_title = title_part
            break

    if manhwa_title:
        manhwa_title = re.sub(r"\s+", " ", manhwa_title).strip()
    return (manhwa_title, chapter_num)

# ==============================================================================
# استخراج هوشمند و بازگشتی فایل‌های فشرده
# ==============================================================================

def unzip_recursive(archive_path: Path, target_dir: Path) -> List[Path]:
    """استخراج بازگشتی فایل‌های فشرده (حتی زیپ‌های درون زیپ) و استخراج تصاویر"""
    target_dir.mkdir(parents=True, exist_ok=True)
    images_list: List[Path] = []

    try:
        with zipfile.ZipFile(archive_path, "r") as ref:
            if ref.testzip() is not None:
                raise zipfile.BadZipFile("فایل فشرده خراب است.")

            for file_info in ref.infolist():
                if file_info.is_dir():
                    continue
                parts = Path(file_info.filename).parts
                if any(p in IGNORED_FILES or p.startswith("._") for p in parts):
                    continue

                out_path = target_dir / file_info.filename
                # جلوگیری از حملات امنیتی Zip Slip
                try:
                    out_path.resolve().relative_to(target_dir.resolve())
                except ValueError:
                    continue

                ext = out_path.suffix.lower()
                if ext in ARCHIVE_EXTS:
                    # استخراج آرشیو داخلی
                    temp_nested = target_dir / (out_path.stem + "_nested")
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with ref.open(file_info) as src, open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    try:
                        images_list.extend(unzip_recursive(out_path, temp_nested))
                    finally:
                        out_path.unlink(missing_ok=True)
                elif ext in IMAGE_EXTS:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with ref.open(file_info) as src, open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    images_list.append(out_path)
    except Exception as e:
        raise RuntimeError(f"خطا در پردازش فایل فشرده: {e}")

    return images_list

# ==============================================================================
# توابع کار با پایگاه داده (دریافت و ثبت ناهمگام)
# ==============================================================================

def db_get_manhwa_id(title: str) -> int:
    session = DBSession()
    try:
        row = session.query(Manhwa).filter(func.lower(Manhwa.title) == title.lower()).first()
        if not row:
            row = Manhwa(title=title)
            session.add(row)
            session.commit()
        return row.id
    finally:
        DBSession.remove()

def db_save_chapter(manhwa_id: int, num: float, folder: str, pages: List[str]) -> int:
    session = DBSession()
    try:
        chapter = session.query(Chapter).filter_by(manhwa_id=manhwa_id, chapter_number=num).first()
        if chapter:
            for p in list(chapter.pages):
                session.delete(p)
            chapter.folder_name = folder
            session.flush()
        else:
            chapter = Chapter(manhwa_id=manhwa_id, chapter_number=num, folder_name=folder)
            session.add(chapter)
            session.flush()

        for idx, name in enumerate(pages, start=1):
            session.add(Page(chapter_id=chapter.id, page_number=idx, file_name=name))
        session.commit()
        return chapter.id
    except Exception:
        session.rollback()
        raise
    finally:
        DBSession.remove()

def db_next_chapter_val(manhwa_id: int) -> float:
    session = DBSession()
    try:
        val = session.query(func.max(Chapter.chapter_number)).filter_by(manhwa_id=manhwa_id).scalar()
        return float((val or 0) + 1)
    finally:
        DBSession.remove()

def db_get_all_manhwas() -> List[dict]:
    session = DBSession()
    try:
        rows = session.query(Manhwa).order_by(Manhwa.title).all()
        result = []
        for r in rows:
            count = session.query(func.count(Chapter.id)).filter_by(manhwa_id=r.id).scalar()
            result.append({"id": r.id, "title": r.title, "chapters": count})
        return result
    finally:
        DBSession.remove()

def db_get_manhwa_details(m_id: int) -> Optional[dict]:
    session = DBSession()
    try:
        m = session.query(Manhwa).get(m_id)
        if not m:
            return None
        ch_list = [{"id": c.id, "number": c.chapter_number, "folder": c.folder_name} for c in m.chapters]
        ch_list.sort(key=lambda x: x["number"])
        return {"id": m.id, "title": m.title, "chapters": ch_list}
    finally:
        DBSession.remove()

def db_get_chapter_details(c_id: int) -> Optional[dict]:
    session = DBSession()
    try:
        c = session.query(Chapter).get(c_id)
        if not c:
            return None
        pages_list = [{"n": p.page_number, "file": p.file_name} for p in c.pages]
        pages_list.sort(key=lambda x: x["n"])

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
            "pages": pages_list,
            "prev_id": prev_id,
            "next_id": next_id,
        }
    finally:
        DBSession.remove()

def db_set_progress(u_id: int, m_id: int, c_id: int, scroll: float):
    session = DBSession()
    try:
        row = session.query(UserProgress).filter_by(user_id=u_id, manhwa_id=m_id).first()
        if row:
            row.last_chapter_id = c_id
            row.scroll_percentage = scroll
        else:
            row = UserProgress(user_id=u_id, manhwa_id=m_id, last_chapter_id=c_id, scroll_percentage=scroll)
            session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        DBSession.remove()

def db_get_progress(u_id: int, m_id: int) -> Optional[dict]:
    session = DBSession()
    try:
        row = session.query(UserProgress).filter_by(user_id=u_id, manhwa_id=m_id).first()
        if row:
            return {"last_chapter_id": row.last_chapter_id, "scroll_percentage": row.scroll_percentage}
        return None
    finally:
        DBSession.remove()

async def execute_db(func_to_run, *args):
    """اجرای امن و ناهمگام توابع بلاک‌کننده دیتابیس در ترد جداگانه"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func_to_run(*args))

# ==============================================================================
# ربات تلگرام (Pyrogram)
# ==============================================================================

bot = Client(
    name="manhwa_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(SESSION_DIR),
)

user_states: dict = {}
user_queues: dict = {}
user_workers: dict = {}

def get_user_state(uid: int) -> dict:
    if uid not in user_states:
        user_states[uid] = {
            "smart_sort": False,
            "pending_title": None,
            "awaiting_title": None,
        }
    return user_states[uid]

def get_farsi_text(key: str, **kwargs) -> str:
    messages = {
        "start": (
            "سلام به ربات مانهواخوان خوش آمدید! 📚👋\n\n"
            "فایل‌های ZIP یا CBZ مانهوای خود را برای من ارسال کنید تا به سرعت استخراج "
            "و در قالب وب‌سایت برای خواندن آماده کنم.\n\n"
            "دستورات ربات:\n"
            "• /library — کتابخانه و لینک‌های مطالعه شما\n"
            "• /settings — تنظیمات مرتب‌سازی تصاویر\n"
        ),
        "queued": "📥 فایل «{name}» در صف پردازش قرار گرفت. جایگاه شما در صف: {pos}",
        "processing": "⚙️ در حال شروع پردازش «{name}»...",
        "extracting": "📦 در حال استخراج تصاویر چپتر...",
        "done": (
            "✅ مانهوای «{title}» فصل {chapter} با {pages} صفحه آماده شد!\n\n"
            "📖 لینک مطالعه مستقیم شما:\n{url}"
        ),
        "corrupt": "❌ متاسفانه فایل آرشیو «{name}» خراب است و پردازش نشد.",
        "no_images": "⚠️ هیچ تصویری در داخل فایل آرشیو «{name}» پیدا نشد.",
        "ask_title": (
            "❓ نتوانستم اسم مانهوا را از روی فایل «{name}» تشخیص دهم.\n"
            "لطفاً نام مانهوا را تایپ و ارسال کنید تا پردازش ادامه یابد:"
        ),
        "title_saved": "📝 نام مانهوا با موفقیت ثبت شد. در حال ادامه فرآیند...",
        "settings": "⚙️ تنظیمات مانهواخوان:\n\nمرتب‌سازی هوشمند تصاویر (Natural Sort): {status}",
        "smart_on": "روشن ✅",
        "smart_off": "خاموش ❌",
        "smart_toggled": "مرتب‌سازی هوشمند تصاویر {status} شد.",
        "empty_lib": "کتابخانه شما در حال حاضر خالی است. فایل مانهوا بفرستید تا ساخته شود!",
        "lib_header": "📚 کتابخانه شخصی شما:\n",
        "url_error": "⚠️ متغیر PUBLIC_URL در تنظیمات Railway شما ثبت نشده است.",
        "error": "❌ مشکلی پیش آمد: {err}"
    }
    return messages.get(key, key).format(**kwargs)

def make_web_url(path: str) -> str:
    base = PUBLIC_URL if PUBLIC_URL else f"http://localhost:{PORT}"
    return f"{base}{path}"

@bot.on_message(filters.command("start") & filters.private)
async def bot_start(client: Client, msg: Message):
    get_user_state(msg.from_user.id)
    await msg.reply_text(get_farsi_text("start"))

@bot.on_message(filters.command("settings") & filters.private)
async def bot_settings(client: Client, msg: Message):
    state = get_user_state(msg.from_user.id)
    status = get_farsi_text("smart_on") if state["smart_sort"] else get_farsi_text("smart_off")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 تغییر وضعیت مرتب‌سازی", callback_data="toggle_sort_mode")
    ]])
    await msg.reply_text(get_farsi_text("settings", status=status), reply_markup=keyboard)

@bot.on_message(filters.command("library") & filters.private)
async def bot_library(client: Client, msg: Message):
    list_m = await execute_db(db_get_all_manhwas)
    if not list_m:
        await msg.reply_text(get_farsi_text("empty_lib"))
        return
    if not PUBLIC_URL:
        await msg.reply_text(get_farsi_text("url_error"))

    buttons = []
    text = get_farsi_text("lib_header")
    for item in list_m:
        text += f"\n• {item['title']} ({item['chapters']} چپتر)"
        buttons.append([InlineKeyboardButton(
            f"📖 {item['title']}",
            url=make_web_url(f"/m/{item['id']}?uid={msg.from_user.id}")
        )])
    await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_callback_query(filters.regex("^toggle_sort_mode$"))
async def cb_toggle_sort(client: Client, cq: CallbackQuery):
    state = get_user_state(cq.from_user.id)
    state["smart_sort"] = not state["smart_sort"]
    status = get_farsi_text("smart_on") if state["smart_sort"] else get_farsi_text("smart_off")
    await cq.answer(get_farsi_text("smart_toggled", status=status))
    try:
        await cq.message.edit_text(
            get_farsi_text("settings", status=status),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 تغییر وضعیت مرتب‌سازی", callback_data="toggle_sort_mode")
            ]])
        )
    except RPCError:
        pass

@bot.on_message(filters.document & filters.private)
async def bot_document_receive(client: Client, msg: Message):
    doc = msg.document
    filename = doc.file_name or "archive.zip"
    ext = Path(filename).suffix.lower()
    if ext not in ARCHIVE_EXTS:
        await msg.reply_text("⚠️ لطفاً فقط فایل با فرمت ZIP یا CBZ بفرستید.")
        return

    uid = msg.from_user.id
    get_user_state(uid)

    if uid not in user_queues:
        user_queues[uid] = asyncio.Queue()
        user_workers[uid] = asyncio.create_task(user_queue_runner(uid))

    q = user_queues[uid]
    position = q.qsize() + 1
    await q.put({"message": msg, "filename": filename, "client": client})
    await msg.reply_text(get_farsi_text("queued", name=filename, pos=position))

@bot.on_message(filters.text & filters.private & ~filters.command(["start", "settings", "library"]))
async def bot_text_receive(client: Client, msg: Message):
    uid = msg.from_user.id
    state = get_user_state(uid)
    if state.get("awaiting_title"):
        title_val = sanitize_folder_name(msg.text.strip())
        state["pending_title"] = title_val
        job_waiting = state["awaiting_title"]
        state["awaiting_title"] = None
        fut = job_waiting.get("future")
        if fut and not fut.done():
            fut.set_result(title_val)
        await msg.reply_text(get_farsi_text("title_saved", title=title_val))
    else:
        await msg.reply_text("برای شروع مطالعه، یک فایل مانهوا (.zip) برای من ارسال کنید.")

async def user_queue_runner(uid: int):
    q = user_queues[uid]
    while True:
        job = await q.get()
        try:
            await process_manhwa_job(uid, job)
        except Exception as e:
            log.exception("خطا در پردازش وظیفه کاربر %s", uid)
            try:
                await job["message"].reply_text(get_farsi_text("error", err=str(e)))
            except Exception:
                pass
        finally:
            q.task_done()

async def process_manhwa_job(uid: int, job: dict):
    message: Message = job["message"]
    filename: str = job["filename"]
    state = get_user_state(uid)

    status_msg = await message.reply_text(get_farsi_text("processing", name=filename))

    temp_dir = DOWNLOADS_DIR / "temp" / f"{uid}_{secrets.token_hex(4)}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_archive_path = temp_dir / sanitize_folder_name(filename)

    try:
        await message.download(file_name=str(local_archive_path))
    except Exception:
        await status_msg.edit_text(get_farsi_text("corrupt", name=filename))
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # تشخیص هوشمند نام و چپتر
    title, chapter = extract_chapter_info(filename)

    if not title:
        if state.get("pending_title"):
            title = state["pending_title"]
        else:
            title = await wait_for_user_title(uid, message, filename)
            if not title:
                await status_msg.edit_text(get_farsi_text("corrupt", name=filename))
                shutil.rmtree(temp_dir, ignore_errors=True)
                return

    state["pending_title"] = title

    await status_msg.edit_text(get_farsi_text("extracting", name=filename))
    extract_output = temp_dir / "extracted"

    try:
        extracted_images = await execute_db(unzip_recursive, local_archive_path, extract_output)
    except Exception:
        await status_msg.edit_text(get_farsi_text("corrupt", name=filename))
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    if not extracted_images:
        await status_msg.edit_text(get_farsi_text("no_images", name=filename))
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # مرتب‌سازی تصاویر
    if state["smart_sort"]:
        extracted_images = sorted(extracted_images, key=lambda p: natural_sort_key(str(p.relative_to(extract_output))))

    manhwa_id = await execute_db(db_get_manhwa_id, title)
    if chapter is None:
        chapter = await execute_db(db_next_chapter_val, manhwa_id)

    # انتقال به پوشه دیسک ماندگار Volume
    dest_chapter_folder = f"chapter_{chapter:g}"
    final_output_dir = DOWNLOADS_DIR / str(manhwa_id) / dest_chapter_folder
    if final_output_dir.exists():
        shutil.rmtree(final_output_dir, ignore_errors=True)
    final_output_dir.mkdir(parents=True, exist_ok=True)

    stored_relative_paths = []
    for idx, img_path in enumerate(extracted_images, start=1):
        suffix = img_path.suffix.lower()
        new_name = f"page_{idx:04d}{suffix}"
        destination = final_output_dir / new_name
        try:
            shutil.move(str(img_path), str(destination))
        except Exception:
            shutil.copy2(str(img_path), str(destination))
        stored_relative_paths.append(str(destination.relative_to(DOWNLOADS_DIR)))

    final_output_dir.touch(exist_ok=True) # به روزرسانی زمان دسترسی برای سیستم پاک‌سازی

    chapter_id = await execute_db(db_save_chapter, manhwa_id, chapter, dest_chapter_folder, stored_relative_paths)
    shutil.rmtree(temp_dir, ignore_errors=True)

    url = make_web_url(f"/read/{chapter_id}?uid={uid}")
    await status_msg.edit_text(get_farsi_text("done", title=title, chapter=f"{chapter:g}", pages=len(stored_relative_paths), url=url))

async def wait_for_user_title(uid: int, message: Message, filename: str) -> Optional[str]:
    state = get_user_state(uid)
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    state["awaiting_title"] = {"filename": filename, "future": future}
    await message.reply_text(get_farsi_text("ask_title", name=filename))
    try:
        title = await asyncio.wait_for(future, timeout=300) # ۵ دقیقه فرصت پاسخ
        return title
    except asyncio.TimeoutError:
        state["awaiting_title"] = None
        await message.reply_text("⏱️ مهلت پاسخگویی شما به اتمام رسید. لطفا فایل را مجددا ارسال کنید.")
        return None

# ==============================================================================
# سرویس خودکار مدیریت حافظه (Cleanup Task)
# ==============================================================================

async def memory_cleanup_agent():
    """سیستم پس‌زمینه برای حذف مانهواهای قدیمی و بلااستفاده جهت جلوگیری از پر شدن دیسک سرور"""
    while True:
        try:
            now = time.time()
            removed_folders = 0
            if DOWNLOADS_DIR.exists():
                for manhwa_folder in DOWNLOADS_DIR.iterdir():
                    if manhwa_folder.name == "temp":
                        for temp_sub in manhwa_folder.glob("*"):
                            try:
                                if now - temp_sub.stat().st_mtime > MAX_IDLE_TIME:
                                    shutil.rmtree(temp_sub, ignore_errors=True)
                            except Exception:
                                pass
                        continue

                    if not manhwa_folder.is_dir():
                        continue

                    for chapter_folder in manhwa_folder.iterdir():
                        if not chapter_folder.is_dir():
                            continue
                        try:
                            last_access = chapter_folder.stat().st_atime
                            for item in chapter_folder.rglob("*"):
                                try:
                                    last_access = max(last_access, item.stat().st_atime)
                                except Exception:
                                    pass
                            if now - last_access > MAX_IDLE_TIME:
                                shutil.rmtree(chapter_folder, ignore_errors=True)
                                removed_folders += 1
                                log.info("حذف خودکار فایل بلااستفاده جهت بهینه‌سازی دیسک: %s", chapter_folder)
                        except Exception:
                            pass
            if removed_folders > 0:
                log.info("عملیات پاک‌سازی دیسک پایان یافت. %d پوشه آزاد شد.", removed_folders)
        except Exception as e:
            log.error("خطا در سیستم هماهنگ‌سازی حافظه: %s", e)
        await asyncio.sleep(CLEANUP_INTERVAL)

# ==============================================================================
# سرور نمایش وب (FastAPI)
# ==============================================================================

app = FastAPI(title="مانهواخوان اختصاصی")

# نمایش مستقیم تصاویر استخراج شده
app.mount("/img", StaticFiles(directory=str(DOWNLOADS_DIR)), name="img")

HTML_HEAD_TEMPLATE = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes"/>
<title>{title}</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  html, body {{ margin:0; padding:0; background:#080808; color:#f3f4f6; font-family: Tahoma, sans-serif; }}
  ::-webkit-scrollbar {{ width: 5px; }}
  ::-webkit-scrollbar-thumb {{ background:#222; border-radius:3px; }}
  .manhwa-page {{ width:100%; height:auto; display:block; margin: 0 auto; }}
</style>
</head>
"""

def generate_library_view(manhwas: List[dict], uid: str) -> str:
    cards = ""
    if not manhwas:
        cards = '<p class="text-center text-gray-500 mt-20">کتابخانه شما هنوز خالی است.</p>'
    for m in manhwas:
        cards += f"""
        <a href="/m/{m['id']}?uid={uid}"
           class="block bg-neutral-900 hover:bg-neutral-800 transition rounded-2xl p-4 mb-3 border border-neutral-800">
          <div class="flex items-center justify-between">
            <span class="text-lg font-bold text-gray-100">{m['title']}</span>
            <span class="text-sm text-emerald-400 font-semibold">{m['chapters']} فصل</span>
          </div>
        </a>"""
    head = HTML_HEAD_TEMPLATE.format(title="کتابخانه شخصی مانهوا")
    return f"""{head}
<body class="p-4">
  <div class="max-w-xl mx-auto">
    <h1 class="text-2xl font-black mb-8 text-center text-emerald-500">📚 کتابخانه مانهوا</h1>
    {cards}
  </div>
</body></html>"""

def generate_manhwa_view(manhwa: dict, uid: str, resume_ch_id: Optional[int]) -> str:
    list_items = ""
    for c in manhwa["chapters"]:
        badge = ""
        if resume_ch_id and c["id"] == resume_ch_id:
            badge = '<span class="text-xs bg-emerald-600 px-2.5 py-1 rounded-full text-white font-medium">ادامه مطالعه</span>'
        list_items += f"""
        <a href="/read/{c['id']}?uid={uid}"
           class="flex items-center justify-between bg-neutral-900 hover:bg-neutral-800 transition rounded-xl p-4 mb-2.5 border border-neutral-800/60">
          <span class="text-gray-200">فصل {c['number']:g}</span>
          {badge}
        </a>"""
    
    resume_button = ""
    if resume_ch_id:
        resume_button = f"""
        <a href="/read/{resume_ch_id}?uid={uid}"
           class="block text-center bg-emerald-600 hover:bg-emerald-500 text-white font-bold rounded-xl p-4 mb-6 transition shadow-lg shadow-emerald-950/40">
           ▶️ ادامه فصل قبلی
        </a>"""

    head = HTML_HEAD_TEMPLATE.format(title=manhwa["title"])
    return f"""{head}
<body class="p-4">
  <div class="max-w-xl mx-auto">
    <div class="flex items-center justify-between mb-6">
      <h1 class="text-xl font-bold text-gray-100">{manhwa['title']}</h1>
      <a href="/?uid={uid}" class="text-sm text-gray-400 hover:text-emerald-400">← بازگشت به کتابخانه</a>
    </div>
    {resume_button}
    <div class="space-y-1">{list_items}</div>
  </div>
</body></html>"""

def generate_reader_view(chapter: dict, uid: str, resume_percent: float) -> str:
    images_html = ""
    for p in chapter["pages"]:
        images_html += f'<img class="manhwa-page" loading="lazy" src="/img/{p["file"]}" alt="صفحه {p["n"]}"/>'

    prev_action = f'<a href="/read/{chapter["prev_id"]}?uid={uid}" class="flex-1 text-center bg-neutral-800 hover:bg-neutral-700 rounded-xl py-3 text-sm font-semibold transition">فصل قبلی</a>' if chapter["prev_id"] else '<span class="flex-1 text-center bg-neutral-950 text-gray-700 rounded-xl py-3 text-sm">فصل قبلی وجود ندارد</span>'
    next_action = f'<a href="/read/{chapter["next_id"]}?uid={uid}" class="flex-1 text-center bg-emerald-600 hover:bg-emerald-500 rounded-xl py-3 text-sm font-semibold transition">فصل بعدی</a>' if chapter["next_id"] else '<span class="flex-1 text-center bg-neutral-950 text-gray-700 rounded-xl py-3 text-sm">پایان این مانهوا</span>'

    head = HTML_HEAD_TEMPLATE.format(title=f"{chapter['manhwa_title']} - فصل {chapter['number']:g}")

    # جاوااسکریپت برای زوم و ذخیره هوشمند اسکرول
    js_code = f"""
<script>
const CHAPTER_ID = {chapter['id']};
const MANHWA_ID = {chapter['manhwa_id']};
const USER_ID = "{uid}";
const RESUME_SCROLL = {resume_percent};

window.addEventListener('load', () => {{
  setTimeout(() => {{
    const totalHeight = document.documentElement.scrollHeight - window.innerHeight;
    if (totalHeight > 0 && RESUME_SCROLL > 0) {{
      window.scrollTo(0, totalHeight * (RESUME_SCROLL / 100));
    }}
  }}, 500);
}});

let lastReportTime = 0;
function sendProgress() {{
  const now = Date.now();
  if (now - lastReportTime < 2500) return; // هر ۲.۵ ثانیه حداکثر یکبار ارسال کن
  lastReportTime = now;

  const totalHeight = document.documentElement.scrollHeight - window.innerHeight;
  const currentPercent = totalHeight > 0 ? Math.min(100, (window.scrollY / totalHeight) * 100) : 0;

  fetch('/api/progress', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      user_id: USER_ID, manhwa_id: MANHWA_ID,
      chapter_id: CHAPTER_ID, scroll: currentPercent
    }}),
    keepalive: true
  }}).catch(() => {{}});
}}

window.addEventListener('scroll', sendProgress, {{passive: true}});
window.addEventListener('beforeunload', sendProgress);

// پیاده‌سازی لمس و قابلیت زوم هوشمند روان
(function() {{
  const area = document.getElementById('manhwa-container');
  let currentScale = 1, currentX = 0, currentY = 0;
  let startDistance = 0, initialScale = 1;
  let sTouchX = 0, sTouchY = 0, pX = 0, pY = 0;
  let panning = false;

  function updateTransform() {{
    area.style.transform = `translate(${{currentX}}px, ${{currentY}}px) scale(${{currentScale}})`;
    area.style.transformOrigin = 'center top';
  }}

  area.addEventListener('touchstart', (e) => {{
    if (e.touches.length === 2) {{
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      startDistance = Math.hypot(dx, dy);
      initialScale = currentScale;
    }} else if (e.touches.length === 1 && currentScale > 1) {{
      panning = true;
      sTouchX = e.touches[0].clientX;
      sTouchY = e.touches[0].clientY;
      pX = currentX;
      pY = currentY;
    }}
  }}, {{passive: true}});

  area.addEventListener('touchmove', (e) => {{
    if (e.touches.length === 2) {{
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      const d = Math.hypot(dx, dy);
      currentScale = Math.min(4.5, Math.max(1, initialScale * (d / startDistance)));
      if (currentScale === 1) {{ currentX = 0; currentY = 0; }}
      updateTransform();
      e.preventDefault();
    }} else if (panning && e.touches.length === 1 && currentScale > 1) {{
      currentX = pX + (e.touches[0].clientX - sTouchX);
      currentY = pY + (e.touches[0].clientY - sTouchY);
      updateTransform();
      e.preventDefault();
    }}
  }}, {{passive: false}});

  area.addEventListener('touchend', (e) => {{
    if (e.touches.length === 0) {{
      panning = false;
      if (currentScale <= 1.05) {{ currentScale = 1; currentX = 0; currentY = 0; updateTransform(); }}
    }}
  }});

  let clickTime = 0;
  area.addEventListener('touchend', () => {{
    const t = Date.now();
    if (t - clickTime < 300) {{
      currentScale = 1; currentX = 0; currentY = 0; updateTransform();
    }}
    clickTime = t;
  }});
}})();
</script>
"""

    return f"""{head}
<body class="bg-black select-none">
  <div class="sticky top-0 z-20 bg-neutral-950/90 backdrop-blur border-b border-neutral-800 px-4 py-3 flex items-center justify-between">
    <a href="/m/{chapter['manhwa_id']}?uid={uid}" class="text-sm text-gray-300 hover:text-white">← لیست فصل‌ها</a>
    <span class="text-sm font-bold text-gray-200">{chapter['manhwa_title']} — فصل {chapter['number']:g}</span>
  </div>

  <div id="manhwa-container" class="mx-auto max-w-2xl">
    {images_html}
  </div>

  <div class="sticky bottom-0 z-20 bg-neutral-950/90 backdrop-blur border-t border-neutral-800 p-3 flex gap-2">
    {prev_action}
    {next_action}
  </div>
  {js_code}
</body></html>"""

# ---------- آدرس‌های وب (Routes) ----------

@app.get("/", response_class=HTMLResponse)
async def route_index(uid: str = "0"):
    list_m = await execute_db(db_get_all_manhwas)
    return HTMLResponse(generate_library_view(list_m, uid))

@app.get("/m/{manhwa_id}", response_class=HTMLResponse)
async def route_manhwa(manhwa_id: int, uid: str = "0"):
    m = await execute_db(db_get_manhwa_details, manhwa_id)
    if not m:
        raise HTTPException(status_code=404, detail="مانهوا پیدا نشد")
    resume_chapter = None
    try:
        progress = await execute_db(db_get_progress, int(uid), manhwa_id)
        if progress:
            resume_chapter = progress.get("last_chapter_id")
    except Exception:
        pass
    return HTMLResponse(generate_manhwa_view(m, uid, resume_chapter))

@app.get("/read/{chapter_id}", response_class=HTMLResponse)
async def route_read(chapter_id: int, uid: str = "0"):
    chapter = await execute_db(db_get_chapter_details, chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="فصل پیدا نشد")

    try:
        folder = DOWNLOADS_DIR / str(chapter["manhwa_id"]) / chapter["folder"]
        if folder.exists():
            os.utime(folder, None)  # بروزرسانی آمار برای پاک نشدن مانهوای در حال خواندن
    except Exception:
        pass

    scroll_val = 0.0
    try:
        progress = await execute_db(db_get_progress, int(uid), chapter["manhwa_id"])
        if progress and progress.get("last_chapter_id") == chapter_id:
            scroll_val = progress.get("scroll_percentage", 0.0)
    except Exception:
        pass

    return HTMLResponse(generate_reader_view(chapter, uid, scroll_val))

@app.post("/api/progress")
async def route_api_progress(request: Request):
    try:
        payload = await request.json()
        u_id = int(payload.get("user_id", 0))
        m_id = int(payload["manhwa_id"])
        c_id = int(payload["chapter_id"])
        scroll = float(payload.get("scroll", 0.0))
    except Exception:
        return JSONResponse({"ok": False, "error": "payload_error"}, status_code=400)

    if u_id <= 0:
        return JSONResponse({"ok": True, "saved": False})

    try:
        await execute_db(db_set_progress, u_id, m_id, c_id, scroll)
        return JSONResponse({"ok": True, "saved": True})
    except Exception:
        return JSONResponse({"ok": False}, status_code=500)

@app.get("/health")
async def route_health():
    return {"status": "active", "timestamp": datetime.utcnow().isoformat()}

# ==============================================================================
# ردیاب سیگنال پیام‌ها (جهت تست و عیب‌یابی)
# ==============================================================================

@bot.on_message(group=-1)
async def diagnostic_logger(client: Client, msg: Message):
    """به محض ارسال هر پیامی به ربات، این بخش در لاگ‌های ریلوِی پیام چاپ می‌کند"""
    sender = msg.from_user.id if msg.from_user else "ناشناس"
    text_preview = msg.text or "[فایل یا مدیا]"
    log.info("📩 سیگنال پیام جدید دریافت شد! فرستنده: %s | متن: %s", sender, text_preview)

# ==============================================================================
# راه‌اندازی همزمان سرور و ربات تلگرام در یک چرخه ناهمگام
# ==============================================================================

async def main_runner():
    missing_vars = [name for name, val in {
        "API_ID": API_ID, "API_HASH": API_HASH, "BOT_TOKEN": BOT_TOKEN
    }.items() if not val]

    if missing_vars:
        log.error("تنظیمات کلیدی ریلوِی ناقص است: %s", ", ".join(missing_vars))
        log.error("لطفاً مقادیر بالا را در تب Variables سرور ابری خود تکمیل کنید.")
        return

    init_db()

    # ابتدا ربات را استارت می‌زنیم تا کانکشن تلگرام کاملاً برقرار شود
    await bot.start()
    bot_info = await bot.get_me()
    log.info("ربات تلگرام با موفقیت با یوزرنیم @%s فعال شد.", bot_info.username)

    # سپس وب‌سرور و پاک‌کننده حافظه را بالا می‌آوریم
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    app_tasks = [
        asyncio.create_task(server.serve()),
        asyncio.create_task(memory_cleanup_agent())
    ]

    log.info("وب‌سرور مانهواخوان روی پورت %s فعال شد.", PORT)

    try:
        await asyncio.gather(*app_tasks)
    finally:
        try:
            await bot.stop()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main_runner())
    except (KeyboardInterrupt, SystemExit):
        log.info("سیستم در حال خاموش شدن است...")
