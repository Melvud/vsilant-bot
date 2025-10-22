# api.py - REST API for Admin Web App with Email Templates
import os
import json
import hmac
import hashlib
from urllib.parse import unquote
from datetime import datetime, timezone
from typing import Optional
import asyncpg
from aiohttp import web
import aiohttp_cors
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Dependencies
_pool: Optional[asyncpg.Pool] = None
_bot = None
_admin_ids = set()
_run_matching = None
_get_settings = None
_set_schedule_days = None
_set_schedule_time = None
_can_run_now = None
_log_run_start = None
_log_run_finish = None
BOT_TOKEN = ""


def verify_telegram_web_app(init_data: str, bot_token: str) -> Optional[dict]:
    try:
        params = dict(item.split('=', 1) for item in init_data.split('&'))
        data_check_string = '\n'.join(
            f"{k}={unquote(v)}" for k, v in sorted(params.items()) if k != 'hash'
        )
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash != params.get('hash'):
            return None
        
        user_data = json.loads(unquote(params.get('user', '{}')))
        return user_data
    except Exception as e:
        print(f"Verification error: {e}")
        return None


@web.middleware
async def auth_middleware(request, handler):
    if request.path in ['/health', '/']:
        return await handler(request)
    
    if not request.path.startswith('/api/'):
        return await handler(request)
    
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('tma '):
        return web.json_response({'error': 'Unauthorized'}, status=401)
    
    init_data = auth[4:]
    user = verify_telegram_web_app(init_data, BOT_TOKEN)
    
    if not user or user.get('id') not in _admin_ids:
        return web.json_response({'error': 'Forbidden'}, status=403)
    
    request['user_id'] = user.get('id')
    return await handler(request)


async def health(request):
    return web.json_response({'status': 'ok'})


async def get_stats(request):
    total = await _pool.fetchval("SELECT COUNT(*) FROM users")
    subscribed = await _pool.fetchval("SELECT COUNT(*) FROM users WHERE subscribed=TRUE")
    mentors = await _pool.fetchval("SELECT COUNT(*) FROM users WHERE mentor_flag=TRUE")
    pending = await _pool.fetchval("SELECT COUNT(*) FROM users WHERE status='pending'")
    
    segments = await _pool.fetch("""
        SELECT segment, COUNT(*) as count FROM users 
        WHERE segment IS NOT NULL GROUP BY segment ORDER BY count DESC
    """)
    
    recent_pairs = await _pool.fetchval("""
        SELECT COUNT(*) FROM pairings 
        WHERE last_matched_at > NOW() - INTERVAL '7 days'
    """)
    
    return web.json_response({
        'total_users': total,
        'subscribed': subscribed,
        'mentors': mentors,
        'pending_approvals': pending,
        'recent_pairs': recent_pairs,
        'segments': [{'name': r['segment'], 'count': r['count']} for r in segments]
    })


async def get_schedule(request):
    settings = await _get_settings()
    ok, rem = await _can_run_now()
    
    return web.json_response({
        'schedule_days': settings.get('schedule_days', []),
        'schedule_time': settings.get('schedule_time', '09:00'),
        'last_run_at': settings.get('last_run_at').isoformat() if settings.get('last_run_at') else None,
        'can_run_now': ok,
        'cooldown_minutes': int(rem.total_seconds() / 60) if rem else 0
    })


async def update_schedule(request):
    data = await request.json()
    if 'schedule_days' in data:
        await _set_schedule_days(data['schedule_days'])
    if 'schedule_time' in data:
        await _set_schedule_time(data['schedule_time'])
    return web.json_response({'success': True})


async def run_matching_now(request):
    ok, rem = await _can_run_now()
    if not ok:
        return web.json_response({'error': f'Cooldown: {int(rem.total_seconds()/60)} min'}, status=429)
    
    user_id = request['user_id']
    run_id = await _log_run_start('manual', user_id)
    
    try:
        count = await _run_matching()
        await _log_run_finish(run_id, count, ok=True)
        return web.json_response({'success': True, 'pairs': count})
    except Exception as e:
        await _log_run_finish(run_id, 0, ok=False, error_text=str(e))
        return web.json_response({'error': str(e)}, status=500)


async def get_run_history(request):
    limit = int(request.query.get('limit', 20))
    rows = await _pool.fetch("""
        SELECT id, run_type, started_at, finished_at, pairs_count, status, error_text, triggered_by
        FROM run_logs ORDER BY started_at DESC LIMIT $1
    """, limit)
    
    return web.json_response([{
        'id': r['id'], 'run_type': r['run_type'],
        'started_at': r['started_at'].isoformat(),
        'finished_at': r['finished_at'].isoformat() if r['finished_at'] else None,
        'pairs_count': r['pairs_count'], 'status': r['status'],
        'error_text': r['error_text'], 'triggered_by': r['triggered_by']
    } for r in rows])


async def get_pending_approvals(request):
    rows = await _pool.fetch("""
        SELECT user_id, full_name, email, segment, affiliation, about, created_at
        FROM users WHERE status='pending' ORDER BY created_at DESC LIMIT 50
    """)
    
    return web.json_response([{
        'user_id': r['user_id'], 'full_name': r['full_name'],
        'email': r['email'], 'segment': r['segment'],
        'affiliation': r['affiliation'], 'about': r['about'],
        'created_at': r['created_at'].isoformat()
    } for r in rows])


async def approve_user(request):
    data = await request.json()
    user_id = data.get('user_id')
    admin_id = request['user_id']
    
    await _pool.execute("UPDATE users SET status='approved', updated_at=NOW() WHERE user_id=$1", user_id)
    await _pool.execute("INSERT INTO approvals_log(user_id, action, by_admin) VALUES($1,'approved',$2)", user_id, admin_id)
    
    try:
        await _bot.send_message(user_id, "🎉 Your profile has been *approved*. Welcome!")
    except:
        pass
    
    return web.json_response({'success': True})


async def reject_user(request):
    data = await request.json()
    user_id = data.get('user_id')
    admin_id = request['user_id']
    
    await _pool.execute("UPDATE users SET status='rejected', updated_at=NOW() WHERE user_id=$1", user_id)
    await _pool.execute("INSERT INTO approvals_log(user_id, action, by_admin) VALUES($1,'rejected',$2)", user_id, admin_id)
    
    try:
        await _bot.send_message(user_id, "⚠️ Your profile was *rejected*. You can /start again.")
    except:
        pass
    
    return web.json_response({'success': True})


async def get_subscribers(request):
    limit = int(request.query.get('limit', 50))
    rows = await _pool.fetch("""
        SELECT user_id, full_name, segment, affiliation, rc_frequency,
               rc_pref_tue, rc_pref_universities, rc_pref_industry
        FROM users WHERE subscribed=TRUE
        ORDER BY updated_at DESC NULLS LAST, created_at DESC LIMIT $1
    """, limit)
    
    return web.json_response([{
        'user_id': r['user_id'], 'full_name': r['full_name'],
        'segment': r['segment'], 'affiliation': r['affiliation'],
        'rc_frequency': r['rc_frequency'],
        'preferences': {'tue': r['rc_pref_tue'], 'universities': r['rc_pref_universities'], 'industry': r['rc_pref_industry']}
    } for r in rows])


async def pause_user_subscription(request):
    data = await request.json()
    user_id = data.get('user_id')
    await _pool.execute("UPDATE users SET subscribed=FALSE, updated_at=NOW() WHERE user_id=$1", user_id)
    
    try:
        await _bot.send_message(user_id, "⏸ Paused from Random Coffee by admin.")
    except:
        pass
    
    return web.json_response({'success': True})


async def get_events(request):
    limit = int(request.query.get('limit', 50))
    event_type = request.query.get('type')  # 'event' or 'social'
    
    where_clause = "WHERE 1=1"
    if event_type:
        where_clause += f" AND event_type = '{event_type}'"
    
    rows = await _pool.fetch(f"""
        SELECT id, title, description, location, starts_at, ends_at,
               capacity, rsvp_open_at, rsvp_close_at, status, event_type,
               photo_url, registration_url, broadcasted_at
        FROM events {where_clause}
        ORDER BY starts_at DESC NULLS LAST, id DESC 
        LIMIT $1
    """, limit)
    
    return web.json_response([{
        'id': r['id'], 
        'title': r['title'], 
        'description': r['description'],
        'location': r['location'],
        'starts_at': r['starts_at'].isoformat() if r['starts_at'] else None,
        'ends_at': r['ends_at'].isoformat() if r['ends_at'] else None,
        'capacity': r['capacity'],
        'rsvp_open_at': r['rsvp_open_at'].isoformat() if r['rsvp_open_at'] else None,
        'rsvp_close_at': r['rsvp_close_at'].isoformat() if r['rsvp_close_at'] else None,
        'status': r['status'], 
        'event_type': r['event_type'],
        'photo_url': r['photo_url'],
        'registration_url': r['registration_url'],
        'broadcasted': r['broadcasted_at'] is not None
    } for r in rows])

async def upload_image(request):
    """Upload image and return URL"""
    try:
        reader = await request.multipart()
        field = await reader.next()
        
        if field.name != 'image':
            return web.json_response({'error': 'No image field'}, status=400)
        
        filename = field.filename
        if not filename:
            return web.json_response({'error': 'No filename'}, status=400)
        
        # Validate file extension
        ext = filename.split('.')[-1].lower()
        if ext not in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
            return web.json_response({'error': 'Invalid file type'}, status=400)
        
        # Generate unique filename
        import uuid
        unique_filename = f"{uuid.uuid4()}.{ext}"
        
        # Create uploads directory if not exists
        upload_dir = '/app/uploads'
        os.makedirs(upload_dir, exist_ok=True)
        
        # Save file
        filepath = os.path.join(upload_dir, unique_filename)
        with open(filepath, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        
        # Return URL (adjust based on your server setup)
        url = f"{request.scheme}://{request.host}/uploads/{unique_filename}"
        return web.json_response({'success': True, 'url': url})
        
    except Exception as e:
        print(f"Error uploading image: {e}")
        return web.json_response({'error': str(e)}, status=500)


async def serve_upload(request):
    """Serve uploaded files"""
    filename = request.match_info['filename']
    filepath = f'/app/uploads/{filename}'
    
    if not os.path.exists(filepath):
        return web.Response(status=404)
    
    return web.FileResponse(filepath)

async def create_event(request):
    try:
        data = await request.json()
        admin_id = request['user_id']
        
        # Validate required fields
        if not data.get('title'):
            return web.json_response({'error': 'Title is required'}, status=400)
        
        # Parse datetime strings if provided
        starts_at = data.get('starts_at')
        ends_at = data.get('ends_at')
        rsvp_open_at = data.get('rsvp_open_at')
        rsvp_close_at = data.get('rsvp_close_at')
        
        # Convert to datetime objects if strings are provided
        if starts_at and isinstance(starts_at, str):
            try:
                starts_at = datetime.fromisoformat(starts_at.replace('Z', '+00:00'))
            except:
                starts_at = None
        
        if ends_at and isinstance(ends_at, str):
            try:
                ends_at = datetime.fromisoformat(ends_at.replace('Z', '+00:00'))
            except:
                ends_at = None
        
        if rsvp_open_at and isinstance(rsvp_open_at, str):
            try:
                rsvp_open_at = datetime.fromisoformat(rsvp_open_at.replace('Z', '+00:00'))
            except:
                rsvp_open_at = None
        
        if rsvp_close_at and isinstance(rsvp_close_at, str):
            try:
                rsvp_close_at = datetime.fromisoformat(rsvp_close_at.replace('Z', '+00:00'))
            except:
                rsvp_close_at = None
        
        event_id = await _pool.fetchval("""
            INSERT INTO events(
                title, description, location, starts_at, ends_at,
                capacity, rsvp_open_at, rsvp_close_at, status, event_type,
                photo_url, registration_url, created_by
            )
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            RETURNING id
        """, 
            data.get('title'), 
            data.get('description'), 
            data.get('location'),
            starts_at, 
            ends_at, 
            data.get('capacity'),
            rsvp_open_at, 
            rsvp_close_at,
            data.get('status', 'draft'),
            data.get('event_type', 'event'),
            data.get('photo_url'),
            data.get('registration_url'),
            admin_id
        )
        
        return web.json_response({'success': True, 'id': event_id})
        
    except Exception as e:
        print(f"Error creating event: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({'error': str(e)}, status=500)

async def delete_event(request):
    """Delete event by ID"""
    try:
        event_id = int(request.match_info['id'])
        
        # Check if event exists
        exists = await _pool.fetchval(
            "SELECT id FROM events WHERE id=$1",
            event_id
        )
        
        if not exists:
            return web.json_response({'error': 'Event not found'}, status=404)
        
        # Delete event (CASCADE will delete related RSVPs automatically)
        await _pool.execute("DELETE FROM events WHERE id=$1", event_id)
        
        return web.json_response({'success': True})
        
    except ValueError:
        return web.json_response({'error': 'Invalid event ID'}, status=400)
    except Exception as e:
        print(f"Error deleting event: {e}")
        return web.json_response({'error': str(e)}, status=500)


async def update_event(request):
    event_id = int(request.match_info['id'])
    data = await request.json()
    set_clauses, values, idx = [], [], 1
    
    for field in ['title', 'description', 'location', 'starts_at', 'ends_at', 
                  'capacity', 'rsvp_open_at', 'rsvp_close_at', 'status', 'event_type',
                  'photo_url', 'registration_url']:
        if field in data:
            set_clauses.append(f"{field}=${idx}")
            values.append(data[field])
            idx += 1
    
    if set_clauses:
        set_clauses.append("updated_at=NOW()")
        values.append(event_id)
        await _pool.execute(
            f"UPDATE events SET {', '.join(set_clauses)} WHERE id=${idx}", 
            *values
        )
    
    return web.json_response({'success': True})


async def broadcast_event(request):
    event_id = int(request.match_info['id'])
    event = await _pool.fetchrow("SELECT * FROM events WHERE id=$1", event_id)
    if not event:
        return web.json_response({'error': 'Not found'}, status=404)
    
    if event['status'] != 'published':
        await _pool.execute("UPDATE events SET status='published' WHERE id=$1", event_id)
    
    # Определить целевую аудиторию
    is_social = event['event_type'] == 'social'
    
    if is_social:
        # Только подписчики на socials
        users = await _pool.fetch("""
            SELECT user_id, communication_mode FROM users
            WHERE socials_opt_in = TRUE
              AND COALESCE(notif_socials, TRUE) = TRUE
              AND COALESCE(communication_mode,'email+telegram') IN ('email+telegram', 'telegram_only')
              AND COALESCE(status,'approved') = 'approved'
        """)
    else:
        # Все пользователи с включенными уведомлениями о событиях
        users = await _pool.fetch("""
            SELECT user_id, communication_mode FROM users
            WHERE COALESCE(notif_events,TRUE) = TRUE
              AND COALESCE(communication_mode,'email+telegram') IN ('email+telegram', 'telegram_only')
              AND COALESCE(status,'approved') = 'approved'
        """)
    
    def fmt(v): 
        return v.strftime("%B %d, %Y at %H:%M") if v else "TBA"
    
    icon = "💥" if is_social else "🎉"
    text = f"{icon} *New {'Social' if is_social else 'Event'}:* {event['title']}\n\n"
    
    if event['description']:
        text += f"{event['description']}\n\n"
    
    text += f"📍 *Location:* {event['location'] or '—'}\n"
    text += f"🗓 *When:* {fmt(event['starts_at'])}\n"
    
    if event['capacity']:
        text += f"👥 *Capacity:* {event['capacity']} spots\n"
    
    # Кнопка регистрации
    kb = None
    if event.get('registration_url'):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Register", url=event['registration_url'])]
        ])
    
    sent = 0
    for u in users:
        try:
            if event.get('photo_url'):
                try:
                    await _bot.send_photo(
                        u['user_id'], 
                        photo=event['photo_url'],
                        caption=text,
                        parse_mode='Markdown',
                        reply_markup=kb
                    )
                except:
                    # Fallback без фото
                    await _bot.send_message(u['user_id'], text, parse_mode='Markdown', reply_markup=kb)
            else:
                await _bot.send_message(u['user_id'], text, parse_mode='Markdown', reply_markup=kb)
            sent += 1
        except Exception as e:
            print(f"Failed to send to {u['user_id']}: {e}")
    
    await _pool.execute("UPDATE events SET broadcasted_at=NOW() WHERE id=$1", event_id)
    return web.json_response({'success': True, 'sent_to': sent})

async def send_broadcast(request):
    data = await request.json()
    admin_id = request['user_id']
    body = data.get('body')
    filters = data.get('filters', {})
    
    where = ["COALESCE(communication_mode,'email+telegram') IN ('email+telegram', 'telegram_only')",
             "COALESCE(notif_announcements,TRUE)=TRUE", "COALESCE(status,'approved')='approved'"]
    args = []
    
    if filters.get('segments'):
        args.append(filters['segments'])
        where.append(f"segment = ANY(${len(args)})")
    
    if filters.get('affiliations'):
        args.append(filters['affiliations'])
        where.append(f"affiliation = ANY(${len(args)})")
    
    users = await _pool.fetch(f"SELECT user_id FROM users WHERE {' AND '.join(where)}", *args)
    
    sent = 0
    for u in users:
        try:
            await _bot.send_message(u['user_id'], body, parse_mode='Markdown')
            sent += 1
        except:
            pass
    
    await _pool.execute("""
        INSERT INTO broadcasts(title, body, segment_filter, affiliation_filter, created_by, sent_to)
        VALUES($1,$2,$3,$4,$5,$6)
    """, body[:50], body, filters.get('segments'), filters.get('affiliations'), admin_id, sent)
    
    return web.json_response({'success': True, 'sent_to': sent})


async def get_mentors(request):
    """Получить всех менторов"""
    rows = await _pool.fetch("""
        SELECT u.user_id, u.full_name, u.email, u.username, u.segment, u.affiliation, u.about,
               m.tags, m.monthly_avail
        FROM users u
        LEFT JOIN mentorship_mentors m ON u.user_id = m.user_id
        WHERE u.mentor_flag = TRUE AND u.status = 'approved'
        ORDER BY u.full_name
    """)
    
    return web.json_response([{
        'user_id': r['user_id'],
        'full_name': r['full_name'],
        'email': r['email'],
        'username': r['username'],
        'segment': r['segment'],
        'affiliation': r['affiliation'],
        'about': r['about'],
        'tags': r['tags'] or [],
        'monthly_avail': r['monthly_avail']
    } for r in rows])


async def get_mentees(request):
    """Получить всех ментиев"""
    rows = await _pool.fetch("""
        SELECT u.user_id, u.full_name, u.email, u.username, u.segment, u.affiliation, u.about,
               m.interests, m.pref, m.availability_window,
               mm.mentor_id, mentor.full_name as mentor_name
        FROM users u
        LEFT JOIN mentorship_mentees m ON u.user_id = m.user_id
        LEFT JOIN mentorship_matches mm ON u.user_id = mm.mentee_id AND mm.active = TRUE
        LEFT JOIN users mentor ON mm.mentor_id = mentor.user_id
        WHERE u.status = 'approved'
          AND (m.user_id IS NOT NULL OR EXISTS (
              SELECT 1 FROM mentorship_matches WHERE mentee_id = u.user_id
          ))
        ORDER BY u.full_name
    """)
    
    return web.json_response([{
        'user_id': r['user_id'],
        'full_name': r['full_name'],
        'email': r['email'],
        'username': r['username'],
        'segment': r['segment'],
        'affiliation': r['affiliation'],
        'about': r['about'],
        'interests': r['interests'] or [],
        'pref': r['pref'],
        'availability_window': r['availability_window'],
        'has_mentor': r['mentor_id'] is not None,
        'mentor_id': r['mentor_id'],
        'mentor_name': r['mentor_name']
    } for r in rows])


async def assign_mentor(request):
    """Назначить ментора менти с использованием email templates"""
    from email_sender import send_templated_email, create_mentorship_email, send_email
    
    data = await request.json()
    mentor_id = data.get('mentor_id')
    mentee_id = data.get('mentee_id')
    
    if not mentor_id or not mentee_id:
        return web.json_response({'error': 'Missing IDs'}, status=400)
    
    try:
        # Получить данные ментора и менти
        mentor = await _pool.fetchrow("SELECT * FROM users WHERE user_id=$1", mentor_id)
        mentee = await _pool.fetchrow("SELECT * FROM users WHERE user_id=$1", mentee_id)
        
        if not mentor or not mentee:
            return web.json_response({'error': 'User not found'}, status=404)
        
        # Создать match
        await _pool.execute("""
            INSERT INTO mentorship_matches(mentor_id, mentee_id, active, matched_at)
            VALUES($1, $2, TRUE, NOW())
            ON CONFLICT (mentor_id, mentee_id) 
            DO UPDATE SET active=TRUE, matched_at=NOW()
        """, mentor_id, mentee_id)
        
        def fmt_contact(u):
            parts = []
            if u['email']:
                parts.append(f"📧 {u['email']}")
            if u['username']:
                parts.append(f"💬 @{u['username']}")
            return "\n".join(parts) if parts else "No contact info"
        
        def telegram_contact(u):
            return f"@{u['username']}" if u['username'] else "Not provided"
        
        # === Отправка ментору ===
        comm_mode_mentor = mentor.get('communication_mode') or 'email+telegram'
        
        # Telegram для ментора
        if comm_mode_mentor in ['telegram_only', 'email+telegram']:
            mentor_tg_text = (
                "🎓 *Mentorship Match Assigned*\n\n"
                f"You have been assigned as a mentor to:\n\n"
                f"👤 *Name*: {mentee['full_name']}\n"
                f"🎓 *Segment*: {mentee.get('segment') or '—'}\n"
                f"🏫 *Affiliation*: {mentee.get('affiliation') or '—'}\n\n"
                f"📝 *About*:\n{mentee.get('about') or '—'}\n\n"
                f"📲 *Contact*:\n{fmt_contact(mentee)}\n\n"
                f"*Next Steps:*\n"
                f"• Reach out to schedule your first mentoring session\n"
                f"• Aim for monthly calls throughout the program\n"
                f"• Focus on career development in photonics\n\n"
                f"Program runs through May 31, 2025."
            )
            try:
                await _bot.send_message(mentor_id, mentor_tg_text, parse_mode='Markdown')
            except Exception as e:
                print(f"Failed to notify mentor via Telegram: {e}")
        
        # Email для ментора (используем template из БД)
        if comm_mode_mentor in ['email_only', 'email+telegram'] and mentor.get('email'):
            try:
                # Переменные для шаблона
                variables = {
                    'user_name': mentor['full_name'],
                    'user_role_title': "You've Been Assigned a Mentee",
                    'match_emoji': '👨‍🎓',
                    'match_role': 'Mentee',
                    'match_name': mentee['full_name'],
                    'match_segment': mentee.get('segment') or 'Not specified',
                    'match_affiliation': mentee.get('affiliation') or 'Not specified',
                    'match_about': mentee.get('about') or 'No information provided',
                    'match_email': mentee.get('email') or 'Not provided',
                    'match_telegram': telegram_contact(mentee),
                    'next_steps': '• Reach out to schedule your first mentoring session<br>• Aim for monthly calls throughout the program<br>• Focus on career development in photonics'
                }
                
                # Пытаемся использовать шаблон из БД
                success = await send_templated_email(
                    _pool,
                    'mentorship_match',
                    mentor['email'],
                    variables
                )
                
                # Если шаблон не найден, используем fallback
                if not success:
                    html, text = create_mentorship_email(
                        user_name=mentor['full_name'],
                        user_role='mentor',
                        match_name=mentee['full_name'],
                        match_segment=mentee.get('segment') or 'Not specified',
                        match_affiliation=mentee.get('affiliation') or 'Not specified',
                        match_about=mentee.get('about') or 'No information provided',
                        match_email=mentee.get('email') or 'Not provided',
                        match_telegram=telegram_contact(mentee)
                    )
                    await send_email(
                        mentor['email'],
                        "🎓 Mentorship Match - You've Been Assigned a Mentee",
                        html,
                        text
                    )
            except Exception as e:
                print(f"Failed to send email to mentor: {e}")
        
        # === Отправка менти ===
        comm_mode_mentee = mentee.get('communication_mode') or 'email+telegram'
        
        # Telegram для менти
        if comm_mode_mentee in ['telegram_only', 'email+telegram']:
            mentee_tg_text = (
                "🎓 *Mentorship Match Assigned*\n\n"
                f"You have been matched with a mentor:\n\n"
                f"👤 *Name*: {mentor['full_name']}\n"
                f"🎓 *Segment*: {mentor.get('segment') or '—'}\n"
                f"🏫 *Affiliation*: {mentor.get('affiliation') or '—'}\n\n"
                f"📝 *About*:\n{mentor.get('about') or '—'}\n\n"
                f"📲 *Contact*:\n{fmt_contact(mentor)}\n\n"
                f"*Next Steps:*\n"
                f"• Your mentor will reach out to you soon\n"
                f"• Prepare questions about career in photonics\n"
                f"• Be open and engaged in the mentoring process\n\n"
                f"Program runs through May 31, 2025."
            )
            try:
                await _bot.send_message(mentee_id, mentee_tg_text, parse_mode='Markdown')
            except Exception as e:
                print(f"Failed to notify mentee via Telegram: {e}")
        
        # Email для менти (используем template из БД)
        if comm_mode_mentee in ['email_only', 'email+telegram'] and mentee.get('email'):
            try:
                # Переменные для шаблона
                variables = {
                    'user_name': mentee['full_name'],
                    'user_role_title': "You've Been Assigned a Mentor",
                    'match_emoji': '👨‍🏫',
                    'match_role': 'Mentor',
                    'match_name': mentor['full_name'],
                    'match_segment': mentor.get('segment') or 'Not specified',
                    'match_affiliation': mentor.get('affiliation') or 'Not specified',
                    'match_about': mentor.get('about') or 'No information provided',
                    'match_email': mentor.get('email') or 'Not provided',
                    'match_telegram': telegram_contact(mentor),
                    'next_steps': '• Your mentor will reach out to you soon<br>• Prepare questions about career in photonics<br>• Be open and engaged in the mentoring process'
                }
                
                # Пытаемся использовать шаблон из БД
                success = await send_templated_email(
                    _pool,
                    'mentorship_match',
                    mentee['email'],
                    variables
                )
                
                # Если шаблон не найден, используем fallback
                if not success:
                    html, text = create_mentorship_email(
                        user_name=mentee['full_name'],
                        user_role='mentee',
                        match_name=mentor['full_name'],
                        match_segment=mentor.get('segment') or 'Not specified',
                        match_affiliation=mentor.get('affiliation') or 'Not specified',
                        match_about=mentor.get('about') or 'No information provided',
                        match_email=mentor.get('email') or 'Not provided',
                        match_telegram=telegram_contact(mentor)
                    )
                    await send_email(
                        mentee['email'],
                        "🎓 Mentorship Match - You've Been Assigned a Mentor",
                        html,
                        text
                    )
            except Exception as e:
                print(f"Failed to send email to mentee: {e}")
        
        return web.json_response({'success': True})
        
    except Exception as e:
        print(f"Error in assign_mentor: {e}")
        return web.json_response({'error': str(e)}, status=500)


async def unassign_mentor(request):
    """Отменить назначение ментора"""
    data = await request.json()
    mentor_id = data.get('mentor_id')
    mentee_id = data.get('mentee_id')
    
    await _pool.execute("""
        UPDATE mentorship_matches 
        SET active = FALSE 
        WHERE mentor_id = $1 AND mentee_id = $2
    """, mentor_id, mentee_id)
    
    return web.json_response({'success': True})


# ====== EMAIL TEMPLATES ENDPOINTS ======

async def get_email_templates(request):
    """Get all email templates"""
    try:
        rows = await _pool.fetch("""
            SELECT id, name, subject, description, variables, created_at, updated_at
            FROM email_templates
            ORDER BY name
        """)
        
        return web.json_response([{
            'id': r['id'],
            'name': r['name'],
            'subject': r['subject'],
            'description': r['description'],
            'variables': r['variables'] or [],
            'created_at': r['created_at'].isoformat(),
            'updated_at': r['updated_at'].isoformat()
        } for r in rows])
        
    except Exception as e:
        print(f"Error fetching templates: {e}")
        return web.json_response({'error': str(e)}, status=500)


async def get_email_template_by_id(request):
    """Get single email template by ID"""
    try:
        template_id = int(request.match_info['id'])
        
        row = await _pool.fetchrow("""
            SELECT id, name, subject, html_body, text_body, description, variables, 
                   created_at, updated_at
            FROM email_templates
            WHERE id = $1
        """, template_id)
        
        if not row:
            return web.json_response({'error': 'Template not found'}, status=404)
        
        return web.json_response({
            'id': row['id'],
            'name': row['name'],
            'subject': row['subject'],
            'html_body': row['html_body'],
            'text_body': row['text_body'],
            'description': row['description'],
            'variables': row['variables'] or [],
            'created_at': row['created_at'].isoformat(),
            'updated_at': row['updated_at'].isoformat()
        })
        
    except ValueError:
        return web.json_response({'error': 'Invalid template ID'}, status=400)
    except Exception as e:
        print(f"Error fetching template: {e}")
        return web.json_response({'error': str(e)}, status=500)


async def update_email_template(request):
    """Update email template"""
    try:
        template_id = int(request.match_info['id'])
        data = await request.json()
        
        # Check if template exists
        existing = await _pool.fetchrow(
            "SELECT id FROM email_templates WHERE id = $1",
            template_id
        )
        
        if not existing:
            return web.json_response({'error': 'Template not found'}, status=404)
        
        # Update fields
        await _pool.execute("""
            UPDATE email_templates
            SET subject = COALESCE($2, subject),
                html_body = COALESCE($3, html_body),
                text_body = COALESCE($4, text_body),
                description = COALESCE($5, description),
                updated_at = NOW()
            WHERE id = $1
        """, 
            template_id,
            data.get('subject'),
            data.get('html_body'),
            data.get('text_body'),
            data.get('description')
        )
        
        return web.json_response({'success': True})
        
    except ValueError:
        return web.json_response({'error': 'Invalid template ID'}, status=400)
    except Exception as e:
        print(f"Error updating template: {e}")
        return web.json_response({'error': str(e)}, status=500)


def init_api(pool, bot, admin_ids, run_matching, get_settings_fn, set_schedule_days_fn,
             set_schedule_time_fn, can_run_now_fn, log_run_start_fn, log_run_finish_fn, bot_token):
    global _pool, _bot, _admin_ids, _run_matching, _get_settings, _set_schedule_days
    global _set_schedule_time, _can_run_now, _log_run_start, _log_run_finish, BOT_TOKEN
    
    _pool = pool
    _bot = bot
    _admin_ids = admin_ids
    _run_matching = run_matching
    _get_settings = get_settings_fn
    _set_schedule_days = set_schedule_days_fn
    _set_schedule_time = set_schedule_time_fn
    _can_run_now = can_run_now_fn
    _log_run_start = log_run_start_fn
    _log_run_finish = log_run_finish_fn
    BOT_TOKEN = bot_token


async def create_app():
    app = web.Application(middlewares=[auth_middleware])
    
    # Routes
    app.router.add_get('/health', health)
    app.router.add_get('/api/stats', get_stats)
    app.router.add_get('/api/schedule', get_schedule)
    app.router.add_post('/api/schedule', update_schedule)
    app.router.add_post('/api/run-matching', run_matching_now)
    app.router.add_get('/api/run-history', get_run_history)
    app.router.add_get('/api/approvals', get_pending_approvals)
    app.router.add_post('/api/approvals/approve', approve_user)
    app.router.add_post('/api/approvals/reject', reject_user)
    app.router.add_get('/api/subscribers', get_subscribers)
    app.router.add_post('/api/subscribers/pause', pause_user_subscription)
    app.router.add_get('/api/events', get_events)
    app.router.add_post('/api/events', create_event)
    app.router.add_put('/api/events/{id}', update_event)
    app.router.add_delete('/api/events/{id}', delete_event)
    app.router.add_post('/api/events/{id}/broadcast', broadcast_event)
    app.router.add_post('/api/broadcast', send_broadcast)
    app.router.add_get('/api/mentors', get_mentors)
    app.router.add_get('/api/mentees', get_mentees)
    app.router.add_post('/api/mentors/assign', assign_mentor)
    app.router.add_post('/api/mentors/unassign', unassign_mentor)
    # Image upload routes
    app.router.add_post('/api/upload-image', upload_image)
    app.router.add_get('/uploads/{filename}', serve_upload)
    
    # Email Templates routes
    app.router.add_get('/api/email-templates', get_email_templates)
    app.router.add_get('/api/email-templates/{id}', get_email_template_by_id)
    app.router.add_put('/api/email-templates/{id}', update_email_template)
    
    # Serve webapp
    async def serve_webapp(request):
        try:
            with open('/app/webapp.html', 'r') as f:
                return web.Response(text=f.read(), content_type='text/html')
        except:
            return web.Response(text='webapp.html not found', status=404)
    
    app.router.add_get('/', serve_webapp)
    
    # CORS
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*")
    })
    
    for route in list(app.router.routes()):
        cors.add(route)
    
    return app