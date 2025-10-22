# matcher.py - С поддержкой шаблонов из БД
import random
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional

import asyncpg
from aiogram import Bot
from zoneinfo import ZoneInfo

# Импортируем email функции
from email_sender import send_email, create_random_coffee_email, send_templated_email, render_template


async def run_matching_once(
    pool: asyncpg.Pool,
    bot: Bot,
    timezone_name: str,
    lookback_weeks: int,
    starter_questions: List[str],
) -> int:
    """
    Формирует пары и отправляет уведомления через Telegram И Email
    """
    cand = await _fetch_candidates(pool)
    random.shuffle(cand)

    assigned = set()
    pairs: List[Tuple[asyncpg.Record, asyncpg.Record]] = []

    for i, A in enumerate(cand):
        if A["user_id"] in assigned:
            continue

        options = []
        for j in range(i + 1, len(cand)):
            B = cand[j]
            if B["user_id"] in assigned:
                continue
            if _are_compatible(A, B):
                options.append(B)

        random.shuffle(options)
        chosen: Optional[asyncpg.Record] = None
        for B in options:
            if not await _paired_recently(pool, A["user_id"], B["user_id"], lookback_weeks):
                chosen = B
                break

        if chosen:
            assigned.add(A["user_id"])
            assigned.add(chosen["user_id"])
            pairs.append((A, chosen))

    if not pairs:
        return 0

    await _persist_pairs(pool, pairs, timezone_name)
    await _notify_pairs(pool, bot, pairs, timezone_name, starter_questions)
    return len(pairs)


async def _fetch_candidates(pool: asyncpg.Pool) -> List[asyncpg.Record]:
    """Получить всех активных кандидатов"""
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT user_id, username, full_name, email, segment, affiliation, 
                   about, communication_mode
            FROM users
            WHERE subscribed = TRUE
              AND status = 'approved'
            """
        )
    return rows


def _are_compatible(A: asyncpg.Record, B: asyncpg.Record) -> bool:
    """Проверка совместимости"""
    return True


async def _paired_recently(
    pool: asyncpg.Pool,
    a: int, b: int,
    lookback_weeks: int
) -> bool:
    """Проверить были ли пользователи в паре недавно"""
    ua, ub = sorted([a, b])
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """SELECT last_matched_at
               FROM pairings
               WHERE user_a=$1 AND user_b=$2""",
            ua, ub
        )
    if not row or not row["last_matched_at"]:
        return False
    weeks = (datetime.now(timezone.utc) - row["last_matched_at"]).days / 7.0
    return weeks < float(lookback_weeks)


def _monday_of_week(dt_local: datetime) -> datetime:
    wk = dt_local.weekday()
    m = dt_local - timedelta(days=wk)
    return m.replace(hour=0, minute=0, second=0, microsecond=0)


async def _persist_pairs(
    pool: asyncpg.Pool,
    pairs: List[Tuple[asyncpg.Record, asyncpg.Record]],
    timezone_name: str
) -> None:
    """Сохранить пары в БД"""
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(timezone_name)
    dt_local = now.astimezone(tz)
    week_monday = _monday_of_week(dt_local).date()

    async with pool.acquire() as con:
        tr = con.transaction()
        await tr.start()
        try:
            for A, B in pairs:
                ua, ub = sorted([A["user_id"], B["user_id"]])
                await con.execute(
                    """
                    INSERT INTO pairings (user_a, user_b, last_matched_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_a, user_b)
                    DO UPDATE SET last_matched_at = EXCLUDED.last_matched_at
                    """,
                    ua, ub, now
                )
                await con.execute(
                    """
                    INSERT INTO weekly_matches (week_date, user_a, user_b)
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING
                    """,
                    week_monday, ua, ub
                )
            await tr.commit()
        except Exception:
            await tr.rollback()
            raise


def _pretty_name(u: asyncpg.Record) -> str:
    return u["full_name"] or "New friend"


def _contact(u: asyncpg.Record) -> str:
    username = f"@{u['username']}" if u["username"] else "Not provided"
    email = u["email"] or "Not provided"
    return f"{username} / {email}".strip()


def _telegram_contact(u: asyncpg.Record) -> str:
    return f"@{u['username']}" if u["username"] else "Not provided"


async def _notify_pairs(
    pool: asyncpg.Pool,
    bot: Bot,
    pairs: List[Tuple[asyncpg.Record, asyncpg.Record]],
    timezone_name: str,
    starter_questions: List[str],
) -> None:
    """Отправить уведомления парам через Telegram И Email используя шаблоны"""
    
    # Форматируем starter questions для шаблонов
    questions_html = '<ul>' + ''.join(f'<li>{q}</li>' for q in starter_questions) + '</ul>'
    questions_text = '\n'.join(f"• {q}" for q in starter_questions)
    questions_block = "💬 *Starter questions:*\n- " + "\n- ".join(starter_questions)

    for A, B in pairs:
        # === Отправка пользователю A ===
        comm_mode_a = A.get('communication_mode') or 'email+telegram'
        
        # Telegram для A
        if comm_mode_a in ['telegram_only', 'email+telegram']:
            tg_text_a = (
                "☕ *Your Random Coffee match for this week*\n\n"
                f"👤 *Name*: {_pretty_name(B)}\n"
                f"🎓 *Segment*: {B.get('segment') or '—'}\n"
                f"🏫 *Affiliation*: {B.get('affiliation') or '—'}\n"
                f"📲 *Contact*: {_contact(B)}\n\n"
                f"📝 *About them*:\n{B.get('about') or '_(not provided)_'}\n\n"
                f"{questions_block}"
            )
            try:
                await bot.send_message(A["user_id"], tg_text_a, parse_mode='Markdown')
            except Exception as e:
                print(f"Failed to send Telegram to {A['user_id']}: {e}")
        
        # Email для A (используем шаблон из БД или fallback)
        if comm_mode_a in ['email_only', 'email+telegram'] and A.get('email'):
            try:
                # Переменные для шаблона
                variables = {
                    'user_name': _pretty_name(A),
                    'match_name': _pretty_name(B),
                    'match_segment': B.get('segment') or 'Not specified',
                    'match_affiliation': B.get('affiliation') or 'Not specified',
                    'match_about': B.get('about') or 'No information provided',
                    'match_email': B.get('email') or 'Not provided',
                    'match_telegram': _telegram_contact(B),
                    'starter_questions': questions_html  # HTML версия
                }
                
                # Пытаемся использовать шаблон из БД
                success = await send_templated_email(
                    pool,
                    'random_coffee',
                    A['email'],
                    variables
                )
                
                # Если шаблон не найден, используем fallback
                if not success:
                    html, text = create_random_coffee_email(
                        user_name=_pretty_name(A),
                        match_name=_pretty_name(B),
                        match_segment=B.get('segment') or 'Not specified',
                        match_affiliation=B.get('affiliation') or 'Not specified',
                        match_about=B.get('about') or 'No information provided',
                        match_email=B.get('email') or 'Not provided',
                        match_telegram=_telegram_contact(B),
                        starter_questions=starter_questions
                    )
                    await send_email(
                        A['email'],
                        "☕ Your Random Coffee Match This Week",
                        html,
                        text
                    )
            except Exception as e:
                print(f"Failed to send email to {A['email']}: {e}")
        
        # === Отправка пользователю B ===
        comm_mode_b = B.get('communication_mode') or 'email+telegram'
        
        # Telegram для B
        if comm_mode_b in ['telegram_only', 'email+telegram']:
            tg_text_b = (
                "☕ *Your Random Coffee match for this week*\n\n"
                f"👤 *Name*: {_pretty_name(A)}\n"
                f"🎓 *Segment*: {A.get('segment') or '—'}\n"
                f"🏫 *Affiliation*: {A.get('affiliation') or '—'}\n"
                f"📲 *Contact*: {_contact(A)}\n\n"
                f"📝 *About them*:\n{A.get('about') or '_(not provided)_'}\n\n"
                f"{questions_block}"
            )
            try:
                await bot.send_message(B["user_id"], tg_text_b, parse_mode='Markdown')
            except Exception as e:
                print(f"Failed to send Telegram to {B['user_id']}: {e}")
        
        # Email для B (используем шаблон из БД или fallback)
        if comm_mode_b in ['email_only', 'email+telegram'] and B.get('email'):
            try:
                # Переменные для шаблона
                variables = {
                    'user_name': _pretty_name(B),
                    'match_name': _pretty_name(A),
                    'match_segment': A.get('segment') or 'Not specified',
                    'match_affiliation': A.get('affiliation') or 'Not specified',
                    'match_about': A.get('about') or 'No information provided',
                    'match_email': A.get('email') or 'Not provided',
                    'match_telegram': _telegram_contact(A),
                    'starter_questions': questions_html  # HTML версия
                }
                
                # Пытаемся использовать шаблон из БД
                success = await send_templated_email(
                    pool,
                    'random_coffee',
                    B['email'],
                    variables
                )
                
                # Если шаблон не найден, используем fallback
                if not success:
                    html, text = create_random_coffee_email(
                        user_name=_pretty_name(B),
                        match_name=_pretty_name(A),
                        match_segment=A.get('segment') or 'Not specified',
                        match_affiliation=A.get('affiliation') or 'Not specified',
                        match_about=A.get('about') or 'No information provided',
                        match_email=A.get('email') or 'Not provided',
                        match_telegram=_telegram_contact(A),
                        starter_questions=starter_questions
                    )
                    await send_email(
                        B['email'],
                        "☕ Your Random Coffee Match This Week",
                        html,
                        text
                    )
            except Exception as e:
                print(f"Failed to send email to {B['email']}: {e}")