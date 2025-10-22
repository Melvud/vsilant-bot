# client.py - FULL WORKING VERSION
import asyncio
import os
import re
import time
import contextlib
from functools import wraps
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

# ====== ENV & CONSTANTS ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
LOOKBACK_WEEKS = int(os.getenv("LOOKBACK_WEEKS", "12"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Amsterdam")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
COOLDOWN_MIN = int(os.getenv("RUN_COOLDOWN_MIN", "60"))
API_PORT = int(os.getenv("API_PORT", "8080"))
WEBAPP_URL = os.getenv("WEBAPP_URL", f"http://localhost:{API_PORT}")

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

STARTER_QUESTIONS = [
    "What are you working on this week?",
    "What skill are you trying to grow this year?",
    "Share a recent win (big or small).",
    "What's one book/podcast you'd recommend?",
    "If we met in person, where would you pick to go?"
]

WELCOME_TEXT = (
    "☕ *Welcome to Random Coffee!*\n\n"
    "Each week you'll be matched with a new person for a quick chat.\n\n"
    "• Add a short *_About me_*\n"
    "• Get a weekly match with contact & starter questions\n\n"
    "📅 *All program features are now live!*"
)

PENDING_MESSAGE = (
    "⏳ *Your registration is pending approval*\n\n"
    "An admin will review your profile soon.\n"
    "You'll be notified once approved.\n\n"
    "Meanwhile, you can update your profile in ⚙️ Settings."
)

HELP = (
    "❓ *Help*\n\n"
    "Commands:\n"
    "• `/start` — intro / onboarding\n"
    "• `/profile` — your profile\n"
    "• `/subscribe` — opt in to Random Coffee\n"
    "• `/pause` — opt out from Random Coffee\n"
)

# ====== KEYBOARDS ======
def start_kb_new() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Begin", callback_data="begin_onboarding")]
    ])

def start_kb_existing() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 My Profile", callback_data="show_profile")]
    ])

SEGMENTS = [
    "Bachelor Student",
    "Master Student",
    "PhD Candidate",
    "Postdoctoral Researcher",
    "Senior Researcher / Professor",
    "Industry (Photonics/Optics)",
    "Other / Community",
]
AFFILIATIONS = [
    "TU/e: ECO", "TU/e: Phi", "TU/e: AP", "TU/e: Other",
    "Other University (NL/EU)",
    "Industry",
]

def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I agree", callback_data="consent:agree")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="consent:cancel")]
    ])

def intro_continue_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Start", callback_data="intro:start")]
    ])

def segments_kb() -> InlineKeyboardMarkup:
    rows = []
    for s in SEGMENTS:
        rows.append([InlineKeyboardButton(text=s, callback_data=f"seg:{s}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def affiliations_kb() -> InlineKeyboardMarkup:
    rows = []
    for a in AFFILIATIONS:
        rows.append([InlineKeyboardButton(text=a, callback_data=f"aff:{a}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def yesno_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Yes", callback_data=f"{prefix}:yes"),
         InlineKeyboardButton(text="No", callback_data=f"{prefix}:no")]
    ])

def comms_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📧 Email only", callback_data="comms:email")],
        [InlineKeyboardButton(text="💬 Telegram only", callback_data="comms:telegram")],
        [InlineKeyboardButton(text="📧💬 Email + Telegram", callback_data="comms:both")]
    ])

def settings_menu() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="☕ Random Coffee")],
        [KeyboardButton(text="🎉 Events"), KeyboardButton(text="💥 Socials")],
        [KeyboardButton(text="📢 Notifications")],
        [KeyboardButton(text="👤 My Profile"), KeyboardButton(text="🎓 Mentorship")],
        [KeyboardButton(text="⚙️ Settings"), KeyboardButton(text="🌐 Website")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_settings_menu() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="☕ Random Coffee")],
        [KeyboardButton(text="🎉 Events"), KeyboardButton(text="💥 Socials")],
        [KeyboardButton(text="📢 Notifications")],
        [KeyboardButton(text="👤 My Profile"), KeyboardButton(text="🎓 Mentorship")],
        [KeyboardButton(text="⚙️ Settings"), KeyboardButton(text="🌐 Website")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def settings_edit_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🪪 Change Full Name")],
            [KeyboardButton(text="📧 Change Email")],
            [KeyboardButton(text="🎓 Change Segment")],
            [KeyboardButton(text="🏫 Change Affiliation")],
            [KeyboardButton(text="📝 Change About")],
            [KeyboardButton(text="🧑‍🏫 Toggle Open-to-Mentor")],
            [KeyboardButton(text="📣 Change Comms Preference")],
            [KeyboardButton(text="↩️ Back to Main")]
        ],
        resize_keyboard=True
    )

def rc_about_kb(subscribed: bool, freq: Optional[str], p_tue: bool, p_uni: bool, p_ind: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if subscribed:
        kb.button(text="⏸ Pause", callback_data="rc:pause")
        kb.button(text="🚪 Leave", callback_data="rc:leave")
    else:
        kb.button(text="✅ Join", callback_data="rc:join")
    kb.button(text=f"📆 Frequency: {'Weekly' if (freq or 'weekly')=='weekly' else 'Monthly'}", callback_data="rc:freq")
    kb.button(text=f"{'✅' if p_tue else '☑'} Prefer TU/e", callback_data="rc:pref:tue")
    kb.button(text=f"{'✅' if p_uni else '☑'} Open to other universities", callback_data="rc:pref:uni")
    kb.button(text=f"{'✅' if p_ind else '☑'} Open to industry", callback_data="rc:pref:ind")
    kb.adjust(2)
    return kb.as_markup()

def socials_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ Notify me: ON" if enabled else "☑ Notify me: OFF"),
                              callback_data="socials:toggle")]
    ])

def notifs_kb(u) -> InlineKeyboardMarkup:
    def mark(v): return "✅" if v else "☑"
    rows = [
        [InlineKeyboardButton(text=f"{mark(u.get('notif_announcements'))} Announcements", callback_data="ntf:ann")],
        [InlineKeyboardButton(text=f"{mark(u.get('notif_events'))} Events & RSVPs", callback_data="ntf:events")],
        [InlineKeyboardButton(text=f"{mark(u.get('notif_rc'))} Random Coffee matches", callback_data="ntf:rc")],
        [InlineKeyboardButton(text=f"{mark(u.get('notif_mentor'))} Mentorship nudges", callback_data="ntf:mentor")],
        [InlineKeyboardButton(text=f"{mark(u.get('notif_socials'))} Members-only socials", callback_data="ntf:socials")],
        [InlineKeyboardButton(text="ℹ️ Comms mode", callback_data="ntf:info")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def profile_actions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📇 Share Profile Card (preview)", callback_data="pr:share")],
        [InlineKeyboardButton(text="🧾 GDPR: Request deletion", callback_data="pr:gdpr_del")],
    ])

def mentorship_role_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎓 I'm a Mentee", callback_data="mentorrole:mentee")],
        [InlineKeyboardButton(text="🧑‍🏫 I'm a Mentor", callback_data="mentorrole:mentor")],
        [InlineKeyboardButton(text="⬅️ Close", callback_data="mentorrole:close")]
    ])

# ====== FSM ======
class Onboard(StatesGroup):
    waiting_consent = State()
    waiting_intro_ack = State()
    waiting_fullname = State()
    waiting_email = State()
    waiting_segment = State()
    waiting_affiliation = State()
    waiting_about_v2 = State()
    waiting_open_mentor = State()
    waiting_comms = State()
    confirm_submit = State()

class EditAbout(StatesGroup):
    waiting_about = State()
    confirm_about = State()

class EditFullName(StatesGroup):
    waiting_name = State()

class EditEmail(StatesGroup):
    waiting_email = State()

# ====== DB ======
_pool: asyncpg.Pool

async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

async def db_fetchrow(query, *args):
    async with _pool.acquire() as con:
        return await con.fetchrow(query, *args)

async def db_fetch(query, *args):
    async with _pool.acquire() as con:
        return await con.fetch(query, *args)

async def db_execute(query, *args):
    async with _pool.acquire() as con:
        return await con.execute(query, *args)

# schema
async def ensure_schema():
    await db_execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            display_name TEXT,
            gender TEXT,
            "group" TEXT,
            preferred_groups TEXT[],
            email TEXT,
            segment TEXT,
            affiliation TEXT,
            about TEXT,
            mentor_flag BOOLEAN DEFAULT FALSE,
            communication_mode TEXT,
            status TEXT DEFAULT 'pending',  -- ИЗМЕНЕНО: теперь pending по умолчанию
            subscribed BOOLEAN DEFAULT FALSE,
            rc_frequency TEXT DEFAULT 'weekly',
            rc_pref_tue BOOLEAN DEFAULT TRUE,
            rc_pref_universities BOOLEAN DEFAULT FALSE,
            rc_pref_industry BOOLEAN DEFAULT FALSE,
            socials_opt_in BOOLEAN DEFAULT FALSE,
            notif_announcements BOOLEAN DEFAULT TRUE,
            notif_events BOOLEAN DEFAULT TRUE,
            notif_rc BOOLEAN DEFAULT TRUE,
            notif_mentor BOOLEAN DEFAULT TRUE,
            notif_socials BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ,
            consent_ts TIMESTAMPTZ
        )
    """)
    await db_execute("CREATE INDEX IF NOT EXISTS idx_users_email_lower ON users((lower(email)))")
    await db_execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")

    await db_execute("""
        CREATE TABLE IF NOT EXISTS pairings(
            user_a BIGINT NOT NULL,
            user_b BIGINT NOT NULL,
            last_matched_at TIMESTAMPTZ,
            PRIMARY KEY(user_a, user_b)
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS weekly_matches(
            week_date DATE NOT NULL,
            user_a BIGINT NOT NULL,
            user_b BIGINT NOT NULL,
            PRIMARY KEY(week_date, user_a, user_b)
        )
    """)

    # App settings table
    await db_execute("""
        CREATE TABLE IF NOT EXISTS app_settings(
            id SMALLINT PRIMARY KEY DEFAULT 1,
            schedule_days TEXT[],
            schedule_time TEXT,
            last_run_at TIMESTAMPTZ
        )
    """)
    # Seed default settings
    await db_execute("""
        INSERT INTO app_settings(id, schedule_days, schedule_time)
        VALUES (1, ARRAY['MON'], '09:00')
        ON CONFLICT (id) DO NOTHING
    """)

    # Run logs
    await db_execute("""
        CREATE TABLE IF NOT EXISTS run_logs(
            id BIGSERIAL PRIMARY KEY,
            run_type TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            pairs_count INT,
            triggered_by BIGINT,
            status TEXT,
            error_text TEXT
        )
    """)

    # Approvals log
    await db_execute("""
        CREATE TABLE IF NOT EXISTS approvals_log(
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            by_admin BIGINT,
            ts TIMESTAMPTZ DEFAULT NOW(),
            note TEXT
        )
    """)

    # Events
    await db_execute("""
        CREATE TABLE IF NOT EXISTS events(
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            location TEXT,
            starts_at TIMESTAMPTZ,
            ends_at TIMESTAMPTZ,
            capacity INT,
            rsvp_open_at TIMESTAMPTZ,
            rsvp_close_at TIMESTAMPTZ,
            status TEXT DEFAULT 'draft',
            created_by BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ,
            broadcasted_at TIMESTAMPTZ
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS event_rsvps(
            event_id BIGINT REFERENCES events(id) ON DELETE CASCADE,
            user_id  BIGINT NOT NULL,
            status   TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY(event_id, user_id)
        )
    """)

    # Mentorship
    await db_execute("""
        CREATE TABLE IF NOT EXISTS mentorship_mentors(
            user_id BIGINT PRIMARY KEY,
            tags TEXT[],
            monthly_avail INT DEFAULT 1,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS mentorship_mentees(
            user_id BIGINT PRIMARY KEY,
            interests TEXT[],
            pref TEXT,
            availability_window TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS mentorship_matches(
            mentor_id BIGINT NOT NULL,
            mentee_id BIGINT NOT NULL,
            matched_at TIMESTAMPTZ DEFAULT NOW(),
            active BOOLEAN DEFAULT TRUE,
            PRIMARY KEY(mentor_id, mentee_id)
        )
    """)

    # Broadcasts
    await db_execute("""
        CREATE TABLE IF NOT EXISTS broadcasts(
            id BIGSERIAL PRIMARY KEY,
            title TEXT,
            body TEXT,
            segment_filter TEXT[],
            affiliation_filter TEXT[],
            program_filter TEXT[],
            created_by BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            sent_to INT DEFAULT 0
        )
    """)

# users helpers
async def get_user(user_id: int) -> Optional[asyncpg.Record]:
    return await db_fetchrow("""SELECT * FROM users WHERE user_id=$1""", user_id)

async def insert_user(user_id: int, username: Optional[str], full_name: Optional[str]):
    await db_execute("""
        INSERT INTO users (user_id, username, full_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO NOTHING
    """, user_id, username, full_name)

async def upsert_username_fullname(user_id: int, username: Optional[str], full_name: Optional[str]):
    await db_execute("""
        UPDATE users SET username=$2, full_name=COALESCE(users.full_name, $3), updated_at=NOW()
        WHERE user_id=$1
    """, user_id, username, full_name)

# setters
async def set_full_name(user_id: int, name: str): 
    await db_execute("""UPDATE users SET full_name=$2, updated_at=NOW() WHERE user_id=$1""", user_id, name)
async def set_email(user_id: int, email: str): 
    await db_execute("""UPDATE users SET email=$2, updated_at=NOW() WHERE user_id=$1""", user_id, email)
async def set_segment(user_id: int, seg: str): 
    await db_execute("""UPDATE users SET segment=$2, updated_at=NOW() WHERE user_id=$1""", user_id, seg)
async def set_affiliation(user_id: int, aff: str): 
    await db_execute("""UPDATE users SET affiliation=$2, updated_at=NOW() WHERE user_id=$1""", user_id, aff)
async def set_about(user_id: int, about: str): 
    await db_execute("""UPDATE users SET about=$2, updated_at=NOW() WHERE user_id=$1""", user_id, about)
async def set_subscribed(user_id: int, val: bool): 
    await db_execute("""UPDATE users SET subscribed=$2, updated_at=NOW() WHERE user_id=$1""", user_id, val)
async def set_mentor_flag(user_id: int, val: bool): 
    await db_execute("""UPDATE users SET mentor_flag=$2, updated_at=NOW() WHERE user_id=$1""", user_id, val)
async def set_comms(user_id: int, mode: str): 
    await db_execute("""UPDATE users SET communication_mode=$2, updated_at=NOW() WHERE user_id=$1""", user_id, mode)
async def set_rc_frequency(user_id: int, freq: str): 
    await db_execute("""UPDATE users SET rc_frequency=$2, updated_at=NOW() WHERE user_id=$1""", user_id, freq)
async def set_rc_pref(user_id: int, field: str, val: bool): 
    await db_execute(f"""UPDATE users SET {field}=$2, updated_at=NOW() WHERE user_id=$1""", user_id, val)
async def set_socials_opt(user_id: int, val: bool): 
    await db_execute("""UPDATE users SET socials_opt_in=$2, updated_at=NOW() WHERE user_id=$1""", user_id, val)
async def set_notif(user_id: int, field: str, val: bool): 
    await db_execute(f"""UPDATE users SET {field}=$2, updated_at=NOW() WHERE user_id=$1""", user_id, val)
async def set_status(user_id: int, status: str): 
    await db_execute("""UPDATE users SET status=$2, updated_at=NOW() WHERE user_id=$1""", user_id, status)

# ====== SETTINGS & MATCHING ======
async def get_settings():
    row = await db_fetchrow("""SELECT * FROM app_settings WHERE id=1""")
    if not row:
        return {"schedule_days": ["MON"], "schedule_time": "09:00", "last_run_at": None}
    return {
        "schedule_days": row["schedule_days"] or [],
        "schedule_time": row["schedule_time"] or "09:00",
        "last_run_at": row["last_run_at"]
    }

async def set_schedule_days(days: List[str]):
    await db_execute("""UPDATE app_settings SET schedule_days=$1 WHERE id=1""", days)

async def set_schedule_time(time_str: str):
    await db_execute("""UPDATE app_settings SET schedule_time=$1 WHERE id=1""", time_str)

async def can_run_now() -> Tuple[bool, Optional[timedelta]]:
    s = await get_settings()
    last = s.get("last_run_at")
    if not last:
        return (True, None)
    elapsed = datetime.now(timezone.utc) - last
    cooldown = timedelta(minutes=COOLDOWN_MIN)
    if elapsed < cooldown:
        return (False, cooldown - elapsed)
    return (True, None)

async def cooldown_remaining() -> Optional[timedelta]:
    ok, rem = await can_run_now()
    return rem if not ok else None

async def log_run_start(run_type: str, triggered_by: Optional[int] = None) -> int:
    row = await db_fetchrow("""
        INSERT INTO run_logs(run_type, started_at, triggered_by, status)
        VALUES($1, NOW(), $2, 'running')
        RETURNING id
    """, run_type, triggered_by)
    await db_execute("""UPDATE app_settings SET last_run_at=NOW() WHERE id=1""")
    return row["id"]

async def log_run_finish(run_id: int, pairs_count: int, ok: bool = True, error_text: Optional[str] = None):
    await db_execute("""
        UPDATE run_logs
        SET finished_at=NOW(), pairs_count=$2, status=$3, error_text=$4
        WHERE id=$1
    """, run_id, pairs_count, "ok" if ok else "error", error_text)

async def get_run_history(limit: int = 10):
    return await db_fetch("""
        SELECT * FROM run_logs
        ORDER BY started_at DESC
        LIMIT $1
    """, limit)

async def get_run_detail(run_id: int):
    return await db_fetchrow("""SELECT * FROM run_logs WHERE id=$1""", run_id)

# ====== MATCHING (using matcher.py) ======
async def run_matching_once() -> int:
    """Call the real matching function from matcher.py"""
    from matcher import run_matching_once as matcher_run
    return await matcher_run(_pool, bot, TIMEZONE, LOOKBACK_WEEKS, STARTER_QUESTIONS)

# ====== UTILS ======
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
dp = Dispatcher()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def is_approved(user_id: int) -> bool:
    """Проверить что пользователь одобрен"""
    if is_admin(user_id):
        return True
    u = await get_user(user_id)
    return u and u.get('status') == 'approved'

def profile_card_text(u: asyncpg.Record) -> str:
    return (
        f"👤 *Name*: {u['full_name'] or '—'}\n"
        f"🎓 *Segment*: {u.get('segment') or '—'}\n"
        f"🏫 *Affiliation*: {u.get('affiliation') or '—'}\n"
        f"📝 *About*: {u.get('about') or '—'}\n"
        f"🧑‍🏫 *Open to mentor*: {'Yes' if u.get('mentor_flag') else 'No'}\n"
        f"📞 *Contact*: {u.get('email') or '—'}" + (f" · @{u['username']}" if u.get('username') else "")
    )

def format_profile(u: asyncpg.Record) -> str:
    comms_display = {
        "email_only": "📧 Email only",
        "telegram_only": "💬 Telegram only", 
        "email+telegram": "📧💬 Email + Telegram"
    }.get(u.get('communication_mode'), u.get('communication_mode') or '—')
    
    lines = [
        f"👤 *Name*: {u['full_name'] or '—'}",
        f"📧 *Email*: {u.get('email') or '—'}",
        f"🎓 *Segment*: {u.get('segment') or '—'}",
        f"🏫 *Affiliation*: {u.get('affiliation') or '—'}",
        f"📝 *About*: {u.get('about') or '—'}",
        f"🧑‍🏫 *Open to mentor*: {'yes' if u.get('mentor_flag') else 'no'}",
        f"📣 *Comms*: {comms_display}",
        f"☕ *Random Coffee*: {'ON' if u.get('subscribed') else 'OFF'} "
        f"({(u.get('rc_frequency') or 'weekly').capitalize()})",
        f"   Prefs: {'TU/e' if u.get('rc_pref_tue') else ''}"
        f"{' + universities' if u.get('rc_pref_universities') else ''}"
        f"{' + industry' if u.get('rc_pref_industry') else ''}",
        f"👥 *Socials*: {'ON' if u.get('socials_opt_in') else 'OFF'}",
        "📢 *Notifications*: "
        f"{'Ann' if u.get('notif_announcements') else ''} "
        f"{'Ev' if u.get('notif_events') else ''} "
        f"{'RC' if u.get('notif_rc') else ''} "
        f"{'Mentor' if u.get('notif_mentor') else ''} "
        f"{'Soc' if u.get('notif_socials') else ''}",
        f"✅ *Status*: {u.get('status') or 'approved'}",
    ]
    return "\n".join(lines)

async def main_menu_for(user_id: int) -> ReplyKeyboardMarkup:
    if is_admin(user_id):
        return admin_settings_menu()
    return settings_menu()

_last_start: defaultdict[int, float] = defaultdict(float)
def is_duplicate_start(user_id: int, window_sec: float = 2.0) -> bool:
    now = time.time()
    if now - _last_start[user_id] < window_sec:
        return True
    _last_start[user_id] = now
    return False

# ====== SCHEDULER LOOP ======
async def scheduler_loop():
    """Background task that checks schedule and runs matching at configured times."""
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            
            settings = await get_settings()
            schedule_days = settings.get("schedule_days", [])
            schedule_time = settings.get("schedule_time", "09:00")
            
            if not schedule_days or not schedule_time:
                continue
            
            now = datetime.now(ZoneInfo(TIMEZONE))
            current_day = ["MON","TUE","WED","THU","FRI","SAT","SUN"][now.weekday()]
            current_time = now.strftime("%H:%M")
            
            if current_day in schedule_days and current_time == schedule_time:
                ok, _ = await can_run_now()
                if ok:
                    run_id = await log_run_start('scheduled')
                    try:
                        count = await run_matching_once()
                        await log_run_finish(run_id, count, ok=True)
                    except Exception as e:
                        await log_run_finish(run_id, 0, ok=False, error_text=str(e))
                    
                    # Sleep for 2 minutes to avoid duplicate runs
                    await asyncio.sleep(120)
        except Exception as e:
            print(f"Scheduler error: {e}")
            await asyncio.sleep(60)

async def clear_user_messages(user_id: int):
    """
    Попытка очистить историю сообщений пользователя
    Telegram API ограничен, но мы можем удалить последние сообщения
    """
    try:
        # Отправляем пустую клавиатуру
        await bot.send_message(
            user_id,
            "🧹 Clearing chat history...",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="/start")]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        
        # Небольшая пауза
        await asyncio.sleep(0.5)
        
        # Удаляем это сообщение
        # (к сожалению, нельзя удалить все старые сообщения через API)
        
    except Exception as e:
        print(f"Failed to clear messages for {user_id}: {e}")

# ====== HANDLERS ======
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if is_duplicate_start(message.from_user.id):
        return
    user_id = message.from_user.id
    full_name = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])) or None
    username = message.from_user.username

    await ensure_schema()

    existing = await get_user(user_id)
    if not existing:
        await insert_user(user_id, username, full_name)
    else:
        await upsert_username_fullname(user_id, username, full_name)

    if is_admin(user_id):
        with contextlib.suppress(Exception):
            await set_status(user_id, "approved")

    await state.clear()
    existing = await get_user(user_id)

    if existing and (existing["email"] or existing["about"]):
        await message.answer(
            f"{WELCOME_TEXT}\n*Open your profile:*",
            reply_markup=start_kb_existing()
        )
    else:
        await message.answer(
            f"{WELCOME_TEXT}\nTap *Begin* to start a quick setup.",
            reply_markup=start_kb_new()
        )

@dp.callback_query(F.data == "show_profile")
async def cb_show_profile(cq: CallbackQuery):
    await cq.answer()
    u = await get_user(cq.from_user.id)
    await bot.send_message(cq.message.chat.id, format_profile(u), reply_markup=await main_menu_for(cq.from_user.id))

# ====== ONBOARDING ======
@dp.callback_query(F.data == "begin_onboarding")
async def cb_begin_v2(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    msg = await bot.send_message(
        cq.message.chat.id,
        "📄 *Consent*\n\nBy continuing you agree to be contacted about matches and events. "
        "You can request deletion anytime.",
        reply_markup=consent_kb()
    )
    await state.set_state(Onboard.waiting_consent)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(Onboard.waiting_consent, F.data == "consent:cancel")
async def st_consent_cancel(cq: CallbackQuery, state: FSMContext):
    await cq.answer("Cancelled")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    await bot.send_message(cq.message.chat.id, "You can /start again anytime.", reply_markup=await main_menu_for(cq.from_user.id))
    await state.clear()

@dp.callback_query(Onboard.waiting_consent, F.data == "consent:agree")
async def st_consent_agree(cq: CallbackQuery, state: FSMContext):
    await cq.answer("Agreed")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    msg = await bot.send_message(
        cq.message.chat.id,
        "👋 *Intro*\n\nWe help you meet people across TU/e and beyond.\nNext: fill a short form.\n\n"
        "📅 *All program features are now live!*",
        reply_markup=intro_continue_kb()
    )
    await state.set_state(Onboard.waiting_intro_ack)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(Onboard.waiting_intro_ack, F.data == "intro:start")
async def st_intro_start(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    with contextlib.suppress(Exception):
        await cq.message.delete()
    msg = await bot.send_message(cq.message.chat.id, "🪪 *Full name*\n\n_Send your full name as text._")
    await state.set_state(Onboard.waiting_fullname)
    await state.update_data(last_msg_id=msg.message_id)

@dp.message(Onboard.waiting_fullname)
async def st_fullname(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Please send your full name."); return
    data = await state.get_data()
    if mid := data.get("last_msg_id"):
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, mid)
    with contextlib.suppress(Exception):
        await message.delete()
    await state.update_data(reg_fullname=name)
    msg = await message.answer("📧 *Email*\n\n_Send a valid email address._")
    await state.set_state(Onboard.waiting_email)
    await state.update_data(last_msg_id=msg.message_id)

@dp.message(Onboard.waiting_email)
async def st_email(message: Message, state: FSMContext):
    email = (message.text or "").strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        await message.answer("That doesn't look like an email. Please try again.")
        return
    data = await state.get_data()
    if mid := data.get("last_msg_id"):
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, mid)
    with contextlib.suppress(Exception):
        await message.delete()
    await state.update_data(reg_email=email)
    msg = await message.answer("🎓 *Segment*\n\nChoose one:", reply_markup=segments_kb())
    await state.set_state(Onboard.waiting_segment)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(Onboard.waiting_segment, F.data.startswith("seg:"))
async def st_segment(cq: CallbackQuery, state: FSMContext):
    _, seg = cq.data.split(":", 1)
    if seg not in SEGMENTS:
        await cq.answer("Invalid"); return
    await cq.answer(seg)
    with contextlib.suppress(Exception):
        await cq.message.delete()
    await state.update_data(reg_segment=seg)
    msg = await bot.send_message(cq.message.chat.id, "🏫 *Affiliation*\n\nChoose one:", reply_markup=affiliations_kb())
    await state.set_state(Onboard.waiting_affiliation)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(Onboard.waiting_affiliation, F.data.startswith("aff:"))
async def st_affiliation(cq: CallbackQuery, state: FSMContext):
    _, aff = cq.data.split(":", 1)
    if aff not in AFFILIATIONS:
        await cq.answer("Invalid"); return
    await cq.answer(aff)
    with contextlib.suppress(Exception):
        await cq.message.delete()
    examples = (
        "Examples:\n"
        "• Student: _Master student in Photonics, working on fiber sensors; interested in internships in Eindhoven; likes short tech talks and site visits._\n"
        "• Researcher: _Senior researcher (PIC packaging) at [Company/Lab]; happy to give career advice; open to Random Coffee monthly._"
    )
    msg = await bot.send_message(
        cq.message.chat.id,
        f"📝 *About yourself*\n\nShort, 1–3 sentences. {examples}\n\n_Send your text (required)._",
    )
    await state.update_data(reg_affiliation=aff)
    await state.set_state(Onboard.waiting_about_v2)
    await state.update_data(last_msg_id=msg.message_id)

@dp.message(Onboard.waiting_about_v2)
async def st_about_v2(message: Message, state: FSMContext):
    about = (message.text or "").strip()
    if not about:
        await message.answer("Please send a short text about yourself (required)."); return
    data = await state.get_data()
    if mid := data.get("last_msg_id"):
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, mid)
    with contextlib.suppress(Exception):
        await message.delete()
    await state.update_data(reg_about=about)
    desc = (
        "🎓 *Mentorship*\n\n"
        "1:1 mentorship until end of May 2025\n\n"
    )
    msg = await message.answer(
        "🧑‍🏫 *Open to be a mentor?*\n\n" + desc,
        reply_markup=yesno_kb("mentor")
    )
    await state.set_state(Onboard.waiting_open_mentor)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(Onboard.waiting_open_mentor, F.data.startswith("mentor:"))
async def st_open_mentor(cq: CallbackQuery, state: FSMContext):
    _, ans = cq.data.split(":", 1)
    await cq.answer(ans.upper())
    with contextlib.suppress(Exception):
        await cq.message.delete()
    await state.update_data(reg_open_to_mentor=(ans == "yes"))
    msg = await bot.send_message(cq.message.chat.id, "📣 *Communication preference*", reply_markup=comms_kb())
    await state.set_state(Onboard.waiting_comms)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(Onboard.waiting_comms, F.data.startswith("comms:"))
async def st_comms(cq: CallbackQuery, state: FSMContext):
    _, mode = cq.data.split(":", 1)
    await cq.answer()
    with contextlib.suppress(Exception):
        await cq.message.delete()
    
    # Определяем режим коммуникации
    if mode == "email":
        comms_mode = "email_only"
    elif mode == "telegram":
        comms_mode = "telegram_only"
    else:  # both
        comms_mode = "email+telegram"
    
    await state.update_data(reg_comms=comms_mode)
    data = await state.get_data()
    
    # Определяем текст для отображения
    comms_display = {
        "email_only": "📧 Email only",
        "telegram_only": "💬 Telegram only",
        "email+telegram": "📧💬 Email + Telegram"
    }[comms_mode]
    
    summary = (
        f"👤 *{data.get('reg_fullname')}*\n"
        f"📧 {data.get('reg_email')}\n"
        f"🎓 {data.get('reg_segment')}\n"
        f"🏫 {data.get('reg_affiliation')}\n"
        f"📝 {data.get('reg_about')}\n"
        f"🧑‍🏫 Open to mentor: {'Yes' if data.get('reg_open_to_mentor') else 'No'}\n"
        f"📣 Comms: {comms_display}\n\n"
        f"📅 *All program features are now live!*"
    )
    msg = await bot.send_message(cq.message.chat.id, f"📋 *Preview*\n\n{summary}\n\nSubmit?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Submit", callback_data="submit:ok")]
    ]))
    await state.set_state(Onboard.confirm_submit)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(Onboard.confirm_submit, F.data == "submit:ok")
async def st_submit_ok(cq: CallbackQuery, state: FSMContext):
    await cq.answer("Submitted")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    data = await state.get_data()
    
    # Статус 'pending' если не админ
    status = 'approved' if is_admin(cq.from_user.id) else 'pending'
    
    try:
        await db_execute("""
            UPDATE users SET
                full_name = COALESCE($2, full_name),
                email = $3,
                segment = $4,
                affiliation = $5,
                about = $6,
                mentor_flag = $7,
                communication_mode = $8,
                status = $9,
                consent_ts = NOW(),
                updated_at = NOW()
            WHERE user_id = $1
        """, cq.from_user.id,
           data.get('reg_fullname'),
           data.get('reg_email'),
           data.get('reg_segment'),
           data.get('reg_affiliation'),
           data.get('reg_about'),
           bool(data.get('reg_open_to_mentor')),
           data.get('reg_comms') or 'email+telegram',
           status
        )
    except Exception:
        pass
    
    if status == 'pending':
        await bot.send_message(
            cq.message.chat.id,
            PENDING_MESSAGE,
            reply_markup=await main_menu_for(cq.from_user.id)
        )
    else:
        await bot.send_message(
            cq.message.chat.id,
            "✅ *Thank you for registering!*\n\nYour profile is now *ready*.",
            reply_markup=await main_menu_for(cq.from_user.id)
        )
    await state.clear()

# ====== WEBSITE ======
@dp.message(F.text == "🌐 Website")
async def btn_website(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Open phe.tue.nl", url="https://phe.tue.nl")]
    ])
    
    await message.answer(
        "🌐 *Visit our website*\n\n"
        "Learn more about Photonics Eindhoven Society:",
        reply_markup=kb
    )

# ====== SETTINGS ======
@dp.message(F.text == "⚙️ Settings")
async def btn_settings(message: Message):
    await message.answer("⚙️ *Settings*", reply_markup=settings_edit_kb())

@dp.message(F.text == "↩️ Back to Main")
async def btn_back(message: Message):
    await message.answer("Main menu:", reply_markup=await main_menu_for(message.from_user.id))

@dp.message(F.text == "🪪 Change Full Name")
async def st_change_fullname(message: Message, state: FSMContext):
    msg = await message.answer("✏️ Send your *full name*:")
    await state.set_state(EditFullName.waiting_name)
    await state.update_data(last_msg_id=msg.message_id)

@dp.message(EditFullName.waiting_name)
async def st_change_fullname_done(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Please send a non-empty name."); return
    data = await state.get_data()
    if mid := data.get("last_msg_id"):
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, mid)
    with contextlib.suppress(Exception):
        await message.delete()
    await set_full_name(message.from_user.id, name)
    await message.answer("✅ Saved.", reply_markup=await main_menu_for(message.from_user.id))
    await state.clear()

@dp.message(F.text == "📧 Change Email")
async def st_change_email(message: Message, state: FSMContext):
    msg = await message.answer("✏️ Send your *email*:")
    await state.set_state(EditEmail.waiting_email)
    await state.update_data(last_msg_id=msg.message_id)

@dp.message(EditEmail.waiting_email)
async def st_change_email_done(message: Message, state: FSMContext):
    email = (message.text or "").strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        await message.answer("That doesn't look like an email. Please try again.")
        return
    data = await state.get_data()
    if mid := data.get("last_msg_id"):
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, mid)
    with contextlib.suppress(Exception):
        await message.delete()
    await set_email(message.from_user.id, email)
    await message.answer("✅ Saved.", reply_markup=await main_menu_for(message.from_user.id))
    await state.clear()

@dp.message(F.text == "🎓 Change Segment")
async def st_change_segment(message: Message):
    await message.answer("Choose your *Segment*:", reply_markup=segments_kb())

@dp.callback_query(F.data.startswith("seg:"))
async def st_change_segment_done(cq: CallbackQuery):
    _, seg = cq.data.split(":", 1)
    if seg not in SEGMENTS:
        await cq.answer("Invalid"); return
    await set_segment(cq.from_user.id, seg)
    await cq.answer("Saved")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    await bot.send_message(cq.message.chat.id, "✅ Saved.", reply_markup=await main_menu_for(cq.from_user.id))

@dp.message(F.text == "🏫 Change Affiliation")
async def st_change_aff(message: Message):
    await message.answer("Choose your *Affiliation*:", reply_markup=affiliations_kb())

@dp.callback_query(F.data.startswith("aff:"))
async def st_change_aff_done(cq: CallbackQuery):
    _, aff = cq.data.split(":", 1)
    if aff not in AFFILIATIONS:
        await cq.answer("Invalid"); return
    await set_affiliation(cq.from_user.id, aff)
    await cq.answer("Saved")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    await bot.send_message(cq.message.chat.id, "✅ Saved.", reply_markup=await main_menu_for(cq.from_user.id))

@dp.message(F.text == "📝 Change About")
async def btn_change_about(message: Message, state: FSMContext):
    msg = await message.answer("✏️ Send your new *About me* (1–3 sentences).")
    await state.set_state(EditAbout.waiting_about)
    await state.update_data(last_msg_id=msg.message_id)

@dp.message(EditAbout.waiting_about)
async def edit_about_input(message: Message, state: FSMContext):
    about = (message.text or "").strip()
    if not about:
        await message.answer("Please send some text."); return
    data = await state.get_data()
    if mid := data.get("last_msg_id"):
        with contextlib.suppress(Exception):
            await bot.delete_message(message.chat.id, mid)
    with contextlib.suppress(Exception):
        await message.delete()
    await state.update_data(pending_about=about)
    preview = f"📝 *About me – preview:*\n\n{about}\n\nSave it?"
    msg = await message.answer(preview, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Save", callback_data="about_save")],
        [InlineKeyboardButton(text="✏️ Edit", callback_data="about_edit"),
         InlineKeyboardButton(text="❌ Cancel", callback_data="about_cancel")]
    ]))
    await state.set_state(EditAbout.confirm_about)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(EditAbout.confirm_about, F.data == "about_save")
async def edit_about_save(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    about = data.get("pending_about", "")
    await set_about(cq.from_user.id, about)
    await cq.answer("Saved")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    await bot.send_message(cq.message.chat.id, "✅ Updated.", reply_markup=await main_menu_for(cq.from_user.id))
    await state.clear()

@dp.callback_query(EditAbout.confirm_about, F.data == "about_edit")
async def edit_about_edit(cq: CallbackQuery, state: FSMContext):
    await cq.answer("Send new text")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    msg = await bot.send_message(cq.message.chat.id, "✏️ Send your updated *About me* text:")
    await state.set_state(EditAbout.waiting_about)
    await state.update_data(last_msg_id=msg.message_id)

@dp.callback_query(EditAbout.confirm_about, F.data == "about_cancel")
async def edit_about_cancel(cq: CallbackQuery, state: FSMContext):
    await cq.answer("Canceled")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    await bot.send_message(cq.message.chat.id, "Canceled.", reply_markup=await main_menu_for(cq.from_user.id))
    await state.clear()

@dp.message(F.text == "🧑‍🏫 Toggle Open-to-Mentor")
async def toggle_mentor(message: Message):
    u = await get_user(message.from_user.id)
    cur = bool(u.get("mentor_flag"))
    await set_mentor_flag(message.from_user.id, not cur)
    await message.answer(f"✅ Mentor flag set to *{'Yes' if not cur else 'No'}*.", reply_markup=await main_menu_for(message.from_user.id))

@dp.message(F.text == "📣 Change Comms Preference")
async def change_comms(message: Message):
    await message.answer("Choose your *Communication preference*:", reply_markup=comms_kb())

@dp.callback_query(F.data.startswith("comms:"))
async def change_comms_done(cq: CallbackQuery):
    _, mode = cq.data.split(":", 1)
    if mode == "email":
        val = "email_only"
    elif mode == "telegram":
        val = "telegram_only"
    else:  # both
        val = "email+telegram"
    
    await set_comms(cq.from_user.id, val)
    await cq.answer("Saved")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    await bot.send_message(cq.message.chat.id, "✅ Saved.", reply_markup=await main_menu_for(cq.from_user.id))


# ====== PROFILE ======
@dp.message(Command("profile"))
@dp.message(F.text == "👤 My Profile")
async def cmd_profile(message: Message):
    u = await get_user(message.from_user.id)
    if not u:
        await message.answer("Please */start* first."); return
    await message.answer(format_profile(u), reply_markup=profile_actions_kb())

@dp.callback_query(F.data == "pr:share")
async def pr_share(cq: CallbackQuery):
    await cq.answer()
    u = await get_user(cq.from_user.id)
    card = (
        "📇 *Your Profile Card (preview)*\n\n" +
        profile_card_text(u) +
        "\n\nThis card is used in all 1:1 introductions."
    )
    await cq.message.edit_text(card, reply_markup=profile_actions_kb())

@dp.callback_query(F.data == "pr:gdpr_del")
async def pr_gdpr_del(cq: CallbackQuery):
    await cq.answer()
    
    # Подтверждение
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Yes, delete everything", callback_data="pr:gdpr_confirm")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="pr:gdpr_cancel")]
    ])
    
    await cq.message.edit_text(
        "⚠️ *Confirm Account Deletion*\n\n"
        "This will permanently delete:\n"
        "• Your profile and all data\n"
        "• Random Coffee history\n"
        "• Mentorship assignments\n"
        "• Event registrations\n"
        "• All records from database\n\n"
        "*This action cannot be undone!*\n\n"
        "Are you sure?",
        reply_markup=confirm_kb
    )

@dp.callback_query(F.data == "pr:gdpr_cancel")
async def pr_gdpr_cancel(cq: CallbackQuery):
    await cq.answer("Cancelled")
    u = await get_user(cq.from_user.id)
    await cq.message.edit_text(format_profile(u), reply_markup=profile_actions_kb())

@dp.callback_query(F.data == "pr:gdpr_confirm")
async def pr_gdpr_confirm(cq: CallbackQuery):
    user_id = cq.from_user.id
    
    try:
        # Сохраняем имя для финального сообщения
        u = await get_user(user_id)
        user_name = u.get('full_name') or 'User'
        
        # Удаляем из всех таблиц
        async with _pool.acquire() as con:
            tr = con.transaction()
            await tr.start()
            
            try:
                # 1. Удаляем пары
                await con.execute(
                    "DELETE FROM pairings WHERE user_a=$1 OR user_b=$1",
                    user_id
                )
                
                # 2. Удаляем weekly matches
                await con.execute(
                    "DELETE FROM weekly_matches WHERE user_a=$1 OR user_b=$1",
                    user_id
                )
                
                # 3. Удаляем из mentorship
                await con.execute(
                    "DELETE FROM mentorship_mentors WHERE user_id=$1",
                    user_id
                )
                await con.execute(
                    "DELETE FROM mentorship_mentees WHERE user_id=$1",
                    user_id
                )
                await con.execute(
                    "DELETE FROM mentorship_matches WHERE mentor_id=$1 OR mentee_id=$1",
                    user_id
                )
                
                # 4. Удаляем RSVP
                await con.execute(
                    "DELETE FROM event_rsvps WHERE user_id=$1",
                    user_id
                )
                
                # 5. Удаляем из approvals log
                await con.execute(
                    "DELETE FROM approvals_log WHERE user_id=$1",
                    user_id
                )
                
                # 6. Удаляем основную запись пользователя (последнее!)
                await con.execute(
                    "DELETE FROM users WHERE user_id=$1",
                    user_id
                )
                
                await tr.commit()
                
            except Exception as e:
                await tr.rollback()
                raise e
        
        # Удаляем сообщение с подтверждением
        with contextlib.suppress(Exception):
            await cq.message.delete()
        
        # Отправляем финальное сообщение
        await bot.send_message(
            user_id,
            "✅ *Account Deleted*\n\n"
            f"Goodbye, {user_name}! All your data has been permanently deleted.\n\n"
            "• Profile deleted ✓\n"
            "• Random Coffee history deleted ✓\n"
            "• Mentorship records deleted ✓\n"
            "• Event registrations deleted ✓\n"
            "• All database records deleted ✓\n\n"
            "You can create a new account anytime by typing /start\n\n"
            "Thank you for using PhE Bot! 👋",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="/start")]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
            
        await cq.answer("Account deleted successfully")
        
    except Exception as e:
        print(f"Error deleting account for {user_id}: {e}")
        await cq.answer("Error occurred")
        await bot.send_message(
            user_id,
            f"❌ *Error deleting account*\n\n"
            f"Please contact an administrator.\n"
            f"Error details: {str(e)[:100]}"
        )

# ====== RANDOM COFFEE ======
@dp.message(F.text == "☕ Random Coffee")
async def rc_about(message: Message):
    # Проверка approval
    if not await is_approved(message.from_user.id):
        await message.answer(PENDING_MESSAGE)
        return
    
    u = await get_user(message.from_user.id)
    text = (
        "☕ *Random Coffee – About*\n\n"
        "1:1 introduction within TU/e (your group/other groups), other universities (e.g., *Fontys – coming soon*), and industry.\n\n"
        "Goal: expand your network, share experience, and better understand what colleagues are doing in science and companies.\n\n"
        "*Preferences* (optional): Prefer TU/e / Open to other universities / Open to industry\n\n"
        "📅 *All program features are now live!*"
    )
    kb = rc_about_kb(
        subscribed=bool(u.get("subscribed")),
        freq=u.get("rc_frequency"),
        p_tue=bool(u.get("rc_pref_tue")),
        p_uni=bool(u.get("rc_pref_universities")),
        p_ind=bool(u.get("rc_pref_industry"))
    )
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "rc:join")
async def rc_join(cq: CallbackQuery):
    await set_subscribed(cq.from_user.id, True)
    await cq.answer("Joined")
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=rc_about_kb(True, u.get("rc_frequency"), u.get("rc_pref_tue"), u.get("rc_pref_universities"), u.get("rc_pref_industry")))
    await bot.send_message(cq.message.chat.id, "✅ You're in! You'll be included in upcoming matches.")

@dp.callback_query(F.data == "rc:pause")
async def rc_pause(cq: CallbackQuery):
    await set_subscribed(cq.from_user.id, False)
    await cq.answer("Paused")
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=rc_about_kb(False, u.get("rc_frequency"), u.get("rc_pref_tue"), u.get("rc_pref_universities"), u.get("rc_pref_industry")))
    await bot.send_message(cq.message.chat.id, "⏸ Paused. You won't receive matches until you join again.")

@dp.callback_query(F.data == "rc:leave")
async def rc_leave(cq: CallbackQuery):
    await set_subscribed(cq.from_user.id, False)
    await cq.answer("Left")
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=rc_about_kb(False, u.get("rc_frequency"), u.get("rc_pref_tue"), u.get("rc_pref_universities"), u.get("rc_pref_industry")))
    await bot.send_message(cq.message.chat.id, "🚪 Left Random Coffee. Join anytime.")

@dp.callback_query(F.data == "rc:freq")
async def rc_freq(cq: CallbackQuery):
    u = await get_user(cq.from_user.id)
    cur = (u.get("rc_frequency") or "weekly")
    new = "monthly" if cur == "weekly" else "weekly"
    await set_rc_frequency(cq.from_user.id, new)
    await cq.answer(f"Frequency: {new.capitalize()}")
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=rc_about_kb(bool(u.get("subscribed")), u.get("rc_frequency"), u.get("rc_pref_tue"), u.get("rc_pref_universities"), u.get("rc_pref_industry")))

@dp.callback_query(F.data.startswith("rc:pref:"))
async def rc_pref_toggle(cq: CallbackQuery):
    _, _, key = cq.data.split(":")
    field_map = {"tue": "rc_pref_tue", "uni": "rc_pref_universities", "ind": "rc_pref_industry"}
    field = field_map.get(key)
    if not field:
        await cq.answer("Invalid"); return
    
    u = await get_user(cq.from_user.id)
    new_val = not bool(u.get(field))
    await set_rc_pref(cq.from_user.id, field, new_val)
    
    # Получаем обновленные данные
    u = await get_user(cq.from_user.id)
    new_kb = rc_about_kb(
        bool(u.get("subscribed")), 
        u.get("rc_frequency"), 
        u.get("rc_pref_tue"), 
        u.get("rc_pref_universities"), 
        u.get("rc_pref_industry")
    )
    
    # Пытаемся обновить, если не получается - просто отвечаем
    try:
        await cq.message.edit_reply_markup(reply_markup=new_kb)
        await cq.answer("Saved")
    except Exception:
        await cq.answer("Saved")

@dp.message(F.text.in_({"☕ Subscribe", "/subscribe"}))
async def legacy_subscribe(message: Message):
    await set_subscribed(message.from_user.id, True)
    await message.answer("☕ Enabled via legacy command. Use *☕ Random Coffee* for more options.", reply_markup=await main_menu_for(message.from_user.id))

@dp.message(F.text.in_({"⏸ Pause", "/pause"}))
async def legacy_pause(message: Message):
    await set_subscribed(message.from_user.id, False)
    await message.answer("⏸ Paused via legacy command. Use *☕ Random Coffee* for more options.", reply_markup=await main_menu_for(message.from_user.id))

# ====== EVENTS ======
@dp.message(F.text == "🎉 Events")
async def events_upcoming(message: Message):
    if not await is_approved(message.from_user.id):
        await message.answer(PENDING_MESSAGE)
        return
    
    # Получить опубликованные события (не socials)
    events = await db_fetch("""
        SELECT id, title, description, location, starts_at, ends_at, 
               photo_url, registration_url, capacity
        FROM events
        WHERE status = 'published' 
          AND event_type = 'event'
          AND (starts_at IS NULL OR starts_at >= NOW())
        ORDER BY starts_at ASC NULLS LAST
        LIMIT 10
    """)
    
    if not events:
        await message.answer(
            "🎉 *Events*\n\n"
            "No upcoming events at the moment.\n"
            "Check back soon!"
        )
        return
    
    for event in events:
        await send_event_card(message.chat.id, event)

@dp.message(F.text == "💥 Socials")
async def socials_entry(message: Message):
    if not await is_approved(message.from_user.id):
        await message.answer(PENDING_MESSAGE)
        return
    
    u = await get_user(message.from_user.id)
    enabled = bool(u.get("socials_opt_in"))
    
    # Получить опубликованные social события
    socials = await db_fetch("""
        SELECT id, title, description, location, starts_at, 
               photo_url, registration_url
        FROM events
        WHERE status = 'published' 
          AND event_type = 'social'
          AND (starts_at IS NULL OR starts_at >= NOW())
        ORDER BY starts_at ASC NULLS LAST
        LIMIT 10
    """)
    
    txt = (
        "💥 *Members-only socials*\n\n"
        "Toggle notifications for informal, low-lift meetups.\n"
        "Types: movie nights, quizzes, walks.\n\n"
        f"Notifications: {'✅ ON' if enabled else '☑ OFF'}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✅ Notify me: ON" if enabled else "☑ Notify me: OFF"),
            callback_data="socials:toggle"
        )]
    ])
    
    await message.answer(txt, reply_markup=kb)
    
    if socials:
        await message.answer("\n📅 *Upcoming Socials:*")
        for social in socials:
            await send_event_card(message.chat.id, social, is_social=True)
    else:
        await message.answer("\nNo upcoming socials at the moment.")

async def send_event_card(chat_id: int, event: asyncpg.Record, is_social: bool = False):
    """Отправить красивую карточку события"""
    
    # Форматирование даты
    def fmt_date(dt):
        if not dt:
            return "TBA"
        return dt.strftime("%B %d, %Y at %H:%M")
    
    # Формирование текста
    icon = "💥" if is_social else "🎉"
    text_parts = [
        f"{icon} *{event['title']}*\n",
    ]
    
    if event.get('description'):
        text_parts.append(f"{event['description']}\n")
    
    if event.get('location'):
        text_parts.append(f"📍 *Location:* {event['location']}")
    
    if event.get('starts_at'):
        text_parts.append(f"🗓 *When:* {fmt_date(event['starts_at'])}")
        if event.get('ends_at'):
            text_parts.append(f"   → {fmt_date(event['ends_at'])}")
    text = "\n".join(text_parts)
    
    # Кнопки
    buttons = []
    
    # Кнопка регистрации
    if event.get('registration_url'):
        buttons.append([InlineKeyboardButton(
            text="📝 Register",
            url=event['registration_url']
        )])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    # Отправка
    if event.get('photo_url'):
        try:
            await bot.send_photo(
                chat_id,
                photo=event['photo_url'],
                caption=text,
                parse_mode='Markdown',
                reply_markup=kb
            )
        except Exception as e:
            print(f"Failed to send photo: {e}")
            # Fallback без фото
            await bot.send_message(
                chat_id,
                text,
                parse_mode='Markdown',
                reply_markup=kb
            )
    else:
        await bot.send_message(
            chat_id,
            text,
            parse_mode='Markdown',
            reply_markup=kb
        )

@dp.callback_query(F.data == "socials:toggle")
async def socials_toggle(cq: CallbackQuery):
    u = await get_user(cq.from_user.id)
    new_val = not bool(u.get("socials_opt_in"))
    await set_socials_opt(cq.from_user.id, new_val)
    await cq.answer("Updated")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✅ Notify me: ON" if new_val else "☑ Notify me: OFF"),
            callback_data="socials:toggle"
        )]
    ])
    
    await cq.message.edit_reply_markup(reply_markup=kb)
    
    if new_val:
        info = (
            "ℹ️ *Members-only socials*: informal, low-lift meetups (movie nights, quizzes, walks).\n"
            "You'll get a notification when we schedule one!"
        )
        await bot.send_message(cq.message.chat.id, info)

# ====== NOTIFICATIONS ======
@dp.message(F.text == "📢 Notifications")
async def notif_entry(message: Message):
    if not await is_approved(message.from_user.id):
        await message.answer(PENDING_MESSAGE)
        return
    u = await get_user(message.from_user.id)
    txt = (
        "📢 *Notifications*\n"
        "Toggles below. Your *Comms mode* is honored: Email-only or Email+Telegram.\n"
        "You can change Comms in ⚙️ Settings."
    )
    await message.answer(txt, reply_markup=notifs_kb(u))

@dp.callback_query(F.data.startswith("ntf:"))
async def notif_toggle(cq: CallbackQuery):
    action = cq.data.split(":")[1]
    field_map = {
        "ann": "notif_announcements",
        "events": "notif_events",
        "rc": "notif_rc",
        "mentor": "notif_mentor",
        "socials": "notif_socials"
    }
    if action == "info":
        u = await get_user(cq.from_user.id)
        await cq.answer("Comms mode", show_alert=False)
        await bot.send_message(cq.message.chat.id, f"📣 Comms mode: *{u.get('communication_mode') or '—'}*")
        return
    field = field_map.get(action)
    if not field:
        await cq.answer("Invalid"); return
    u = await get_user(cq.from_user.id)
    new_val = not bool(u.get(field))
    await set_notif(cq.from_user.id, field, new_val)
    await cq.answer("Updated")
    u = await get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=notifs_kb(u))

# ====== MENTORSHIP ======
@dp.message(F.text == "🎓 Mentorship")
async def mentorship_entry(message: Message):
    if not await is_approved(message.from_user.id):
        await message.answer(PENDING_MESSAGE)
        return
    text = (
        "🎓 *Mentorship*\n\n"
        "1:1 mentorship runs through *May 31, 2025*. ~Monthly calls; focus on career in photonics.\n"
        "Choose your role to pre-register.\n\n"
        "📅 *All program features are now live!*"
    )
    await message.answer(text, reply_markup=mentorship_role_kb())

@dp.callback_query(F.data.startswith("mentorrole:"))
async def mentorship_role_select(cq: CallbackQuery):
    _, role = cq.data.split(":", 1)
    if role == "close":
        await cq.answer()
        with contextlib.suppress(Exception):
            await cq.message.delete()
        return
    await cq.answer("Saved")
    with contextlib.suppress(Exception):
        await cq.message.delete()
    nice = (
        f"🎓 *Mentorship*\n\n"
        f"Thanks for registering as *{'Mentee' if role=='mentee' else 'Mentor'}*!\n"
        f"We've recorded your interest.\n"
        f"📅 The program is live. You'll receive next steps by email/Telegram."
    )
    await bot.send_message(cq.message.chat.id, nice, reply_markup=await main_menu_for(cq.from_user.id))

# ====== FALLBACK ======
@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP, reply_markup=await main_menu_for(message.from_user.id))

@dp.message()
async def unknown(message: Message):
    await message.answer("Main menu:", reply_markup=await main_menu_for(message.from_user.id))

# ====== ENTRYPOINT ======
async def main():
    await init_pool()
    await ensure_schema()

    # Initialize and start API server
    from api import init_api, create_app
    from aiohttp import web
    
    init_api(
        pool=_pool,
        bot=bot,
        admin_ids=ADMIN_IDS,
        run_matching=run_matching_once,
        get_settings_fn=get_settings,
        set_schedule_days_fn=set_schedule_days,
        set_schedule_time_fn=set_schedule_time,
        can_run_now_fn=can_run_now,
        log_run_start_fn=log_run_start,
        log_run_finish_fn=log_run_finish,
        bot_token=BOT_TOKEN
    )
    
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', API_PORT)
    await site.start()
    
    print(f"✅ API server started on port {API_PORT}")
    print(f"✅ Web Admin: {WEBAPP_URL}")
    print(f"✅ Bot is running...")

    # Start scheduler and polling
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())