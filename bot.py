"""
IMPERIYA — "Imperiyaning eng sodiq xodimi" konkurs boti

Render uchun:
- BOT_TOKEN environment variable
- DATABASE_URL environment variable (Render PostgreSQL Internal Database URL)
"""

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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable topilmadi.")

CHANNEL_USERNAME = "@imperiya_edu"
CHANNEL_LINK = "https://t.me/imperiya_edu"
INSTAGRAM_LINK = "https://www.instagram.com/imperiya_edu/"

ADMIN_ID = 7050215692
DEFAULT_CONTEST_DAYS = int(os.getenv("CONTEST_DAYS", "10"))
REFERRALS_PER_BONUS = 5

PRIZES = [
    ("🥇", "1-o‘rin", "Smart TV"),
    ("🥈", "2-o‘rin", "Tefal"),
    ("🥉", "3-o‘rin", "Choper"),
]

CANDIDATES = [
    "Sardorbek Axmadjonov",
    "Davron Axmedov",
    "Barchinoy Aliboyeva",
    "Muhammadqodir Erkinboyev",
    "Feruza G‘aybullayeva",
    "Nozima Hakimova",
    "Leniyara Menaliyeva",
    "Xurshidabonu Muhammadjanova",
    "Diyorbek Eraliyev",
    "Jahongir Ne'matillayev",
    "Husanboy Ergashev",
    "Sevara Tursunboyeva",
    "Sardorbek O'rmonov",
    "Azimova Feruza",
    "Shohsanam Otamirzayeva",
    "Gulrux Hoshimjonova",
    "Shoxsanam Mirzayeva",
    "Hakima G'aniyeva",
    "Shaxnoza Abdug'aniyeva",
    "Xojiakbar Yo‘ldashev",
    "Diyorbek Abduqahhorov",
    "Sodiqjon Ibragimov",
    "Arofat Abdurahmonova",
]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("imperiya-bot")


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
    # Render PostgreSQL
    db = psycopg2.connect(DATABASE_URL)
    db.autocommit = False
    cursor = db.cursor(cursor_factory=RealDictCursor)
    DB_KIND = "postgres"

log.info("Database: %s", DB_KIND)


def execute(sql, params=()):
    """PostgreSQL va SQLite uchun bitta execute helper."""
    if DB_KIND == "postgres":
        sql = sql.replace("?", "%s")
    cursor.execute(sql, params)
    return cursor


def fetchone(sql, params=()):
    return execute(sql, params).fetchone()


def fetchall(sql, params=()):
    return execute(sql, params).fetchall()


def commit():
    db.commit()


def init_database():
    if DB_KIND == "postgres":
        cursor.execute("""
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
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                vote_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                candidate TEXT NOT NULL,
                vote_type TEXT NOT NULL,
                created_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS referral_done (
                user_id BIGINT PRIMARY KEY,
                created_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Eski PostgreSQL bazalari uchun ustunlarni tekshirish
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS bonus_granted INTEGER DEFAULT 0
        """)
        commit()
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
    if DB_KIND == "postgres":
        execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )
    else:
        execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
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


# =========================================================
# KLAVIATURALAR
# =========================================================

def requirements_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Telegram kanalga obuna bo‘lish", url=CHANNEL_LINK)],
        [InlineKeyboardButton("📸 Instagram sahifaga obuna bo‘lish", url=INSTAGRAM_LINK)],
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
            InlineKeyboardButton(
                f"👤 {CANDIDATES[j]}",
                callback_data=f"vote_{j}"
            )
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
        log.debug("edit xatosi: %s", e)


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
        "1️⃣ Telegram kanalga obuna bo‘ling\n"
        "2️⃣ Instagram sahifamizga obuna bo‘ling\n\n"
        "So‘ng «Obunalarni tekshirish» tugmasini bosing.",
        reply_markup=requirements_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    save_user(query.from_user)

    telegram_ok = False
    try:
        member = await context.bot.get_chat_member(
            chat_id=CHANNEL_USERNAME,
            user_id=user_id
        )
        telegram_ok = member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("Telegram obuna tekshiruvi xatosi: %s", e)

    execute(
        "UPDATE users SET subscribed = ?, instagram_verified = 1 WHERE user_id = ?",
        (1 if telegram_ok else 0, user_id),
    )
    commit()

    if not telegram_ok:
        await edit(
            query,
            "❌ <b>Telegram kanal obunasi tasdiqlanmadi.</b>\n\n"
            "Kanalga obuna bo‘ling va qayta tekshiring.",
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

def votes_available(row) -> int:
    return (0 if row["main_vote_used"] else 1) + row["bonus_votes"]


async def _guard(query) -> bool:
    if not contest_active():
        await edit(query, "⛔ <b>Konkurs yakunlangan.</b>")
        return False
    if not user_has_requirements(query.from_user.id):
        await edit(
            query,
            "❌ Avval majburiy obuna shartlarini bajaring.",
            requirements_keyboard(),
        )
        return False
    return True


async def show_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await _guard(query):
        return

    row = get_user(query.from_user.id)
    left = votes_available(row)

    if left <= 0:
        await edit(
            query,
            "ℹ️ <b>Sizning ovozlaringiz tugagan.</b>\n\n"
            f"Qo‘shimcha ovoz olish uchun do‘stlaringizni taklif qiling: "
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

    if not await _guard(query):
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

    if not await _guard(query):
        return

    user_id = query.from_user.id
    try:
        index = int(query.data.replace("confirm_", ""))
        candidate = CANDIDATES[index]
    except (ValueError, IndexError):
        await query.answer("❌ Nomzod topilmadi.", show_alert=True)
        return

    row = get_user(user_id)

    if row["main_vote_used"] == 0:
        vote_type = "main"
        execute("UPDATE users SET main_vote_used = 1 WHERE user_id = ?", (user_id,))
    elif row["bonus_votes"] > 0:
        vote_type = "bonus"
        execute("UPDATE users SET bonus_votes = bonus_votes - 1 WHERE user_id = ?", (user_id,))
    else:
        await query.answer("❌ Sizda ovoz qolmagan.", show_alert=True)
        return

    execute(
        "INSERT INTO votes (user_id, candidate, vote_type, created_at) VALUES (?, ?, ?, ?)",
        (user_id, candidate, vote_type, datetime.now().isoformat()),
    )
    commit()

    counts = vote_counts()
    rank = next(i for i, (n, _) in enumerate(counts, 1) if n == candidate)
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
# ADMIN
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

    def one(sql):
        return fetchone(sql)[0 if DB_KIND == "sqlite" else "count"] if False else fetchone(sql)

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
# ADMIN BUYRUQLARI
# =========================================================

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
    if not is_admin(update.effective_user.id):
        return
    set_setting("contest_start", datetime.now().isoformat())
    await update.message.reply_text(
        f"🔄 Konkurs hozirdan qayta boshlandi ({contest_days()} kun).\n"
        f"⏱ Tugash: {contest_end().strftime('%d.%m.%Y %H:%M')}"
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
    log.error("BOT ERROR: %r", context.error)


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Konkursni boshlash"),
        BotCommand("referral", "Referral havolam"),
        BotCommand("timeleft", "Qolgan vaqt"),
        BotCommand("help", "Yordam"),
    ])


# =========================================================
# RENDER PORT HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"IMPERIYA BOT OK")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health server portda ishga tushdi: %s", port)
    server.serve_forever()


# =========================================================
# MAIN
# =========================================================

def main():
    # Render Web Service port tekshiruvini o'tkazish uchun.
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    for name, handler in [
        ("start", start),
        ("referral", referral_command),
        ("help", help_command),
        ("timeleft", timeleft_command),
        ("admin", admin_command),
        ("setdays", setdays_command),
        ("extend", extend_command),
        ("restart", restart_command),
    ]:
        app.add_handler(CommandHandler(name, handler))

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
    ]:
        app.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    app.add_error_handler(error_handler)

    log.info("IMPERIYA KONKURS BOT ISHLAYAPTI | DB=%s | %s kun", DB_KIND, contest_days())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
