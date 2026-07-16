# ==============================================================================
#  🖤 MANHWA-INK — A "God-Tier" Manhwa Reader & Telegram Bot (FIXED & OPTIMIZED)
# ==============================================================================

from __future__ import annotations

import asyncio
import io
import os
import time
import json
import random
import zipfile
import secrets
import logging
import datetime
import html
import hashlib
import urllib.parse
import mimetypes
from contextlib import asynccontextmanager, closing
from typing import Optional, List, Dict, Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, StreamingResponse, FileResponse
)
from starlette.middleware.base import BaseHTTPMiddleware

# --- Pyrofork / Pyrogram ------------------------------------------------------
try:
    from pyrogram import Client, filters
    from pyrogram.types import (
        InlineKeyboardMarkup, InlineKeyboardButton,
        Message, CallbackQuery,
    )
    from pyrogram.errors import RPCError
    PYRO_AVAILABLE = True
except Exception:  # pragma: no cover
    PYRO_AVAILABLE = False
    Client = None  # type: ignore

# ==============================================================================
#  CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("manhwa-ink")

API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
PORT = int(os.getenv("PORT", "8080"))

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()
}

# Storage paths
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
if not os.path.isdir("/app"):  # local dev fallback
    DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "manhwa.db")
CACHE_DIR = os.path.join(DATA_DIR, "imgcache")
os.makedirs(CACHE_DIR, exist_ok=True)

BOT_ENABLED = PYRO_AVAILABLE and bool(API_ID and API_HASH and BOT_TOKEN)

# ==============================================================================
#  HELPERS & SECURITY
# ==============================================================================
def esc(text: Any) -> str:
    """Escapes HTML to prevent layout breaks and XSS injections."""
    if text is None:
        return ""
    return html.escape(str(text))

def get_channel_id() -> Optional[Any]:
    """Smarter channel ID parser (converts string numbers to int)."""
    if not CHANNEL_ID:
        return None
    val = CHANNEL_ID.replace(" ", "")
    if val.startswith("-") and val[1:].isdigit():
        return int(val)
    if val.isdigit():
        return int(val)
    return val

# ==============================================================================
#  DATABASE LAYER
# ==============================================================================
def db() -> sqlite3.Connection:
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    coins INTEGER DEFAULT 100,
    last_wheel TEXT,
    web_token TEXT,
    is_admin INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS manhwas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE,
    title TEXT NOT NULL,
    description TEXT,
    cover TEXT,
    genres TEXT,
    status TEXT DEFAULT 'Ongoing',
    views INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manhwa_id INTEGER NOT NULL,
    number REAL NOT NULL,
    title TEXT,
    images TEXT,
    is_premium INTEGER DEFAULT 0,
    cost INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (manhwa_id) REFERENCES manhwas(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bookmarks (
    user_id INTEGER NOT NULL,
    manhwa_id INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, manhwa_id)
);

CREATE TABLE IF NOT EXISTS history (
    user_id INTEGER NOT NULL,
    manhwa_id INTEGER NOT NULL,
    chapter_id INTEGER,
    scroll_y INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, manhwa_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL,
    user_id INTEGER,
    name TEXT,
    body TEXT,
    reaction TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ratings (
    user_id INTEGER NOT NULL,
    manhwa_id INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    PRIMARY KEY (user_id, manhwa_id)
);

CREATE TABLE IF NOT EXISTS coins_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS unlocks (
    user_id INTEGER NOT NULL,
    chapter_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, chapter_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

SEED = [
    {
        "slug": "shadow-monarch",
        "title": "Shadow Monarch Ascension",
        "description": "A weak hunter awakens the power to command an army of shadows and rises to become the strongest of all.",
        "cover": "https://images.unsplash.com/photo-1534447677768-be436bb09401?w=600&q=80",
        "genres": "Action,Fantasy,Adventure",
        "status": "Ongoing",
    },
    {
        "slug": "crimson-blade",
        "title": "Crimson Blade Chronicles",
        "description": "A wandering swordsman seeks revenge across a war-torn empire, blade painted red with fate.",
        "cover": "https://images.unsplash.com/photo-1531259683007-016a7b628fc3?w=600&q=80",
        "genres": "Action,Drama,Historical",
        "status": "Ongoing",
    },
]

SAMPLE_IMAGES = [
    "https://images.unsplash.com/photo-1509023464722-18d996393ca8?w=900&q=80",
    "https://images.unsplash.com/photo-1517842645767-c639042777db?w=900&q=80",
]


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript(DDL)
        conn.commit()
        count = conn.execute("SELECT COUNT(*) c FROM manhwas").fetchone()["c"]
        if count == 0:
            log.info("Seeding default manhwa data ...")
            for m in SEED:
                cur = conn.execute(
                    "INSERT INTO manhwas (slug,title,description,cover,genres,status,views) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (m["slug"], m["title"], m["description"], m["cover"],
                     m["genres"], m["status"], random.randint(500, 9000)),
                )
                mid = cur.lastrowid
                for ch in range(1, 4):
                    imgs = json.dumps(SAMPLE_IMAGES)
                    premium = 1 if ch == 3 else 0
                    conn.execute(
                        "INSERT INTO chapters (manhwa_id,number,title,images,is_premium,cost) "
                        "VALUES (?,?,?,?,?,?)",
                        (mid, ch, f"Chapter {ch}", imgs, premium, 20 if premium else 0),
                    )
            conn.commit()
        log.info("Database ready at %s", DB_PATH)


def q_all(sql: str, args: tuple = ()) -> List[Any]:
    with closing(db()) as conn:
        return conn.execute(sql, args).fetchall()


def q_one(sql: str, args: tuple = ()) -> Optional[Any]:
    with closing(db()) as conn:
        return conn.execute(sql, args).fetchone()


def q_exec(sql: str, args: tuple = ()) -> int:
    with closing(db()) as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def ensure_user(uid: int, username: str = "", first_name: str = "") -> Any:
    row = q_one("SELECT * FROM users WHERE id=?", (uid,))
    if not row:
        q_exec(
            "INSERT INTO users (id,username,first_name,is_admin) VALUES (?,?,?,?)",
            (uid, username, first_name, 1 if uid in ADMIN_IDS else 0),
        )
        row = q_one("SELECT * FROM users WHERE id=?", (uid,))
    return row


def add_coins(uid: int, amount: int, reason: str) -> None:
    q_exec("UPDATE users SET coins = coins + ? WHERE id=?", (amount, uid))
    q_exec("INSERT INTO coins_log (user_id,amount,reason) VALUES (?,?,?)", (uid, amount, reason))


# ==============================================================================
#  WEB SESSION HELPERS
# ==============================================================================
def get_web_user(request: Request) -> Optional[Any]:
    token = request.cookies.get("sid")
    if not token:
        return None
    s = q_one("SELECT * FROM sessions WHERE token=?", (token,))
    if not s:
        return None
    return q_one("SELECT * FROM users WHERE id=?", (s["user_id"],))


# ==============================================================================
#  FASTAPI APP + LIFESPAN
# ==============================================================================
bot: Optional["Client"] = None
STATE: Dict[str, Any] = {"started_at": time.time(), "requests": 0}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    global bot
    if BOT_ENABLED:
        try:
            bot = Client(
                "manhwa_bot",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=BOT_TOKEN,
                workdir=DATA_DIR,
                in_memory=True,
            )
            register_bot_handlers(bot)
            await bot.start()
            me = await bot.get_me()
            log.info("🤖 Bot online as @%s", me.username)
            if PUBLIC_URL:
                log.info("🌐 PUBLIC_URL configured: %s", PUBLIC_URL)
            asyncio.create_task(auto_backup_scheduler())
        except Exception as e:
            log.error("Bot failed to start: %s", e)
            bot = None
    else:
        log.warning("Bot disabled (missing API_ID/API_HASH/BOT_TOKEN or pyrofork).")

    yield

    if bot:
        try:
            await bot.stop()
            log.info("Bot stopped cleanly.")
        except Exception:
            pass


app = FastAPI(title="Manhwa-Ink", lifespan=lifespan, docs_url=None, redoc_url=None)


class RateLimiter(BaseHTTPMiddleware):
    WINDOW = 10
    MAX_REQ = 120
    _hits: Dict[str, List[float]] = {}

    async def dispatch(self, request: Request, call_next):
        STATE["requests"] += 1
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        bucket = self._hits.setdefault(ip, [])
        cutoff = now - self.WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        limit = self.MAX_REQ * 2 if request.url.path.startswith("/proxy") else self.MAX_REQ
        if len(bucket) >= limit:
            return JSONResponse(
                {"error": "Rate limit exceeded. Slow down, speedreader!"},
                status_code=429,
            )
        bucket.append(now)
        if len(self._hits) > 5000:
            self._hits.clear()
        return await call_next(request)


app.add_middleware(RateLimiter)


# ==============================================================================
#  FRONTEND — CSS
# ==============================================================================
CSS = """
:root{
  --ink:#0b0b0f; --panel:#14141c; --accent:#fe0055; --border:#242432;
  --text:#ececf2; --muted:#8a8aa0; --gold:#ffca3a;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  background:var(--ink);color:var(--text);
  font-family:'Trebuchet MS',system-ui,-apple-system,sans-serif;
  line-height:1.5;min-height:100vh;
  background-image:radial-gradient(rgba(255,255,255,.035) 1px,transparent 1.4px);
  background-size:9px 9px;
}
a{color:inherit;text-decoration:none}
img{display:block;max-width:100%}
.wrap{max-width:1200px;margin:0 auto;padding:0 16px}

/* ---------- HEADER ---------- */
header{
  position:sticky;top:0;z-index:50;background:var(--panel);
  border-bottom:2px solid var(--border);
  box-shadow:0 4px 0 rgba(0,0,0,.4);
}
.nav{display:flex;align-items:center;gap:14px;height:64px}
.logo{
  font-family:'Impact','Arial Black',sans-serif;font-size:26px;letter-spacing:1px;
  color:#fff;text-transform:uppercase;transform:skew(-6deg);
  padding:2px 10px;border:2px solid var(--accent);background:var(--ink);
  box-shadow:4px 4px 0 var(--accent);
}
.logo span{color:var(--accent)}
.nav .grow{flex:1}
.searchbox{
  display:flex;align-items:center;background:var(--ink);border:2px solid var(--border);
  border-radius:0;padding:6px 10px;min-width:180px;
}
.searchbox input{background:none;border:none;color:var(--text);outline:none;width:100%}
.btn{
  display:inline-block;background:var(--accent);color:#fff;font-weight:bold;
  padding:8px 16px;border:2px solid #ff4b86;cursor:pointer;
  text-transform:uppercase;letter-spacing:.5px;font-size:13px;
  box-shadow:3px 3px 0 rgba(0,0,0,.5);transition:transform .08s;
}
.btn:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 rgba(0,0,0,.6)}
.btn:active{transform:translate(2px,2px);box-shadow:1px 1px 0 rgba(0,0,0,.5)}
.btn.ghost{background:transparent;color:var(--text);border-color:var(--border)}
.btn.gold{background:var(--gold);color:#1a1a1a;border-color:#ffe08a}

/* ---------- HERO / SECTION TITLES ---------- */
.sec-title{
  font-family:'Impact','Arial Black',sans-serif;font-size:24px;text-transform:uppercase;
  letter-spacing:1px;margin:28px 0 16px;color:#fff;display:flex;align-items:center;gap:10px;
}
.sec-title::before{content:"";width:8px;height:26px;background:var(--accent);display:inline-block;transform:skew(-12deg)}

/* ---------- CARD GRID ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:16px}
.card{
  background:var(--panel);border:2px solid var(--border);overflow:hidden;
  position:relative;transition:transform .1s,border-color .1s;
}
.card:hover{transform:translateY(-4px);border-color:var(--accent)}
.card .cov{aspect-ratio:2/3;width:100%;object-fit:cover;background:#1c1c26}
.card .meta{padding:8px 10px}
.card .t{font-weight:bold;font-size:14px;line-height:1.2;height:34px;overflow:hidden}
.badge{
  position:absolute;top:8px;left:8px;background:var(--accent);color:#fff;
  font-size:10px;font-weight:bold;padding:3px 7px;text-transform:uppercase;
  transform:skew(-8deg);box-shadow:2px 2px 0 rgba(0,0,0,.5);
}
.badge.done{background:#2ec4b6;color:#08201d}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.chip{font-size:10px;color:var(--muted);border:1px solid var(--border);padding:2px 6px}

/* ---------- FILTER TAGS ---------- */
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.ftag{
  font-size:12px;padding:6px 12px;border:2px solid var(--border);cursor:pointer;
  background:var(--panel);text-transform:uppercase;font-weight:bold;color:var(--muted);
}
.ftag.active{background:var(--accent);color:#fff;border-color:#ff4b86}

/* ---------- SPEECH BUBBLE ALERT ---------- */
.bubble{
  position:relative;background:#fff;color:#111;padding:14px 18px;border:3px solid #111;
  border-radius:16px;margin:16px 0;font-weight:bold;max-width:520px;
}
.bubble::after{
  content:"";position:absolute;bottom:-18px;left:36px;border:9px solid transparent;
  border-top-color:#111;
}
.bubble.pink{background:var(--accent);color:#fff;border-color:#7a0027}
.bubble.pink::after{border-top-color:#7a0027}

/* ---------- DETAIL PAGE ---------- */
.detail{display:flex;gap:24px;flex-wrap:wrap;margin-top:24px}
.detail .cover{width:240px;border:2px solid var(--border);box-shadow:8px 8px 0 var(--accent)}
.detail .info{flex:1;min-width:260px}
.detail h1{font-family:'Impact',sans-serif;font-size:36px;text-transform:uppercase;line-height:1}
.stars{font-size:24px;color:var(--gold);cursor:pointer;user-select:none}
.stars span{transition:transform .1s}
.stars span:hover{transform:scale(1.25)}
.chapter-list{display:flex;flex-direction:column;gap:8px;margin-top:16px}
.chapter-row{
  display:flex;justify-content:space-between;align-items:center;background:var(--panel);
  border:2px solid var(--border);padding:12px 14px;
}
.chapter-row:hover{border-color:var(--accent)}
.lock{color:var(--gold);font-size:12px;font-weight:bold}

/* ---------- READER ---------- */
.reader{max-width:800px;margin:0 auto;background:#000}
.reader img{width:100%;min-height:200px;background:#111}
.reader-bar{
  position:sticky;top:64px;z-index:40;background:var(--panel);border-bottom:2px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;padding:10px 14px;
}
.react-bar{display:flex;gap:10px;justify-content:center;padding:18px;background:var(--panel);border-top:2px solid var(--border)}
.react{font-size:26px;cursor:pointer;background:var(--ink);border:2px solid var(--border);padding:6px 12px}
.react:hover{border-color:var(--accent);transform:scale(1.1)}

/* ---------- COMMENTS ---------- */
.comment{background:var(--panel);border:2px solid var(--border);padding:12px;margin-bottom:10px}
.comment .who{color:var(--accent);font-weight:bold;font-size:13px}
.comment .when{color:var(--muted);font-size:11px}
textarea,input.txt{
  width:100%;background:var(--ink);border:2px solid var(--border);color:var(--text);
  padding:10px;font-family:inherit;outline:none;
}
textarea:focus,input.txt:focus{border-color:var(--accent)}

/* ---------- FOOTER ---------- */
footer{border-top:2px solid var(--border);margin-top:48px;padding:24px 0;color:var(--muted);text-align:center;font-size:13px}

/* ---------- DASHBOARD ---------- */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:20px 0}
.stat{background:var(--panel);border:2px solid var(--border);padding:18px;border-left:6px solid var(--accent)}
.stat .n{font-family:'Impact',sans-serif;font-size:34px;color:#fff}
.stat .l{color:var(--muted);text-transform:uppercase;font-size:12px;letter-spacing:1px}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{border:2px solid var(--border);padding:8px 10px;text-align:left;font-size:13px}
th{background:var(--panel);text-transform:uppercase;color:var(--accent)}

@media(max-width:640px){
  .logo{font-size:20px}
  .searchbox{min-width:120px}
  .detail h1{font-size:26px}
  header .nav{gap:8px}
}
"""

MANIFEST = {
    "name": "Manhwa-Ink Reader",
    "short_name": "Manhwa-Ink",
    "description": "God-Tier Manhwa Reader",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0b0b0f",
    "theme_color": "#fe0055",
    "icons": [
        {"src": "/icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
    ],
}


# ==============================================================================
#  FRONTEND — HTML BUILDERS
# ==============================================================================
def page_shell(title: str, body: str, og: Dict[str, str] = None, user: Any = None) -> str:
    og = og or {}
    og_title = og.get("title", title)
    og_desc = og.get("description", "Read the hottest manhwa in a comic-book dark theme.")
    og_img = og.get("image", (PUBLIC_URL + "/icon.png") if PUBLIC_URL else "")
    og_url = og.get("url", PUBLIC_URL or "/")
    coins = f'💰 {user["coins"]}' if user else ""
    account = (
        f'<a class="btn ghost" href="/bookmarks">★ Library</a>'
        f'<span class="btn gold">{coins}</span>'
        if user else
        '<a class="btn" href="/login">Sign in with Telegram</a>'
    )
    return f"""<!doctype html><html lang="fa"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Manhwa-Ink</title>
<meta name="theme-color" content="#fe0055">
<link rel="manifest" href="/manifest.json">
<meta name="description" content="{esc(og_desc)}">
<!-- OpenGraph -->
<meta property="og:type" content="website">
<meta property="og:title" content="{esc(og_title)}">
<meta property="og:description" content="{esc(og_desc)}">
<meta property="og:image" content="{esc(og_img)}">
<meta property="og:url" content="{esc(og_url)}">
<meta property="og:site_name" content="Manhwa-Ink">
<!-- Twitter -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(og_title)}">
<meta name="twitter:description" content="{esc(og_desc)}">
<meta name="twitter:image" content="{esc(og_img)}">
<style>{CSS}</style>
</head><body>
<header><div class="wrap nav">
  <a class="logo" href="/">MANHWA<span>·</span>INK</a>
  <div class="grow"></div>
  <form class="searchbox" action="/search" method="get">
    <input name="q" placeholder="Search..." autocomplete="off" dir="auto">
  </form>
  {account}
</div></header>
<main class="wrap">{body}</main>
<footer>
  <div class="wrap">🖤 Manhwa-Ink — built with FastAPI + Pyrofork · Reader for the ink-stained soul.</div>
</footer>
<script>
if('serviceWorker' in navigator){{navigator.serviceWorker.register('/sw.js').catch(()=>{{}});}}
</script>
</body></html>"""


def card_html(m: Any) -> str:
    badge = ('<span class="badge done">Completed</span>' if m["status"] == "Completed"
             else '<span class="badge">Ongoing</span>')
    genres = "".join(f'<span class="chip">{esc(g)}</span>' for g in (m["genres"] or "").split(",")[:3] if g)
    cover_url = m["cover"] or ""
    cover = f'/proxy?url={urllib.parse.quote_plus(cover_url)}' if cover_url else ""
    return f"""<a class="card" href="/manhwa/{m['slug']}">
      {badge}
      <img class="cov" loading="lazy" src="{cover}" alt="{esc(m['title'])}">
      <div class="meta"><div class="t" dir="auto">{esc(m['title'])}</div>
        <div class="chips">{genres}</div></div>
    </a>"""


# ==============================================================================
#  WEB ROUTES — PUBLIC READER
# ==============================================================================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = get_web_user(request)
    latest = q_all("SELECT * FROM manhwas ORDER BY created_at DESC LIMIT 12")
    popular = q_all("SELECT * FROM manhwas ORDER BY views DESC LIMIT 12")

    continue_html = ""
    if user:
        h = q_one(
            "SELECT h.*, m.title, m.slug, m.cover, c.number FROM history h "
            "JOIN manhwas m ON m.id=h.manhwa_id LEFT JOIN chapters c ON c.id=h.chapter_id "
            "WHERE h.user_id=? ORDER BY h.updated_at DESC LIMIT 1", (user["id"],))
        if h:
            continue_html = f"""
            <div class="bubble pink" dir="auto">📖 Continue reading <b>{esc(h['title'])}</b> — Chapter {h['number'] or '?'}!
              <a class="btn ghost" style="margin-left:8px" href="/manhwa/{h['slug']}">Resume</a></div>"""

    history_html = ""
    if user:
        rows = q_all(
            "SELECT m.* FROM history h JOIN manhwas m ON m.id=h.manhwa_id "
            "WHERE h.user_id=? ORDER BY h.updated_at DESC LIMIT 5", (user["id"],))
        if rows:
            history_html = (
                '<h2 class="sec-title" dir="auto">Recently Read</h2><div class="grid">'
                + "".join(card_html(m) for m in rows) + "</div>")

    genres_set = set()
    for m in latest:
        for g in (m["genres"] or "").split(","):
            if g.strip():
                genres_set.add(g.strip())
    filter_tags = '<span class="ftag active" data-g="all">All</span>' + "".join(
        f'<span class="ftag" data-g="{esc(g)}">{esc(g)}</span>' for g in sorted(genres_set))

    body = f"""
    {continue_html}
    <div class="bubble" dir="auto">Welcome to <b>Manhwa-Ink</b> — {q_one("SELECT COUNT(*) c FROM manhwas")['c']} titles inked & ready. Pick your poison! 🖤</div>
    <h2 class="sec-title" dir="auto">Browse</h2>
    <div class="filters" id="filters">{filter_tags}</div>
    <div class="grid" id="grid">{''.join(card_html(m) for m in latest)}</div>
    {history_html}
    <h2 class="sec-title" dir="auto">🔥 Most Read</h2>
    <div class="grid">{''.join(card_html(m) for m in popular)}</div>
    <script>
      document.querySelectorAll('.ftag').forEach(t=>t.onclick=()=>{{
        document.querySelectorAll('.ftag').forEach(x=>x.classList.remove('active'));
        t.classList.add('active');
        const g=t.dataset.g;
        document.querySelectorAll('#grid .card').forEach(c=>{{
          const chips=[...c.querySelectorAll('.chip')].map(x=>x.textContent);
          c.style.display=(g==='all'||chips.includes(g))?'':'none';
        }});
      }});
    </script>
    """
    return HTMLResponse(page_shell("Home", body, user=user))


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    user = get_web_user(request)
    like = f"%{q.strip()}%"
    rows = q_all(
        "SELECT * FROM manhwas WHERE title LIKE ? OR genres LIKE ? OR description LIKE ? "
        "ORDER BY views DESC LIMIT 60", (like, like, like)) if q.strip() else []
    grid = "".join(card_html(m) for m in rows) or '<div class="bubble" dir="auto">No results found. Try another spell. 🔮</div>'
    body = f'<h2 class="sec-title" dir="auto">Search: "{esc(q)}"</h2><div class="grid">{grid}</div>'
    return HTMLResponse(page_shell(f"Search {q}", body, user=user))


@app.get("/manhwa/{slug}", response_class=HTMLResponse)
async def manhwa_detail(request: Request, slug: str):
    user = get_web_user(request)
    m = q_one("SELECT * FROM manhwas WHERE slug=?", (slug,))
    if not m:
        raise HTTPException(404, "Manhwa not found")
    q_exec("UPDATE manhwas SET views = views + 1 WHERE id=?", (m["id"],))
    chapters = q_all("SELECT * FROM chapters WHERE manhwa_id=? ORDER BY number ASC", (m["id"],))

    ravg = q_one("SELECT AVG(stars) a, COUNT(*) c FROM ratings WHERE manhwa_id=?", (m["id"],))
    avg = round(ravg["a"], 1) if ravg["a"] else 0
    rcount = ravg["c"]
    my_stars = 0
    bookmarked = False
    if user:
        r = q_one("SELECT stars FROM ratings WHERE manhwa_id=? AND user_id=?", (m["id"], user["id"]))
        my_stars = r["stars"] if r else 0
        bookmarked = bool(q_one("SELECT 1 FROM bookmarks WHERE manhwa_id=? AND user_id=?",
                                (m["id"], user["id"])))

    ch_rows = ""
    for c in chapters:
        lock = ""
        if c["is_premium"]:
            unlocked = user and q_one("SELECT 1 FROM unlocks WHERE user_id=? AND chapter_id=?",
                                      (user["id"], c["id"]))
            lock = "✓ Unlocked" if unlocked else f'🔒 {c["cost"]} coins'
        ch_rows += f"""<a class="chapter-row" href="/read/{m['slug']}/{c['id']}">
          <span dir="auto"><b>Chapter {c['number']:g}</b> · {esc(c['title'] or '')}</span>
          <span class="lock">{lock}</span></a>"""

    genres = "".join(f'<span class="chip">{esc(g)}</span>' for g in (m["genres"] or "").split(",") if g)
    cover_url = m["cover"] or ""
    cover = f'/proxy?url={urllib.parse.quote_plus(cover_url)}' if cover_url else ""
    bm_btn = (f'<button class="btn {"gold" if bookmarked else ""}" id="bm">'
              f'{"★ Bookmarked" if bookmarked else "☆ Bookmark"}</button>') if user else \
             '<a class="btn ghost" href="/login">Sign in to bookmark</a>'

    stars_html = "".join(
        f'<span data-v="{i}" style="color:{"#ffca3a" if i<=my_stars else "#555"}">★</span>'
        for i in range(1, 6))

    og = {"title": m["title"], "description": (m["description"] or "")[:180],
          "image": m["cover"] or "", "url": f"{PUBLIC_URL}/manhwa/{slug}" if PUBLIC_URL else ""}

    body = f"""
    <div class="detail">
      <img class="cover" src="{cover}" alt="{esc(m['title'])}">
      <div class="info">
        <h1 dir="auto">{esc(m['title'])}</h1>
        <div class="chips" style="margin:10px 0">{genres}
          <span class="chip">{esc(m['status'])}</span>
          <span class="chip">👁 {m['views']}</span></div>
        <div class="stars" id="stars" data-mid="{m['id']}">{stars_html}
          <span style="font-size:15px;color:var(--muted);margin-left:8px">
            {avg}/5 ({rcount} ratings)</span></div>
        <p style="margin:14px 0;color:#cfcfe0" dir="auto">{esc(m['description'] or '')}</p>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          {('<a class="btn" href="/read/'+m['slug']+'/'+str(chapters[0]['id'])+'">▶ Read First</a>') if chapters else ''}
          {bm_btn}
        </div>
      </div>
    </div>
    <h2 class="sec-title" dir="auto">Chapters</h2>
    <div class="chapter-list">{ch_rows or '<div class="bubble" dir="auto">No chapters yet.</div>'}</div>
    <script>
      const stars=document.getElementById('stars');
      if(stars) stars.querySelectorAll('span[data-v]').forEach(s=>s.onclick=async()=>{{
        const v=s.dataset.v, mid=stars.dataset.mid;
        const r=await fetch('/api/rate',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{manhwa_id:+mid,stars:+v}})}});
        if(r.status===401){{alert('Sign in to rate!');location='/login';return;}}
        location.reload();
      }});
      const bm=document.getElementById('bm');
      if(bm) bm.onclick=async()=>{{
        const r=await fetch('/api/bookmark',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{manhwa_id:{m['id']}}})}});
        if(r.status===401){{location='/login';return;}}
        location.reload();
      }};
    </script>
    """
    return HTMLResponse(page_shell(m["title"], body, og=og, user=user))


@app.get("/read/{slug}/{chapter_id}", response_class=HTMLResponse)
async def reader(request: Request, slug: str, chapter_id: int):
    user = get_web_user(request)
    m = q_one("SELECT * FROM manhwas WHERE slug=?", (slug,))
    c = q_one("SELECT * FROM chapters WHERE id=? AND manhwa_id=?", (chapter_id, m["id"] if m else 0))
    if not m or not c:
        raise HTTPException(404, "Chapter not found")

    if c["is_premium"]:
        if not user:
            body = f"""<div class="bubble pink" dir="auto">🔒 <b>Premium Chapter</b> — sign in with Telegram to unlock
              for {c['cost']} coins.</div><a class="btn" href="/login">Sign in</a>"""
            return HTMLResponse(page_shell("Locked", body, user=user))
        unlocked = q_one("SELECT 1 FROM unlocks WHERE user_id=? AND chapter_id=?", (user["id"], c["id"]))
        if not unlocked:
            body = f"""
            <div class="bubble pink" dir="auto">🔒 <b>Premium Chapter {c['number']:g}</b> costs {c['cost']} coins.
              You have 💰 {user['coins']}.</div>
            <button class="btn gold" id="unlock">Unlock for {c['cost']} coins</button>
            <script>
              document.getElementById('unlock').onclick=async()=>{{
                const r=await fetch('/api/unlock',{{method:'POST',headers:{{'Content-Type':'application/json'}},
                  body:JSON.stringify({{chapter_id:{c['id']}}})}});
                const d=await r.json();
                if(d.ok) location.reload(); else alert(d.error||'Not enough coins!');
              }};
            </script>"""
            return HTMLResponse(page_shell("Unlock", body, user=user))

    try:
        images = json.loads(c["images"] or "[]")
    except Exception:
        images = []
    
    # URL-encoding image links in HTML correctly to avoid breaking query params in proxy
    strip = "".join(
        f'<img loading="lazy" src="/proxy?url={urllib.parse.quote_plus(u)}" alt="page">' for u in images)

    nxt = q_one("SELECT id FROM chapters WHERE manhwa_id=? AND number>? ORDER BY number ASC LIMIT 1",
                (m["id"], c["number"]))
    prv = q_one("SELECT id FROM chapters WHERE manhwa_id=? AND number<? ORDER BY number DESC LIMIT 1",
                (m["id"], c["number"]))
    nav = ""
    if prv:
        nav += f'<a class="btn ghost" href="/read/{slug}/{prv["id"]}">← Prev</a>'
    if nxt:
        nav += f'<a class="btn" href="/read/{slug}/{nxt["id"]}">Next →</a>'

    comments = q_all(
        "SELECT * FROM comments WHERE chapter_id=? ORDER BY created_at DESC LIMIT 50", (c["id"],))
    clist = "".join(
        f"""<div class="comment" dir="auto"><span class="who">{esc(cm['name'] or 'Anon')}</span>
          <span class="when"> · {esc(cm['created_at'])}</span>
          <div>{(esc(cm['reaction'] or ''))} {esc(cm['body'] or '')}</div></div>""" for cm in comments) \
        or '<div class="bubble" dir="auto">Be the first to comment! 💬</div>'

    body = f"""
    <div class="reader-bar">
      <a href="/manhwa/{slug}"><b>{esc(m['title'])}</b> · Ch {c['number']:g}</a>
      <div style="display:flex;gap:8px">{nav}</div>
    </div>
    <div class="reader" id="reader">{strip or '<div class="bubble" dir="auto">No pages.</div>'}</div>
    <div class="react-bar" id="reacts">
      <span class="react" data-r="🔥">🔥</span>
      <span class="react" data-r="💖">💖</span>
      <span class="react" data-r="😮">😮</span>
      <span class="react" data-r="😂">😂</span>
    </div>
    <h2 class="sec-title" dir="auto">Comments</h2>
    <div style="margin-bottom:14px">
      <textarea id="cbody" rows="2" placeholder="Drop a comment..." dir="auto"></textarea>
      <button class="btn" id="csend" style="margin-top:8px">Post Comment</button>
    </div>
    <div id="clist">{clist}</div>
    <script>
      const CID={c['id']}, MID={m['id']}, SLUG="{slug}";
      const KEY='read_pos_'+CID;
      window.addEventListener('load',()=>{{
        const y=localStorage.getItem(KEY);
        if(y) window.scrollTo(0,+y);
      }});
      let t;
      window.addEventListener('scroll',()=>{{
        clearTimeout(t);
        t=setTimeout(()=>{{
          localStorage.setItem(KEY,window.scrollY);
          localStorage.setItem('last_read',JSON.stringify({{slug:SLUG,cid:CID}}));
          fetch('/api/progress',{{method:'POST',headers:{{'Content-Type':'application/json'}},
            body:JSON.stringify({{manhwa_id:MID,chapter_id:CID,scroll_y:Math.round(window.scrollY)}})}}).catch(()=>{{}});
        }},800);
      }});
      document.querySelectorAll('.react').forEach(r=>r.onclick=async()=>{{
        await fetch('/api/comment',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{chapter_id:CID,reaction:r.dataset.r,body:''}})}});
        r.style.transform='scale(1.4)';setTimeout(()=>r.style.transform='',250);
      }});
      document.getElementById('csend').onclick=async()=>{{
        const b=document.getElementById('cbody').value.trim();
        if(!b) return;
        const r=await fetch('/api/comment',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{chapter_id:CID,body:b}})}});
        if(r.ok) location.reload();
      }};
    </script>
    """
    og = {"title": f"{m['title']} — Chapter {c['number']:g}", "image": m["cover"] or "",
          "description": (m["description"] or "")[:180]}
    return HTMLResponse(page_shell(f"{m['title']} Ch {c['number']:g}", body, og=og, user=user))


@app.get("/bookmarks", response_class=HTMLResponse)
async def bookmarks_page(request: Request):
    user = get_web_user(request)
    if not user:
        return RedirectResponse("/login")
    rows = q_all(
        "SELECT m.* FROM bookmarks b JOIN manhwas m ON m.id=b.manhwa_id "
        "WHERE b.user_id=? ORDER BY b.created_at DESC", (user["id"],))
    grid = "".join(card_html(m) for m in rows) or \
        '<div class="bubble" dir="auto">No bookmarks yet. Go find something to love! 💖</div>'
    body = f"""
    <div class="bubble pink" dir="auto">Hey {esc(user['first_name'] or 'reader')}! 💰 Balance: <b>{user['coins']} coins</b>
      · <a href="/logout" style="color:#fff;text-decoration:underline">Logout</a></div>
    <h2 class="sec-title" dir="auto">Your Library</h2><div class="grid">{grid}</div>"""
    return HTMLResponse(page_shell("My Library", body, user=user))


# ==============================================================================
#  WEB LOGIN — SIGN IN WITH TELEGRAM
# ==============================================================================
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    body = f"""
    <h2 class="sec-title" dir="auto">Sign in with Telegram</h2>
    <div class="bubble" dir="auto">1. Open the bot on Telegram and send <b>/login</b><br>
      2. Copy the token it gives you<br>3. Paste it below. Done! 🎉</div>
    <div style="max-width:420px">
      <input class="txt" id="tok" placeholder="Paste your login token here" dir="auto">
      <button class="btn" id="go" style="margin-top:10px">Sign In</button>
      <div id="msg" style="margin-top:10px"></div>
    </div>
    <script>
      document.getElementById('go').onclick=async()=>{{
        const token=document.getElementById('tok').value.trim();
        const r=await fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{token}})}});
        const d=await r.json();
        if(d.ok){{location='/';}}
        else document.getElementById('msg').innerHTML='<div class="bubble pink" dir="auto">Invalid token 😢</div>';
      }};
    </script>"""
    return HTMLResponse(page_shell("Login", body))


@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    token = (data.get("token") or "").strip()
    u = q_one("SELECT * FROM users WHERE web_token=? AND web_token != ''", (token,))
    if not token or not u:
        return JSONResponse({"ok": False}, status_code=401)
    sid = secrets.token_urlsafe(32)
    q_exec("INSERT INTO sessions (token,user_id) VALUES (?,?)", (sid, u["id"]))
    q_exec("UPDATE users SET web_token='' WHERE id=?", (u["id"],))
    resp = JSONResponse({"ok": True})
    resp.set_cookie("sid", sid, httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("sid")
    if token:
        q_exec("DELETE FROM sessions WHERE token=?", (token,))
    resp = RedirectResponse("/")
    resp.delete_cookie("sid")
    return resp


# ==============================================================================
#  WEB API — RATE / BOOKMARK / COMMENT / PROGRESS / UNLOCK
# ==============================================================================
def require_web_user(request: Request) -> Any:
    u = get_web_user(request)
    if not u:
        raise HTTPException(401, "Login required")
    return u


@app.post("/api/rate")
async def api_rate(request: Request):
    u = get_web_user(request)
    if not u:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    stars = max(1, min(5, int(data.get("stars", 0))))
    mid = int(data.get("manhwa_id", 0))
    q_exec("INSERT INTO ratings (user_id,manhwa_id,stars) VALUES (?,?,?) "
           "ON CONFLICT(user_id,manhwa_id) DO UPDATE SET stars=?", (u["id"], mid, stars, stars))
    return {"ok": True}


@app.post("/api/bookmark")
async def api_bookmark(request: Request):
    u = get_web_user(request)
    if not u:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    mid = int(data.get("manhwa_id", 0))
    existing = q_one("SELECT 1 FROM bookmarks WHERE user_id=? AND manhwa_id=?", (u["id"], mid))
    if existing:
        q_exec("DELETE FROM bookmarks WHERE user_id=? AND manhwa_id=?", (u["id"], mid))
        return {"ok": True, "bookmarked": False}
    q_exec("INSERT INTO bookmarks (user_id,manhwa_id) VALUES (?,?)", (u["id"], mid))
    return {"ok": True, "bookmarked": True}


@app.post("/api/comment")
async def api_comment(request: Request):
    data = await request.json()
    u = get_web_user(request)
    cid = int(data.get("chapter_id", 0))
    body = (data.get("body") or "").strip()[:1000]
    reaction = (data.get("reaction") or "").strip()[:8]
    name = (u["first_name"] if u else "Anon") or "Anon"
    q_exec("INSERT INTO comments (chapter_id,user_id,name,body,reaction) VALUES (?,?,?,?,?)",
           (cid, u["id"] if u else None, name, body, reaction))
    return {"ok": True}


@app.post("/api/progress")
async def api_progress(request: Request):
    u = get_web_user(request)
    if not u:
        return {"ok": False}
    data = await request.json()
    q_exec(
        "INSERT INTO history (user_id,manhwa_id,chapter_id,scroll_y,updated_at) "
        "VALUES (?,?,?,?,datetime('now')) "
        "ON CONFLICT(user_id,manhwa_id) DO UPDATE SET chapter_id=?,scroll_y=?,updated_at=datetime('now')",
        (u["id"], int(data.get("manhwa_id", 0)), int(data.get("chapter_id", 0)),
         int(data.get("scroll_y", 0)), int(data.get("chapter_id", 0)), int(data.get("scroll_y", 0))))
    return {"ok": True}


@app.post("/api/unlock")
async def api_unlock(request: Request):
    u = get_web_user(request)
    if not u:
        return JSONResponse({"ok": False, "error": "Login required"}, status_code=401)
    data = await request.json()
    cid = int(data.get("chapter_id", 0))
    ch = q_one("SELECT * FROM chapters WHERE id=?", (cid,))
    if not ch or not ch["is_premium"]:
        return {"ok": True}
    if q_one("SELECT 1 FROM unlocks WHERE user_id=? AND chapter_id=?", (u["id"], cid)):
        return {"ok": True}
    fresh = q_one("SELECT coins FROM users WHERE id=?", (u["id"],))
    if fresh["coins"] < ch["cost"]:
        return {"ok": False, "error": "Not enough coins!"}
    add_coins(u["id"], -ch["cost"], f"unlock chapter {cid}")
    q_exec("INSERT OR IGNORE INTO unlocks (user_id,chapter_id) VALUES (?,?)", (u["id"], cid))
    return {"ok": True}


# ==============================================================================
#  IMAGE PROXY + CACHE (BUG-FREE & HIGH PERFORMANCE)
# ==============================================================================
@app.get("/proxy")
async def proxy(url: str = Query(...)):
    """Proxy + cache external images securely."""
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Bad url")
    
    # Using deterministic MD5 hashes for persistent caching across app reboots
    key = hashlib.md5(url.encode('utf-8', errors='ignore')).hexdigest()
    cache_file = os.path.join(CACHE_DIR, key + ".img")
    
    if os.path.exists(cache_file):
        return FileResponse(cache_file, media_type=guess_mime(cache_file),
                            headers={"Cache-Control": "public, max-age=604800"})
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": ""})
            r.raise_for_status()
            content = r.content
            mime = r.headers.get("content-type", "image/jpeg")
        
        if len(content) < 8 * 1024 * 1024:
            try:
                with open(cache_file, "wb") as f:
                    f.write(content)
            except Exception:
                pass
        return Response(content=content, media_type=mime,
                        headers={"Cache-Control": "public, max-age=604800"})
    except Exception as e:
        log.warning("proxy failed for %s: %s", url, e)
        px = bytes.fromhex(
            "47494638396101000100800000000000ffffff21f90401000000002c00000000"
            "010001000002024401003b")
        return Response(content=px, media_type="image/gif")


def guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "image/jpeg"


# ==============================================================================
#  PWA + STATIC-ISH ROUTES
# ==============================================================================
@app.get("/manifest.json")
async def manifest():
    return JSONResponse(MANIFEST)


@app.get("/sw.js")
async def service_worker():
    sw = """
const CACHE='manhwa-ink-v1';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>self.clients.claim());
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  e.respondWith(
    caches.open(CACHE).then(c=>c.match(e.request).then(r=>{
      const f=fetch(e.request).then(res=>{
        try{if(res.ok&&e.request.url.includes('/proxy'))c.put(e.request,res.clone());}catch(_){}
        return res;
      }).catch(()=>r);
      return r||f;
    }))
  );
});"""
    return PlainTextResponse(sw, media_type="application/javascript")


@app.get("/icon.png")
async def icon():
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360f8cf000000030001fe00fe0a0000000049454e44ae426082")
    return Response(content=png, media_type="image/png")


@app.get("/health")
async def health():
    return {"ok": True, "bot": bool(bot), "uptime": int(time.time() - STATE["started_at"])}


@app.post("/webhook/{token}")
async def webhook_stub(token: str):
    if token != BOT_TOKEN:
        raise HTTPException(403, "forbidden")
    return {"ok": True, "note": "Bot runs via MTProto polling; webhook not required."}


# ==============================================================================
#  ADMIN ANALYTICS DASHBOARD
# ==============================================================================
@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, key: str = ""):
    if key != ADMIN_PASSWORD:
        return HTMLResponse(page_shell("Admin", """
        <h2 class="sec-title">Admin Dashboard</h2>
        <div class="bubble pink">🔐 Access denied. Append <b>?key=YOUR_ADMIN_PASSWORD</b> to the URL.</div>"""))

    users = q_one("SELECT COUNT(*) c FROM users")["c"]
    manhwas = q_one("SELECT COUNT(*) c FROM manhwas")["c"]
    chapters = q_one("SELECT COUNT(*) c FROM chapters")["c"]
    comments = q_one("SELECT COUNT(*) c FROM comments")["c"]
    sessions = q_one("SELECT COUNT(*) c FROM sessions")["c"]
    coins = q_one("SELECT COALESCE(SUM(coins),0) c FROM users")["c"]
    top = q_all("SELECT title,views FROM manhwas ORDER BY views DESC LIMIT 8")

    try:
        load1, load5, load15 = os.getloadavg()
        load = f"{load1:.2f} / {load5:.2f} / {load15:.2f}"
    except Exception:
        load = "n/a"
    uptime = str(datetime.timedelta(seconds=int(time.time() - STATE["started_at"])))

    top_rows = "".join(f"<tr><td>{esc(t['title'])}</td><td>{t['views']}</td></tr>" for t in top)

    body = f"""
    <h2 class="sec-title">📊 Live Analytics</h2>
    <div class="stat-grid">
      <div class="stat"><div class="n">{users}</div><div class="l">Total Users</div></div>
      <div class="stat"><div class="n">{sessions}</div><div class="l">Active Web Sessions</div></div>
      <div class="stat"><div class="n">{manhwas}</div><div class="l">Manhwas</div></div>
      <div class="stat"><div class="n">{chapters}</div><div class="l">Chapters</div></div>
      <div class="stat"><div class="n">{comments}</div><div class="l">Comments</div></div>
      <div class="stat"><div class="n">{coins}</div><div class="l">Coins in Economy</div></div>
    </div>
    <div class="stat-grid">
      <div class="stat"><div class="n" style="font-size:22px">{load}</div><div class="l">Server Load (1/5/15m)</div></div>
      <div class="stat"><div class="n" style="font-size:22px">{uptime}</div><div class="l">Uptime</div></div>
      <div class="stat"><div class="n" style="font-size:22px">{STATE['requests']}</div><div class="l">Total Requests</div></div>
      <div class="stat"><div class="n" style="font-size:22px">{'ON' if bot else 'OFF'}</div><div class="l">Bot Status</div></div>
    </div>
    <h2 class="sec-title">Most Read</h2>
    <table><tr><th>Title</th><th>Views</th></tr>{top_rows}</table>
    <script>setTimeout(()=>location.reload(),15000);</script>
    """
    return HTMLResponse(page_shell("Dashboard", body))


# ==============================================================================
#  AUTOMATIC BACKUP SCHEDULER
# ==============================================================================
async def make_backup_zip() -> io.BytesIO:
    with closing(db()) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(DB_PATH, arcname="manhwa.db")
    buf.seek(0)
    buf.name = f"manhwa_backup_{datetime.date.today()}.zip"
    return buf


async def auto_backup_scheduler():
    while True:
        await asyncio.sleep(60 * 60 * 12)
        if not bot or not ADMIN_IDS:
            continue
        try:
            buf = await make_backup_zip()
            for aid in ADMIN_IDS:
                try:
                    await bot.send_document(aid, buf, caption="🗄 Automatic DB backup")
                    buf.seek(0)
                except Exception as e:
                    log.warning("backup to %s failed: %s", aid, e)
        except Exception as e:
            log.error("auto backup failed: %s", e)


# ==============================================================================
#  TELEGRAM BOT — HANDLERS
# ==============================================================================
BOT_FLOW: Dict[int, Dict[str, Any]] = {}
BOT_LAST: Dict[int, float] = {}
FLOOD_SECONDS = 0.7


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS or bool(q_one("SELECT 1 FROM users WHERE id=? AND is_admin=1", (uid,)))


def anti_flood(uid: int) -> bool:
    now = time.time()
    last = BOT_LAST.get(uid, 0)
    BOT_LAST[uid] = now
    return (now - last) >= FLOOD_SECONDS


async def post_to_channel(manhwa: Any, chapter: Any):
    target = get_channel_id()
    if not bot or not target:
        return
    read_url = f"{PUBLIC_URL}/read/{manhwa['slug']}/{chapter['id']}" if PUBLIC_URL else ""
    genres = " · ".join(g for g in (manhwa["genres"] or "").split(",") if g)
    caption = (
        f"🆕 **NEW CHAPTER DROP!**\n\n"
        f"📖 **{manhwa['title']}**\n"
        f"🔖 Chapter {chapter['number']:g}"
        f"{' — ' + chapter['title'] if chapter['title'] else ''}\n"
        f"🏷 {genres}\n"
        f"📌 Status: {manhwa['status']}\n\n"
        f"🖤 _Read it now on Manhwa-Ink!_"
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📖 Read Now", url=read_url)]]) if read_url else None
    try:
        cover = manhwa["cover"]
        if cover:
            await bot.send_photo(target, cover, caption=caption, reply_markup=kb)
        else:
            await bot.send_message(target, caption, reply_markup=kb)
    except Exception as e:
        log.warning("channel post failed: %s", e)


def register_bot_handlers(app_bot: "Client"):
    @app_bot.on_message(filters.command("start") & filters.private)
    async def start_cmd(client, message: Message):
        if not anti_flood(message.from_user.id):
            return
        u = ensure_user(message.from_user.id, message.from_user.username or "",
                        message.from_user.first_name or "")
        web = PUBLIC_URL or "the website"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Open Reader", url=PUBLIC_URL or "https://t.me")],
            [InlineKeyboardButton("🎡 Daily Wheel", callback_data="wheel"),
             InlineKeyboardButton("💰 Balance", callback_data="balance")],
        ])
        await message.reply(
            f"🖤 **Welcome to Manhwa-Ink, {message.from_user.first_name}!**\n\n"
            f"Your gateway to the ink-stained multiverse.\n\n"
            f"💰 You have **{u['coins']} coins**.\n\n"
            f"Commands:\n"
            f"• /login — get a web login token\n"
            f"• /wheel — spin the daily lucky wheel\n"
            f"• /balance — check your coins\n"
            f"• /help — all commands\n\n"
            f"🌐 Reader: {web}",
            reply_markup=kb,
        )

    @app_bot.on_message(filters.command("help") & filters.private)
    async def help_cmd(client, message: Message):
        if not anti_flood(message.from_user.id):
            return
        ensure_user(message.from_user.id, message.from_user.username or "",
                    message.from_user.first_name or "")
        txt = ("**📚 Commands**\n\n"
               "/start — welcome\n/login — web login token\n/wheel — daily coins\n"
               "/balance — your coins\n")
        if is_admin(message.from_user.id):
            txt += "\n**🛠 Admin**\n/admin — admin panel\n/backup — DB backup\n/stats — quick stats\n"
        await message.reply(txt)

    @app_bot.on_message(filters.command("login") & filters.private)
    async def login_cmd(client, message: Message):
        if not anti_flood(message.from_user.id):
            return
        u = ensure_user(message.from_user.id, message.from_user.username or "",
                        message.from_user.first_name or "")
        token = secrets.token_urlsafe(18)
        q_exec("UPDATE users SET web_token=? WHERE id=?", (token, u["id"]))
        login_url = f"{PUBLIC_URL}/login" if PUBLIC_URL else "the /login page"
        await message.reply(
            f"🔑 **Your one-time web login token:**\n\n`{token}`\n\n"
            f"Go to {login_url}, paste it, and you're synced across devices! "
            f"(Valid for a single use.)")

    @app_bot.on_message(filters.command("balance") & filters.private)
    async def balance_cmd(client, message: Message):
        if not anti_flood(message.from_user.id):
            return
        u = ensure_user(message.from_user.id, message.from_user.username or "",
                        message.from_user.first_name or "")
        await message.reply(f"💰 You have **{u['coins']} coins**.")

    @app_bot.on_message(filters.command("wheel") & filters.private)
    async def wheel_cmd(client, message: Message):
        if not anti_flood(message.from_user.id):
            return
        await spin_wheel(message.from_user, message.reply)

    @app_bot.on_message(filters.command("stats") & filters.private)
    async def stats_cmd(client, message: Message):
        if not is_admin(message.from_user.id):
            return
        u = q_one("SELECT COUNT(*) c FROM users")["c"]
        m = q_one("SELECT COUNT(*) c FROM manhwas")["c"]
        ch = q_one("SELECT COUNT(*) c FROM chapters")["c"]
        cm = q_one("SELECT COUNT(*) c FROM comments")["c"]
        await message.reply(
            f"📊 **Quick Stats**\n\n👥 Users: {u}\n📚 Manhwas: {m}\n"
            f"📖 Chapters: {ch}\n💬 Comments: {cm}")

    @app_bot.on_message(filters.command("backup") & filters.private)
    async def backup_cmd(client, message: Message):
        if not is_admin(message.from_user.id):
            return await message.reply("⛔ Admins only.")
        await message.reply("🗄 Preparing backup...")
        try:
            buf = await make_backup_zip()
            await message.reply_document(buf, caption="✅ Database backup")
        except Exception as e:
            await message.reply(f"❌ Backup failed: {e}")

    @app_bot.on_message(filters.command("admin") & filters.private)
    async def admin_cmd(client, message: Message):
        if not is_admin(message.from_user.id):
            return await message.reply("⛔ You are not an admin.")
        await message.reply("🛠 **Admin Panel**", reply_markup=admin_keyboard())

    @app_bot.on_callback_query()
    async def cb_handler(client, cq: CallbackQuery):
        data = cq.data
        uid = cq.from_user.id
        try:
            if data == "wheel":
                await cq.answer()
                await spin_wheel(cq.from_user, cq.message.reply)
            elif data == "balance":
                u = ensure_user(uid, cq.from_user.username or "", cq.from_user.first_name or "")
                await cq.answer(f"You have {u['coins']} coins 💰", show_alert=True)
            elif data == "adm_home" and is_admin(uid):
                await cq.message.edit_text("🛠 **Admin Panel**", reply_markup=admin_keyboard())
            elif data == "adm_addm" and is_admin(uid):
                BOT_FLOW[uid] = {"action": "add_manhwa"}
                await cq.message.edit_text(
                    "➕ **Add Manhwa**\nSend as ONE message, pipe-separated:\n\n"
                    "`Title | slug | Description | CoverURL | Genre1,Genre2 | Ongoing`")
            elif data == "adm_addc" and is_admin(uid):
                BOT_FLOW[uid] = {"action": "add_chapter"}
                mans = q_all("SELECT id,title FROM manhwas ORDER BY id DESC LIMIT 20")
                lst = "\n".join(f"`{m['id']}` — {m['title']}" for m in mans)
                await cq.message.edit_text(
                    "➕ **Add Chapter**\nAvailable manhwa IDs:\n" + lst +
                    "\n\nSend pipe-separated:\n"
                    "`manhwa_id | number | Chapter Title | img1,img2,img3 | premium(0/1) | cost`")
            elif data == "adm_bc" and is_admin(uid):
                BOT_FLOW[uid] = {"action": "broadcast"}
                await cq.message.edit_text("📢 **Broadcast**\nSend the message to broadcast to all users.")
            elif data == "adm_stats" and is_admin(uid):
                u = q_one("SELECT COUNT(*) c FROM users")["c"]
                m = q_one("SELECT COUNT(*) c FROM manhwas")["c"]
                ch = q_one("SELECT COUNT(*) c FROM chapters")["c"]
                await cq.answer()
                await cq.message.edit_text(
                    f"📊 Users: {u} · Manhwas: {m} · Chapters: {ch}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅ Back", callback_data="adm_home")]]))
            else:
                await cq.answer()
        except Exception as e:
            log.warning("callback error: %s", e)
            try:
                await cq.answer("Something went wrong.", show_alert=True)
            except Exception:
                pass

    @app_bot.on_message(filters.private & filters.text & ~filters.command(
        ["start", "help", "login", "balance", "wheel", "admin", "backup", "stats"]))
    async def flow_handler(client, message: Message):
        uid = message.from_user.id
        if not anti_flood(uid):
            return
        flow = BOT_FLOW.get(uid)
        if not flow or not is_admin(uid):
            return
        action = flow["action"]
        try:
            if action == "add_manhwa":
                parts = [p.strip() for p in message.text.split("|")]
                if len(parts) < 5:
                    return await message.reply("❌ Need at least: Title | slug | Desc | Cover | Genres")
                title, slug, desc, cover, genres = parts[:5]
                status = parts[5] if len(parts) > 5 else "Ongoing"
                slug = slug or title.lower().replace(" ", "-")
                q_exec("INSERT INTO manhwas (slug,title,description,cover,genres,status) "
                       "VALUES (?,?,?,?,?,?)", (slug, title, desc, cover, genres, status))
                BOT_FLOW.pop(uid, None)
                await message.reply(f"✅ Added **{title}** (slug: `{slug}`)")

            elif action == "add_chapter":
                parts = [p.strip() for p in message.text.split("|")]
                if len(parts) < 4:
                    return await message.reply(
                        "❌ Need: manhwa_id | number | title | img1,img2 | premium | cost")
                mid = int(parts[0]); num = float(parts[1]); ctitle = parts[2]
                imgs = [u.strip() for u in parts[3].split(",") if u.strip()]
                premium = int(parts[4]) if len(parts) > 4 and parts[4] else 0
                cost = int(parts[5]) if len(parts) > 5 and parts[5] else (20 if premium else 0)
                man = q_one("SELECT * FROM manhwas WHERE id=?", (mid,))
                if not man:
                    return await message.reply("❌ Invalid manhwa_id.")
                cid = q_exec(
                    "INSERT INTO chapters (manhwa_id,number,title,images,is_premium,cost) "
                    "VALUES (?,?,?,?,?,?)",
                    (mid, num, ctitle, json.dumps(imgs), premium, cost))
                BOT_FLOW.pop(uid, None)
                await message.reply(f"✅ Added Chapter {num:g} to **{man['title']}**. Auto-posting...")
                ch = q_one("SELECT * FROM chapters WHERE id=?", (cid,))
                await post_to_channel(man, ch)

            elif action == "broadcast":
                BOT_FLOW.pop(uid, None)
                text = message.text
                users = q_all("SELECT id FROM users")
                sent = fail = 0
                await message.reply(f"📢 Broadcasting to {len(users)} users...")
                for row in users:
                    try:
                        await client.send_message(row["id"], text)
                        sent += 1
                        await asyncio.sleep(0.05)
                    except Exception:
                        fail += 1
                await message.reply(f"✅ Broadcast done. Sent: {sent}, Failed: {fail}")
        except Exception as e:
            BOT_FLOW.pop(uid, None)
            await message.reply(f"❌ Error: {e}")


def admin_keyboard() -> "InlineKeyboardMarkup":
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Manhwa", callback_data="adm_addm"),
         InlineKeyboardButton("➕ Add Chapter", callback_data="adm_addc")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_bc"),
         InlineKeyboardButton("📊 Quick Stats", callback_data="adm_stats")],
    ])


async def spin_wheel(tg_user, reply_fn):
    u = ensure_user(tg_user.id, tg_user.username or "", tg_user.first_name or "")
    now = datetime.datetime.utcnow()
    if u["last_wheel"]:
        try:
            last = datetime.datetime.fromisoformat(u["last_wheel"])
            if (now - last).total_seconds() < 86400:
                remain = 86400 - (now - last).total_seconds()
                hrs = int(remain // 3600); mins = int((remain % 3600) // 60)
                return await reply_fn(
                    f"⏳ You already spun today! Come back in **{hrs}h {mins}m**.")
        except Exception:
            pass
    prize = random.choices([5, 10, 20, 50, 100, 250],
                           weights=[35, 30, 18, 10, 5, 2])[0]
    add_coins(u["id"], prize, "daily wheel")
    q_exec("UPDATE users SET last_wheel=? WHERE id=?", (now.isoformat(), u["id"]))
    fresh = q_one("SELECT coins FROM users WHERE id=?", (u["id"],))
    await reply_fn(
        f"🎡 **The wheel spins...** 🎰\n\n"
        f"🎉 You won **{prize} coins**!\n"
        f"💰 New balance: **{fresh['coins']} coins**")


# ==============================================================================
#  ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    log.info("🚀 Launching Manhwa-Ink on port %s ...", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
