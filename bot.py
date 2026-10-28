"""
IMPERIYA — "Imperiyaning eng sodiq xodimi" konkurs boti

Render uchun:
- BOT_TOKEN environment variable
- DATABASE_URL environment variable (Render PostgreSQL Internal Database URL)
- ixtiyoriy: ADMIN_ID, CONTEST_DAYS
"""

import asyncio
import html
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("imperiya-bot")


# =========================================================
# RENDER PORT HEALTH SERVER (eng birinchi ishga tushadi)
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"IMPERIYA BOT OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health server portda ishga tushdi: %s", port)
    server.serve_forever()


threading.Thread(target=start_health_server, daemon=True).start()


# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable topilmadi.")

# Majburiy kanallar: (tugmadagi nom, kanal username yoki ID, havola)
# Bot HAR BIR kanalda ADMIN bo'lishi shart, aks holda obuna tekshirilmaydi.
# Yopiq kanal bo'lsa, username o'rniga raqamli ID yozing: -1001234567890
REQUIRED_CHANNELS = [
    ("Qog‘ay Online", "@qogay_online_rasmiy", "https://t.me/qogay_online_rasmiy"),
    ("Imperiya", "@imperiya_edu", "https://t.me/imperiya_edu"),
]

# Instagram sahifalar (bot buni tekshira olmaydi, faqat havola sifatida ko'rsatadi)
INSTAGRAM_PAGES = [
    ("Imperiya", "https://www.instagram.com/imperiya_edu/"),
    ("Komfort Mebellari", "https://www.instagram.com/komfort.mebellari/"),
]

ADMIN_ID = int(os.getenv("ADMIN_ID", "7050215692"))
DEFAULT_CONTEST_DAYS = int(os.getenv("CONTEST_DAYS", "7"))
REFERRALS_PER_BONUS = 5

PRIZES = [
    ("🥇", "1-o‘rin", "Kondisioner"),
    ("🥈", "2-o‘rin", "Televizor"),
    ("🥉", "3-o‘rin", "Kuller"),
]

CANDIDATES = [
    "3-Maktab",
    "12-Maktab",
    "15-Maktab",
    "16-Maktab",
    "17-Maktab",
    "18-Maktab",
    "19-Maktab",
    "20-Maktab",
    "21-Maktab",
    "22-Maktab",
    "23-Maktab",
    "24-Maktab",
    "25-Maktab",
    "26-Maktab",
    "27-Maktab",
    "32-Maktab",
    "33-Maktab",
    "36-Maktab",
    "41-Maktab",
    "44-Maktab",
]


# =========================================================
# DATABASE
# =========================================================

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # Local kompyuterda test qilish uchun SQLite fallback.
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE_DIR, "imperiya_konkurs.db")
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    cursor = db.cursor()
    DB_KIND = "sqlite"
else:
    # Render PostgreSQL. autocommit: hech qanday tranzaksiya qotib qolmaydi.
    db = psycopg2.connect(DATABASE_URL, connect_timeout=15)
    db.autocommit = True
    cursor = db.cursor(cursor_factory=RealDictCursor)
    DB_KIND = "postgres"

log.info("Database: %s", DB_KIND)


def execute(sql, params=()):
    """PostgreSQL va SQLite uchun bitta execute helper."""
    if DB_KIND == "postgres":
        sql = sql.replace("?", "%s")
    try:
        cursor.execute(sql, params)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    return cursor


def fetchone(sql, params=()):
    return execute(sql, params).fetchone()


def fetchall(sql, params=()):
    return execute(sql, params).fetchall()


def commit():
    try:
        db.commit()
    except Exception as e:
        log.warning("commit xatosi: %s", e)


def init_database():
    if DB_KIND == "postgres":
        statements = [
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                subscribed INTEGER DEFAULT 0,
                instagram_verified INTEGER DEFAULT 0,
                referrer_id BIGINT,
                referral_count INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                bonus_votes INTEGER DEFAULT 0,
                bonus_granted INTEGER DEFAULT 0,
                main_vote_used INTEGER DEFAULT 0,
                created_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS votes (
                vote_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                candidate TEXT NOT NULL,
                vote_type TEXT NOT NULL,
                created_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS referral_done (
                user_id BIGINT PRIMARY KEY,
                created_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """,
        ]
        for stmt in statements:
            cursor.execute(stmt)
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_granted INTEGER DEFAULT 0"
            )
        except Exception as e:
            log.warning("ALTER TABLE o‘tkazib yuborildi: %s", e)
    else:
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                subscribed INTEGER DEFAULT 0,
                instagram_verified INTEGER DEFAULT 0,
                referrer_id INTEGER,
                referral_count INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                bonus_votes INTEGER DEFAULT 0,
                bonus_granted INTEGER DEFAULT 0,
                main_vote_used INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS votes (
                vote_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                candidate TEXT NOT NULL,
                vote_type TEXT NOT NULL,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS referral_done (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        cols = [r["name"] for r in cursor.execute("PRAGMA table_info(users)")]
        if "bonus_granted" not in cols:
            cursor.execute("ALTER TABLE users ADD COLUMN bonus_granted INTEGER DEFAULT 0")
            cursor.execute(
                "UPDATE users SET bonus_granted = points / ?",
                (REFERRALS_PER_BONUS,),
            )
    commit()


init_database()


# =========================================================
# KONKURS VAQTI
# =========================================================

def get_setting(key: str, default=None):
    row = fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str):
    execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value
        """,
        (key, value),
    )
    commit()


def contest_start() -> datetime:
    value = get_setting("contest_start")
    if value:
        return datetime.fromisoformat(value)
    now = datetime.now()
    set_setting("contest_start", now.isoformat())
    return now


def contest_days() -> int:
    return int(get_setting("contest_days", DEFAULT_CONTEST_DAYS))


def contest_end() -> datetime:
    return contest_start() + timedelta(days=contest_days())


def contest_active() -> bool:
    return datetime.now() < contest_end()


def time_left_text() -> str:
    left = contest_end() - datetime.now()
    if left.total_seconds() <= 0:
        return "yakunlangan"
    days = left.days
    hours, rem = divmod(left.seconds, 3600)
    minutes = rem // 60
    return f"{days} kun {hours} soat {minutes} daqiqa"


contest_start()


# =========================================================
# YORDAMCHI
# =========================================================

def esc(text) -> str:
    return html.escape(str(text or ""))


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def save_user(user):
    execute(
        """
        INSERT INTO users (user_id, first_name, username, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name = EXCLUDED.first_name,
            username = EXCLUDED.username
        """,
        (user.id, user.first_name or "", user.username or "", datetime.now().isoformat()),
    )
    commit()


def get_user(user_id: int):
    return fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))


def user_has_requirements(user_id: int) -> bool:
    row = get_user(user_id)
    return bool(row and row["subscribed"] == 1 and row["instagram_verified"] == 1)


def vote_counts() -> list:
    counts = {c: 0 for c in CANDIDATES}
    for r in fetchall("SELECT candidate, COUNT(*) AS n FROM votes GROUP BY candidate"):
        if r["candidate"] in counts:
            counts[r["candidate"]] = int(r["n"])
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


def progress_bar(value: int, total: int, length: int = 10) -> str:
    if total <= 0:
        return "░" * length
    filled = round(value / total * length)
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


def prizes_text() -> str:
    return "\n".join(f"{icon} <b>{place}:</b> {prize}" for icon, place, prize in PRIZES)


def contest_text() -> str:
    return (
        "🏆 <b>IMPERIYANING ENG SODIQ XODIMI</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 <b>SOVG‘ALAR</b>\n"
        f"{prizes_text()}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"➕ Har {REFERRALS_PER_BONUS} ta haqiqiy referral = 1 ta bonus ovoz\n"
        f"⏳ Qolgan vaqt: <b>{time_left_text()}</b>\n\n"
        "⚠️ Soxta akkauntlar, nakrutka va sun’iy ovozlar hisobga olinmaydi. "
        "Qoidabuzarlik aniqlansa, ishtirokchi chetlashtiriladi."
    )


def leaderboard_text(title: str = "📈 REYTING") -> str:
    counts = vote_counts()
    total = sum(n for _, n in counts)
    top = counts[0][1] if counts else 0
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

    lines = [f"<b>{title}</b>", f"🗳 Jami ovozlar: <b>{total}</b>", ""]
    for i, (name, n) in enumerate(counts):
        icon = medals[i] if i < len(medals) else f"{i + 1}."
        percent = (n / total * 100) if total else 0
        lines.append(f"{icon} <b>{esc(name)}</b>")
        lines.append(f"<code>{progress_bar(n, top)}</code> {n} ta • {percent:.0f}%")
        lines.append("")
    return "\n".join(lines)


def votes_available(row) -> int:
    return (0 if row["main_vote_used"] else 1) + row["bonus_votes"]


# =========================================================
# KLAVIATURALAR
# =========================================================

def requirements_keyboard():
    return InlineKeyboardMarkup([
        *[
            [InlineKeyboardButton(f"📢 {title} — obuna bo‘lish", url=link)]
            for title, _chat, link in REQUIRED_CHANNELS
        ],
        *[
            [InlineKeyboardButton(f"📸 {title} — Instagram", url=link)]
            for title, link in INSTAGRAM_PAGES
        ],
        [InlineKeyboardButton("✅ Obunalarni tekshirish", callback_data="check_subscription")],
    ])


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗳 Ovoz berish", callback_data="show_candidates")],
        [InlineKeyboardButton("📈 Reyting", callback_data="leaderboard")],
        [
            InlineKeyboardButton("🔗 Referral", callback_data="my_referral"),
            InlineKeyboardButton("📊 Statistikam", callback_data="my_stats"),
        ],
    ])


def candidate_keyboard():
    rows = []
    for i in range(0, len(CANDIDATES), 2):
        rows.append([
            InlineKeyboardButton(f"👤 {CANDIDATES[j]}", callback_data=f"vote_{j}")
            for j in range(i, min(i + 2, len(CANDIDATES)))
        ])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(index: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"confirm_{index}"),
        InlineKeyboardButton("↩️ Boshqasini tanlash", callback_data="show_candidates"),
    ]])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Umumiy statistika", callback_data="admin_stats")],
        [InlineKeyboardButton("🏆 Natijalar", callback_data="admin_results")],
        [InlineKeyboardButton("👥 Ovoz berganlar", callback_data="admin_voters")],
        [InlineKeyboardButton("🎁 Referral statistikasi", callback_data="admin_referrals")],
        [InlineKeyboardButton("📢 Barchaga xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔄 Yangilash", callback_data="admin_panel")],
    ])


async def edit(query, text: str, keyboard=None):
    try:
        await query.edit_message_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning("edit xatosi: %s", e)


# =========================================================
# REFERRAL
# =========================================================

async def process_referral(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    row = get_user(user_id)
    if not row:
        return

    referrer_id = row["referrer_id"]
    if not referrer_id or referrer_id == user_id:
        return
    if row["subscribed"] != 1 or row["instagram_verified"] != 1:
        return
    if fetchone("SELECT 1 FROM referral_done WHERE user_id = ?", (user_id,)):
        return
    if not get_user(referrer_id):
        return

    execute(
        "INSERT INTO referral_done (user_id, created_at) VALUES (?, ?)",
        (user_id, datetime.now().isoformat()),
    )
    execute(
        "UPDATE users SET referral_count = referral_count + 1, points = points + 1 "
        "WHERE user_id = ?",
        (referrer_id,),
    )

    ref = get_user(referrer_id)
    should_have = ref["points"] // REFERRALS_PER_BONUS
    new_bonus = should_have - ref["bonus_granted"]
    if new_bonus > 0:
        execute(
            "UPDATE users SET bonus_votes = bonus_votes + ?, bonus_granted = ? WHERE user_id = ?",
            (new_bonus, should_have, referrer_id),
        )
    commit()

    try:
        text = (
            "🎉 <b>Yangi referral!</b>\n"
            f"Sizning havolangiz orqali <b>{esc(row['first_name'])}</b> qo‘shildi.\n"
            f"👥 Jami referral: <b>{ref['referral_count']}</b>"
        )
        if new_bonus > 0:
            text += f"\n🎁 Sizga <b>{new_bonus}</b> ta bonus ovoz berildi!"
        await context.bot.send_message(referrer_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("Referrerga xabar yuborilmadi: %s", e)


async def missing_channels(bot, user_id: int) -> list:
    """Foydalanuvchi obuna bo'lmagan kanallar nomlari ro'yxatini qaytaradi."""
    missing = []
    for title, chat, _link in REQUIRED_CHANNELS:
        ok = False
        try:
            member = await bot.get_chat_member(chat_id=chat, user_id=user_id)
            ok = member.status in ("member", "administrator", "creator") or (
                member.status == "restricted" and getattr(member, "is_member", False)
            )
        except Exception as e:
            log.warning("Obuna tekshiruvi xatosi (%s): %s", chat, e)
        if not ok:
            missing.append(title)
    return missing


# =========================================================
# START / OBUNA
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    if context.args:
        try:
            referrer_id = int(context.args[0])
            row = get_user(user.id)
            if referrer_id != user.id and row["referrer_id"] is None and get_user(referrer_id):
                execute(
                    "UPDATE users SET referrer_id = ? WHERE user_id = ?",
                    (referrer_id, user.id),
                )
                commit()
        except (ValueError, TypeError):
            pass

    if not contest_active():
        await update.message.reply_text(
            "⛔ <b>Konkurs yakunlangan.</b>\n\n"
            f"Tugagan vaqt: {contest_end().strftime('%d.%m.%Y %H:%M')}",
            parse_mode=ParseMode.HTML,
        )
        return

    if user_has_requirements(user.id):
        await process_referral(user.id, context)
        await update.message.reply_text(
            contest_text(), reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML
        )
        return

    await update.message.reply_text(
        contest_text()
        + "\n\n🔐 <b>Ovoz berishdan oldin:</b>\n"
        f"1️⃣ {len(REQUIRED_CHANNELS)} ta Telegram kanalga obuna bo‘ling\n"
        f"2️⃣ {len(INSTAGRAM_PAGES)} ta Instagram sahifaga obuna bo‘ling\n\n"
        "So‘ng «Obunalarni tekshirish» tugmasini bosing.",
        reply_markup=requirements_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    save_user(query.from_user)

    missing = await missing_channels(context.bot, user_id)
    telegram_ok = not missing

    execute(
        "UPDATE users SET subscribed = ?, instagram_verified = 1 WHERE user_id = ?",
        (1 if telegram_ok else 0, user_id),
    )
    commit()

    if not telegram_ok:
        names = "\n".join(f"• {esc(n)}" for n in missing)
        await edit(
            query,
            "❌ <b>Quyidagi kanallarga obuna bo‘lmagansiz:</b>\n\n"
            f"{names}\n\n"
            "Obuna bo‘ling va qayta tekshiring.",
            requirements_keyboard(),
        )
        return

    await process_referral(user_id, context)
    await edit(
        query,
        "✅ <b>Shartlar qabul qilindi!</b>\n\nEndi konkursda ishtirok etishingiz mumkin.",
        main_menu_keyboard(),
    )


# =========================================================
# OVOZ BERISH
# =========================================================

async def _guard(query, context) -> bool:
    if not contest_active():
        await edit(query, "⛔ <b>Konkurs yakunlangan.</b>")
        return False

    if not user_has_requirements(query.from_user.id) or await missing_channels(
        context.bot, query.from_user.id
    ):
        execute("UPDATE users SET subscribed = 0 WHERE user_id = ?", (query.from_user.id,))
        commit()
        await edit(
            query,
            "❌ Avval barcha majburiy kanallarga obuna bo‘ling.",
            requirements_keyboard(),
        )
        return False
    return True


async def show_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await _guard(query, context):
        return

    row = get_user(query.from_user.id)
    left = votes_available(row)

    if left <= 0:
        await edit(
            query,
            "ℹ️ <b>Sizning ovozlaringiz tugagan.</b>\n\n"
            "Qo‘shimcha ovoz olish uchun do‘stlaringizni taklif qiling: "
            f"har {REFERRALS_PER_BONUS} ta referral = 1 ovoz.",
            main_menu_keyboard(),
        )
        return

    await edit(
        query,
        "🗳 <b>OVOZ BERISH</b>\n\n"
        f"🎟 Sizda <b>{left}</b> ta ovoz bor.\n"
        "Kimga ovoz bermoqchisiz? Xodimni tanlang 👇",
        candidate_keyboard(),
    )


async def vote_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await _guard(query, context):
        return

    try:
        index = int(query.data.replace("vote_", ""))
        candidate = CANDIDATES[index]
    except (ValueError, IndexError):
        await query.answer("❌ Nomzod topilmadi.", show_alert=True)
        return

    await edit(
        query,
        "🗳 <b>OVOZNI TASDIQLANG</b>\n\n"
        f"👤 <b>{esc(candidate)}</b>\n\n"
        "Ovoz berilgach, uni qaytarib bo‘lmaydi.",
        confirm_keyboard(index),
    )


async def vote_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await _guard(query, context):
        return

    user_id = query.from_user.id
    try:
        index = int(query.data.replace("confirm_", ""))
        candidate = CANDIDATES[index]
    except (ValueError, IndexError):
        await query.answer("❌ Nomzod topilmadi.", show_alert=True)
        return

    # Shartli UPDATE: qayta bosilsa ham bitta ovoz ketadi.
    vote_type = None
    if execute(
        "UPDATE users SET main_vote_used = 1 WHERE user_id = ? AND main_vote_used = 0",
        (user_id,),
    ).rowcount == 1:
        vote_type = "main"
    elif execute(
        "UPDATE users SET bonus_votes = bonus_votes - 1 WHERE user_id = ? AND bonus_votes > 0",
        (user_id,),
    ).rowcount == 1:
        vote_type = "bonus"

    if vote_type is None:
        commit()
        await query.answer("❌ Sizda ovoz qolmagan.", show_alert=True)
        await edit(
            query,
            "ℹ️ <b>Sizning ovozlaringiz tugagan.</b>",
            main_menu_keyboard(),
        )
        return

    execute(
        "INSERT INTO votes (user_id, candidate, vote_type, created_at) VALUES (?, ?, ?, ?)",
        (user_id, candidate, vote_type, datetime.now().isoformat()),
    )
    commit()

    counts = vote_counts()
    rank = next(i for i, (name, _) in enumerate(counts, 1) if name == candidate)
    votes_now = dict(counts)[candidate]

    row = get_user(user_id)
    await edit(
        query,
        "✅ <b>OVOZINGIZ QABUL QILINDI!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Xodim: <b>{esc(candidate)}</b>\n"
        f"🎟 Ovoz turi: {'asosiy' if vote_type == 'main' else 'bonus'}\n"
        f"📈 Hozirgi o‘rni: <b>{rank}-o‘rin</b> ({votes_now} ta ovoz)\n\n"
        f"🎁 Qolgan ovozlaringiz: <b>{votes_available(row)}</b>\n\n"
        "Rahmat! Ko‘proq ovoz olish uchun do‘stlaringizni taklif qiling 🔗",
        main_menu_keyboard(),
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await edit(
        query,
        leaderboard_text() + f"\n⏳ Qolgan vaqt: <b>{time_left_text()}</b>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Yangilash", callback_data="leaderboard")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="back_main")],
        ]),
    )


# =========================================================
# REFERRAL / STATISTIKA
# =========================================================

async def _referral_text(context, user_id: int) -> str:
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={user_id}"
    row = get_user(user_id)
    until_next = REFERRALS_PER_BONUS - (row["points"] % REFERRALS_PER_BONUS)
    return (
        "🔗 <b>SIZNING REFERRAL HAVOLANGIZ</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"👥 Taklif qilganlar: <b>{row['referral_count']}</b> ta\n"
        f"⭐ Ball: <b>{row['points']}</b>\n"
        f"🎁 Bonus ovozlar: <b>{row['bonus_votes']}</b> ta\n"
        f"🎯 Keyingi bonusgacha: <b>{until_next}</b> ta referral\n\n"
        f"Har {REFERRALS_PER_BONUS} ta haqiqiy referral = 1 ta bonus ovoz."
    )


async def my_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await edit(query, await _referral_text(context, query.from_user.id), main_menu_keyboard())


async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    await update.message.reply_text(
        await _referral_text(context, update.effective_user.id),
        parse_mode=ParseMode.HTML,
    )


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    row = get_user(user_id)

    total_votes = fetchone(
        "SELECT COUNT(*) AS n FROM votes WHERE user_id = ?", (user_id,)
    )["n"]
    last = fetchone(
        "SELECT candidate FROM votes WHERE user_id = ? ORDER BY vote_id DESC LIMIT 1",
        (user_id,),
    )

    await edit(
        query,
        "📊 <b>SIZNING STATISTIKANGIZ</b>\n\n"
        f"👤 Ism: {esc(row['first_name'])}\n"
        f"👥 Referral: {row['referral_count']}\n"
        f"⭐ Ball: {row['points']}\n"
        f"🎁 Bonus ovozlar: {row['bonus_votes']}\n"
        f"🎟 Qolgan ovozlar: {votes_available(row)}\n"
        f"🗳 Jami bergan ovozlar: {total_votes}\n"
        f"🏆 Oxirgi ovoz: {esc(last['candidate']) if last else 'Hali ovoz berilmagan'}",
        main_menu_keyboard(),
    )


async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await edit(query, contest_text(), main_menu_keyboard())


# =========================================================
# ADMIN PANEL
# =========================================================

async def _admin_only(query) -> bool:
    if not is_admin(query.from_user.id):
        await query.answer("❌ Ruxsat yo‘q!", show_alert=True)
        return False
    await query.answer()
    return True


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Sizda admin huquqi mavjud emas.")
        return
    await update.message.reply_text(
        "🔐 <b>IMPERIYA KONKURS — ADMIN PANEL</b>\n\nKerakli bo‘limni tanlang:",
        reply_markup=admin_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _admin_only(query):
        return
    await edit(
        query,
        "🔐 <b>IMPERIYA KONKURS — ADMIN PANEL</b>\n\nKerakli bo‘limni tanlang:",
        admin_keyboard(),
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _admin_only(query):
        return

    users = fetchone("SELECT COUNT(*) AS n FROM users")["n"]
    subs = fetchone("SELECT COUNT(*) AS n FROM users WHERE subscribed = 1")["n"]
    insta = fetchone("SELECT COUNT(*) AS n FROM users WHERE instagram_verified = 1")["n"]
    votes = fetchone("SELECT COUNT(*) AS n FROM votes")["n"]
    voters = fetchone("SELECT COUNT(DISTINCT user_id) AS n FROM votes")["n"]
    referrals = fetchone("SELECT COALESCE(SUM(referral_count), 0) AS n FROM users")["n"]
    bonus = fetchone("SELECT COALESCE(SUM(bonus_votes), 0) AS n FROM users")["n"]

    await edit(
        query,
        "📊 <b>UMUMIY STATISTIKA</b>\n\n"
        f"👥 Jami foydalanuvchilar: {users}\n"
        f"📢 Telegram obunachilari: {subs}\n"
        f"📸 Instagram tasdiqlari: {insta}\n"
        f"🗳 Jami ovozlar: {votes}\n"
        f"👤 Ovoz berganlar: {voters}\n"
        f"🔗 Jami referral: {referrals}\n"
        f"🎁 Ishlatilmagan bonus: {bonus}\n\n"
        f"🟢 Konkurs: <b>{'FAOL' if contest_active() else 'YAKUNLANGAN'}</b>\n"
        f"📅 Davomiyligi: {contest_days()} kun\n"
        f"⏱ Tugash: {contest_end().strftime('%d.%m.%Y %H:%M')}\n"
        f"⏳ Qolgan: {time_left_text()}",
        admin_keyboard(),
    )


async def admin_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _admin_only(query):
        return
    await edit(query, leaderboard_text("🏆 KONKURS NATIJALARI"), admin_keyboard())


async def admin_voters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _admin_only(query):
        return

    voters = fetchall("""
        SELECT votes.vote_id, votes.user_id, votes.candidate, votes.vote_type,
               votes.created_at, users.first_name, users.username
        FROM votes LEFT JOIN users ON users.user_id = votes.user_id
        ORDER BY votes.vote_id DESC LIMIT 15
    """)

    if not voters:
        text = "👥 <b>OVOZ BERGANLAR</b>\n\nHozircha hech kim ovoz bermagan."
    else:
        text = "👥 <b>OVOZ BERGANLAR</b>\nOxirgi 15 ta ovoz:\n\n"
        for i, r in enumerate(voters, 1):
            try:
                date_text = datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M:%S")
            except Exception:
                date_text = r["created_at"]
            username = f"@{esc(r['username'])}" if r["username"] else "username yo‘q"
            text += (
                f"{i}. 👤 {esc(r['first_name'] or 'Noma’lum')} ({username})\n"
                f"   🆔 <code>{r['user_id']}</code> • 🕐 {date_text}\n"
                f"   🗳 {esc(r['candidate'])} • {r['vote_type']}\n\n"
            )

    await edit(query, text, admin_keyboard())


async def admin_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _admin_only(query):
        return

    rows = fetchall("""
        SELECT user_id, first_name, username, referral_count, points, bonus_votes
        FROM users WHERE referral_count > 0
        ORDER BY referral_count DESC LIMIT 20
    """)

    text = "🎁 <b>REFERRAL STATISTIKASI</b>\n\n"
    if not rows:
        text += "Hozircha referral mavjud emas."
    for i, r in enumerate(rows, 1):
        username = f"@{esc(r['username'])}" if r["username"] else "username yo‘q"
        text += (
            f"{i}. <b>{esc(r['first_name'] or 'Noma’lum')}</b> ({username})\n"
            f"   🆔 <code>{r['user_id']}</code>\n"
            f"   👥 {r['referral_count']} • ⭐ {r['points']} • 🎁 {r['bonus_votes']}\n\n"
        )

    await edit(query, text, admin_keyboard())


# =========================================================
# BROADCAST
# =========================================================

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _admin_only(query):
        return

    context.user_data["awaiting_broadcast"] = True
    await edit(
        query,
        "📢 <b>BARCHAGA XABAR YUBORISH</b>\n\n"
        "Yubormoqchi bo‘lgan xabaringizni shu yerga yuboring.\n"
        "Matn, rasm, video yoki boshqa Telegram xabarlarini yuborishingiz mumkin.\n\n"
        "❌ Bekor qilish uchun /cancel buyrug‘ini yuboring.",
    )


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    context.user_data.pop("awaiting_broadcast", None)
    await update.message.reply_text(
        "❌ <b>Broadcast bekor qilindi.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.user_data.get("awaiting_broadcast"):
        return

    message = update.message
    if not message:
        return

    context.user_data.pop("awaiting_broadcast", None)

    users = fetchall("SELECT user_id FROM users ORDER BY user_id")
    total = len(users)
    success = 0
    failed = 0

    status_message = await message.reply_text(
        "⏳ <b>Xabar yuborilmoqda...</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total}</b>",
        parse_mode=ParseMode.HTML,
    )

    for row in users:
        user_id = row["user_id"]
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
            success += 1
            await asyncio.sleep(0.04)  # flood limitdan saqlanish
        except Exception as e:
            failed += 1
            log.warning("Broadcast %s ID ga yuborilmadi: %s", user_id, e)

    await status_message.edit_text(
        "📢 <b>BROADCAST YAKUNLANDI</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Jami: <b>{total}</b>\n"
        f"✅ Yetkazildi: <b>{success}</b>\n"
        f"❌ Yetkazilmadi: <b>{failed}</b>\n\n"
        "Admin panelga qaytishingiz mumkin.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


# =========================================================
# ADMIN BUYRUQLARI
# =========================================================

async def restorevotes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /restorevotes USER_ID NOMZOD_RAQAMI OVOZ_SONI
    Masalan: /restorevotes 123456789 1 5
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Sizda admin huquqi mavjud emas.")
        return

    try:
        user_id = int(context.args[0])
        candidate_index = int(context.args[1]) - 1
        count = int(context.args[2])
        if count < 1 or candidate_index < 0:
            raise ValueError
        candidate = CANDIDATES[candidate_index]
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Buyruq noto‘g‘ri.\n\n"
            "Foydalanish:\n"
            "/restorevotes USER_ID NOMZOD_RAQAMI OVOZ_SONI\n\n"
            "Masalan:\n"
            "/restorevotes 123456789 1 5\n\n"
            "Nomzodlar:\n"
            + "\n".join(f"{i}. {name}" for i, name in enumerate(CANDIDATES, 1))
        )
        return

    if not get_user(user_id):
        await update.message.reply_text(f"❌ {user_id} ID li foydalanuvchi bazadan topilmadi.")
        return

    now = datetime.now().isoformat()
    for _ in range(count):
        execute(
            "INSERT INTO votes (user_id, candidate, vote_type, created_at) VALUES (?, ?, ?, ?)",
            (user_id, candidate, "admin_restore", now),
        )
    commit()

    total = fetchone("SELECT COUNT(*) AS n FROM votes WHERE candidate = ?", (candidate,))["n"]

    await update.message.reply_text(
        "✅ <b>Ovozlar qayta tiklandi!</b>\n\n"
        f"👤 Foydalanuvchi: <code>{user_id}</code>\n"
        f"🏆 Nomzod: <b>{esc(candidate)}</b>\n"
        f"➕ Tiklangan ovoz: <b>{count} ta</b>\n"
        f"📊 Nomzodning jami ovozi: <b>{total} ta</b>\n\n"
        "ℹ️ Bu ovozlar <code>admin_restore</code> sifatida saqlandi.",
        parse_mode=ParseMode.HTML,
    )


async def setdays_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        days = int(context.args[0])
        if days < 1:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Ishlatilishi: /setdays 10")
        return
    set_setting("contest_days", str(days))
    await update.message.reply_text(
        f"✅ Konkurs davomiyligi {days} kunga o‘rnatildi.\n"
        f"⏱ Tugash: {contest_end().strftime('%d.%m.%Y %H:%M')}"
    )


async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        days = int(context.args[0])
        if days < 1:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Ishlatilishi: /extend 3")
        return
    set_setting("contest_days", str(contest_days() + days))
    await update.message.reply_text(
        f"✅ Konkurs {days} kunga uzaytirildi.\n"
        f"⏱ Yangi tugash: {contest_end().strftime('%d.%m.%Y %H:%M')}"
    )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restart  yoki  /restart 15  (15 kunga qayta boshlash)"""
    if not is_admin(update.effective_user.id):
        return
    if context.args:
        try:
            days = int(context.args[0])
            if days < 1:
                raise ValueError
            set_setting("contest_days", str(days))
        except ValueError:
            await update.message.reply_text("Ishlatilishi: /restart 15")
            return
    set_setting("contest_start", datetime.now().isoformat())
    await update.message.reply_text(
        f"🔄 Konkurs hozirdan qayta boshlandi ({contest_days()} kun).\n"
        f"⏱ Tugash: {contest_end().strftime('%d.%m.%Y %H:%M')}"
    )


async def resetvotes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hammasini noldan boshlash: ovozlar, referrallar, bonuslar o'chadi."""
    if not is_admin(update.effective_user.id):
        return

    if not context.args or context.args[0] != "tasdiq":
        await update.message.reply_text(
            "⚠️ Bu BARCHA ovozlar va referrallarni o‘chiradi.\n"
            "Tasdiqlash uchun: /resetvotes tasdiq"
        )
        return

    try:
        execute("DELETE FROM votes")
        execute("DELETE FROM referral_done")
        execute(
            "UPDATE users SET main_vote_used = 0, bonus_votes = 0, "
            "bonus_granted = 0, referral_count = 0, points = 0"
        )
        commit()
        await update.message.reply_text("✅ Hammasi nolga tushirildi. Har kimda yana 1 ta ovoz bor.")
    except Exception as e:
        await update.message.reply_text(f"❌ Xato: {e}")


async def newround_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Eski ovozlar reytingda qoladi, hammaga yangi 1 ta asosiy ovoz beriladi."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Siz admin emassiz.")
        return
    try:
        execute("UPDATE users SET main_vote_used = 0")
        commit()
        n = fetchone("SELECT COUNT(*) AS n FROM users WHERE main_vote_used = 0")["n"]
        await update.message.reply_text(f"✅ {n} ta foydalanuvchiga yangi ovoz berildi.")
    except Exception as e:
        await update.message.reply_text(f"❌ Xato: {e}")


async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tekshiruv uchun: o'z holatingizni ko'rsatadi."""
    row = get_user(update.effective_user.id)
    if not row:
        await update.message.reply_text("Bazada topilmadingiz. /start bosing.")
        return
    await update.message.reply_text(
        f"ID: {row['user_id']}\n"
        f"admin: {is_admin(update.effective_user.id)}\n"
        f"subscribed: {row['subscribed']}\n"
        f"instagram_verified: {row['instagram_verified']}\n"
        f"main_vote_used: {row['main_vote_used']}\n"
        f"bonus_votes: {row['bonus_votes']}\n"
        f"konkurs faol: {contest_active()}\n"
        f"qolgan vaqt: {time_left_text()}\n"
        f"baza: {DB_KIND}"
    )


async def timeleft_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⏳ Konkursning qolgan vaqti: {time_left_text()}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ YORDAM\n\n"
        "/start — konkursni boshlash\n"
        "/referral — referral havolangiz\n"
        "/timeleft — konkursning qolgan vaqti\n"
        "/help — yordam\n\n"
        "Muammo bo‘lsa administratorga murojaat qiling."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("BOT ERROR: %r", context.error, exc_info=context.error)
    try:
        db.rollback()
    except Exception:
        pass


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Konkursni boshlash"),
        BotCommand("referral", "Referral havolam"),
        BotCommand("timeleft", "Qolgan vaqt"),
        BotCommand("help", "Yordam"),
    ])


# =========================================================
# MAIN
# =========================================================

def main():
    # Python 3.14 da event loop avtomatik yaratilmaydi.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    for name, handler in [
        ("start", start),
        ("referral", referral_command),
        ("help", help_command),
        ("timeleft", timeleft_command),
        ("myinfo", myinfo_command),
        ("admin", admin_command),
        ("setdays", setdays_command),
        ("restorevotes", restorevotes_command),
        ("extend", extend_command),
        ("restart", restart_command),
        ("resetvotes", resetvotes_command),
        ("newround", newround_command),
        ("cancel", cancel_broadcast),
    ]:
        app.add_handler(CommandHandler(name, handler))

    # Faqat admin broadcast rejimida yuborgan oddiy xabarlarni qabul qiladi.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message))

    for pattern, handler in [
        (r"^check_subscription$", check_subscription),
        (r"^show_candidates$", show_candidates),
        (r"^vote_\d+$", vote_select),
        (r"^confirm_\d+$", vote_confirm),
        (r"^leaderboard$", leaderboard),
        (r"^my_referral$", my_referral),
        (r"^my_stats$", my_stats),
        (r"^back_main$", back_main),
        (r"^admin_panel$", admin_panel),
        (r"^admin_stats$", admin_stats),
        (r"^admin_results$", admin_results),
        (r"^admin_voters$", admin_voters),
        (r"^admin_referrals$", admin_referrals),
        (r"^admin_broadcast$", admin_broadcast),
    ]:
        app.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    app.add_error_handler(error_handler)

    log.info("IMPERIYA KONKURS BOT ISHLAYAPTI | DB=%s | %s kun", DB_KIND, contest_days())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
