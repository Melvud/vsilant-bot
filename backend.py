import os
import asyncio
import logging
import hmac
import hashlib
import urllib.parse
import uuid
import mimetypes
import json
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
from asyncpg.exceptions import UniqueViolationError
from aiohttp import web

# -------------------------------------------------
# Config & logging
# -------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# ensure postgresql:// for asyncpg
_database_url = os.getenv("DATABASE_URL", "postgres://postgres:postgres@db:5432/phe")
DATABASE_URL = _database_url.replace("postgres://", "postgresql://", 1)

WEBAPP_PATH = os.getenv("WEBAPP_PATH", "/app/webapp.html")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/app/uploads")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTS = {
    "png", "jpg", "jpeg", "gif", "webp",
    "pdf", "doc", "docx", "ppt", "pptx"
}

logger.info(f"DEBUG_MODE: {DEBUG_MODE}")
logger.info(f"ADMIN_IDS: {ADMIN_IDS}")
logger.info(f"BOT_TOKEN: {'***' if BOT_TOKEN else 'NOT SET'}")
logger.info(f"DATABASE_URL: {DATABASE_URL}")

# -------------------------------------------------
# Helpers
# -------------------------------------------------

def _parse_init_data(raw: str) -> Dict[str, Any]:
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True, strict_parsing=False)
    return {k: v[0] for k, v in parsed.items()}

def _check_telegram_signature(init_data: str, token: str) -> Tuple[bool, Dict[str, Any]]:
    if not token:
        return True, {"id": 0}
    if not init_data:
        logger.warning("No init_data provided")
        return False, {}
    try:
        parsed = _parse_init_data(init_data or "")
        hash_hex = parsed.pop("hash", "")
        data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(computed, hash_hex)
        import json as _json
        user_json = parsed.get("user")
        user = _json.loads(user_json) if user_json else {}
        return ok, user
    except Exception as e:
        logger.warning("Failed to verify initData: %s", e)
        return False, {}

def _jsonify_record(record: asyncpg.Record) -> Dict[str, Any]:
    obj = dict(record)
    for k, v in list(obj.items()):
        if isinstance(v, (datetime, date)):
            obj[k] = v.isoformat()
    return obj

def _jsonify_records(records: List[asyncpg.Record]) -> List[Dict[str, Any]]:
    return [_jsonify_record(r) for r in records]

def _safe_filename(name: str) -> str:
    name = os.path.basename(name or "")
    return name.replace("\x00", "")[:200] or f"file-{uuid.uuid4().hex}"

def _ext_type(filename: str) -> Tuple[str, str]:
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if ext in {"png", "jpg", "jpeg", "gif", "webp"}:
        return "photo", mime
    return "document", mime

def _guess_type_from_file_id(file_id: str) -> str:
    """
    Определяем тип отправки боту по file_id.
    Если это локальный файл 'local:xxx.ext' — смотрим расширение.
    Иначе возвращаем 'document' по умолчанию (универсально).
    """
    if not file_id:
        return "document"
    # local:storedname.ext
    name_part = file_id.split(":", 1)[-1]
    ext = name_part.rsplit(".", 1)[-1].lower() if "." in name_part else ""
    if ext in {"png", "jpg", "jpeg", "gif", "webp"}:
        return "photo"
    return "document"

async def enqueue_action(action: str, data: Dict[str, Any]):
    """Файловая очередь для взаимодействия с ботом (/tmp/bot_queue.json)."""
    try:
        queue_file = "/tmp/bot_queue.json"
        queue: List[Dict[str, Any]] = []
        if os.path.exists(queue_file):
            try:
                with open(queue_file, "r", encoding="utf-8") as f:
                    queue = json.load(f)
            except Exception:
                queue = []
        queue.append({"action": action, "data": data, "timestamp": datetime.utcnow().isoformat()})
        with open(queue_file, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False)
        logger.info(f"Queued action '{action}'")
    except Exception as e:
        logger.error(f"Failed to enqueue {action}: {e}")

async def notify_bot_new_assignment(assignment_data: Dict[str, Any]):
    await enqueue_action("send_assignment_to_group", assignment_data)

# -------------------------------------------------
# Auth helpers & decorators
# -------------------------------------------------

async def _require_admin(request: web.Request) -> int:
    """
    Разрешаем:
      - аккаунты из ADMIN_IDS, всегда
      - (УДАЛЕНО) подтверждённых пользователей с ролью teacher (из БД)
    """
    init_data = (
        request.headers.get("X-Telegram-Init-Data") or
        request.headers.get("Telegram-Init-Data") or
        request.query.get("initData") or
        ""
    )

    # DEBUG: пропускаем и считаем учителем
    if DEBUG_MODE and not ADMIN_IDS:
        return 999_999

    ok, user = _check_telegram_signature(init_data, BOT_TOKEN)
    if not ok or not user:
        raise web.HTTPUnauthorized(text='{"detail":"Unauthorized"}', content_type="application/json")

    tg_id = int(user.get("id", 0)) if user.get("id") else 0
    if tg_id <= 0:
        raise web.HTTPUnauthorized(text='{"detail":"Unauthorized"}', content_type="application/json")

    # === ИСПРАВЛЕНИЕ: Оставляем проверку ТОЛЬКО по ADMIN_IDS ===
    if tg_id in ADMIN_IDS:
        return tg_id

    # иначе отказ
    raise web.HTTPUnauthorized(text='{"detail":"Unauthorized"}', content_type="application/json")


def admin_required(handler):
    async def wrapper(request: web.Request, *args, **kwargs):
        uid = await _require_admin(request)
        request["admin_user_id"] = uid
        return await handler(request, *args, **kwargs)
    return wrapper


# -------------------------------------------------
# App lifecycle
# -------------------------------------------------

async def create_pool(app: web.Application):
    logger.info("Connecting to DB: %s", DATABASE_URL)
    app["db_pool"] = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    yield
    await app["db_pool"].close()

# -------------------------------------------------
# Views
# -------------------------------------------------

@admin_required
async def health_handler(request: web.Request):
    return web.json_response({"ok": True, "time": datetime.utcnow().isoformat()})

async def index_handler(request: web.Request):
    if not os.path.exists(WEBAPP_PATH):
        return web.Response(status=404, text="webapp.html not found")
    return web.FileResponse(WEBAPP_PATH)

# ---------- /api/me ----------
async def me_handler(request: web.Request):
    init_data = (
        request.headers.get("X-Telegram-Init-Data") or
        request.headers.get("Telegram-Init-Data") or
        request.query.get("initData") or
        ""
    )
    if DEBUG_MODE and not init_data:
        return web.json_response({"approved": True, "role": "teacher"})

    ok, user = _check_telegram_signature(init_data, BOT_TOKEN)
    if not ok or not user:
        raise web.HTTPUnauthorized(text='{"detail":"Unauthorized"}', content_type="application/json")

    tg_id = int(user.get("id", 0)) if user.get("id") else 0
    if tg_id <= 0:
        raise web.HTTPUnauthorized(text='{"detail":"Unauthorized"}', content_type="application/json")

    # ADMIN_IDS => всегда teacher
    if tg_id in ADMIN_IDS:
        return web.json_response({"approved": True, "role": "teacher"})
    
    # Остальные: проверяем/создаём запись
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT approved, role FROM users WHERE telegram_id=$1", tg_id)
        if not row:
            await conn.execute(
                """
                INSERT INTO users(telegram_id, username, first_name, last_name, role, approved)
                VALUES($1,$2,$3,$4,'pending',FALSE)
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                tg_id, user.get("username"), user.get("first_name") or "", user.get("last_name") or ""
            )
            row = await conn.fetchrow("SELECT approved, role FROM users WHERE telegram_id=$1", tg_id)

    # Не выдаём роль teacher без ADMIN_IDS
    role_to_return = row["role"]
    if role_to_return == "teacher":
        role_to_return = "student"

    return web.json_response({"approved": bool(row["approved"]), "role": role_to_return})

# --------- Groups ----------

@admin_required
async def groups_get(request: web.Request):
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT group_id, name FROM groups ORDER BY name")
    return web.json_response(_jsonify_records(rows))

@admin_required
async def groups_post(request: web.Request):
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        raise web.HTTPBadRequest(text='{"detail":"Missing name"}', content_type="application/json")
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO groups(name) VALUES($1) RETURNING group_id, name", name
            )
        except UniqueViolationError:
            raise web.HTTPConflict(text='{"detail":"Group exists"}', content_type="application/json")
    return web.json_response(_jsonify_record(row), status=201)

@admin_required
async def group_delete(request: web.Request):
    gid = int(request.match_info["group_id"])
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        status = await conn.execute("DELETE FROM groups WHERE group_id=$1", gid)
    if status.startswith("DELETE"):
        return web.json_response({"success": True})
    raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")

# --------- Users ----------

@admin_required
async def users_get(request: web.Request):
    q = request.query
    role = q.get("role")
    gid = int(q["group_id"]) if q.get("group_id") else None
    approved = q.get("approved")
    approved_val: Optional[bool]
    if approved == "true":
        approved_val = True
    elif approved == "false":
        approved_val = False
    else:
        approved_val = None

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        base = """
            SELECT u.user_id, u.telegram_id, u.username, u.first_name, u.last_name,
                   u.role, u.approved,
                   s.group_id,
                   g.name AS group_name
            FROM users u
            LEFT JOIN students s ON s.student_id = u.user_id
            LEFT JOIN groups g ON g.group_id = s.group_id
            WHERE 1=1
        """
        args: List[Any] = []
        if role:
            args.append(role)
            base += f" AND u.role = ${len(args)}"
        if gid is not None:
            args.append(gid)
            base += f" AND s.group_id = ${len(args)}"
        if approved_val is not None:
            args.append(approved_val)
            base += f" AND u.approved = ${len(args)}"
        base += " ORDER BY u.last_name, u.first_name"
        rows = await conn.fetch(base, *args)
    return web.json_response(_jsonify_records(rows))

@admin_required
async def user_approve(request: web.Request):
    uid = int(request.match_info["user_id"])
    data = await request.json()
    approved = data.get("approved")
    if not isinstance(approved, bool):
        raise web.HTTPBadRequest(text='{"detail":"Bad status"}', content_type="application/json")

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        # узнаем текущий статус и telegram_id
        row = await conn.fetchrow(
            "SELECT approved, telegram_id FROM users WHERE user_id=$1",
            uid
        )
        if not row:
            raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")
        prev_approved = bool(row["approved"])
        student_tid = int(row["telegram_id"]) if row["telegram_id"] else None

        status = await conn.execute("UPDATE users SET approved=$1 WHERE user_id=$2", approved, uid)

    if not status.startswith("UPDATE"):
        raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")

    # если впервые одобрили — уведомим ученика и дадим кнопку "Открыть меню"
    if approved and not prev_approved and student_tid:
        await enqueue_action("notify_user_approval", {
            "student_telegram_id": student_tid,
            "button_text": "Открыть меню"
        })

    return web.json_response({"success": True})

@admin_required
async def user_role(request: web.Request):
    uid = int(request.match_info["user_id"])
    data = await request.json()
    role = data.get("role")
    if role not in ("student", "teacher", "pending"):
        raise web.HTTPBadRequest(text='{"detail":"Bad role"}', content_type="application/json")
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.execute("UPDATE users SET role=$1 WHERE user_id=$2", role, uid)
            if not status.startswith("UPDATE"):
                raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")
            if role == "student":
                await conn.execute(
                    "INSERT INTO students (student_id) VALUES ($1) ON CONFLICT (student_id) DO NOTHING",
                    uid,
                )
            else:
                await conn.execute("DELETE FROM students WHERE student_id=$1", uid)
    return web.json_response({"success": True})

@admin_required
async def user_group(request: web.Request):
    uid = int(request.match_info["user_id"])
    data = await request.json()
    gid = data.get("group_id")
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        if gid is not None:
            status = await conn.execute(
                "UPDATE students SET group_id=$1 WHERE student_id=$2",
                int(gid), uid
            )
            if status == "UPDATE 0":
                await conn.execute(
                    """
                    INSERT INTO students(student_id, group_id)
                    VALUES($1,$2)
                    ON CONFLICT (student_id) DO UPDATE SET group_id=EXCLUDED.group_id
                    """,
                    uid, int(gid)
                )
        else:
            await conn.execute("UPDATE students SET group_id=NULL WHERE student_id=$1", uid)
    return web.json_response({"success": True})

# --------- Pending Changes ----------

@admin_required
async def pending_group_changes_get(request: web.Request):
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.first_name, u.last_name,
                   s.group_id AS current_group_id, g1.name AS current_group_name,
                   s.pending_group_id, g2.name AS pending_group_name
            FROM users u
            JOIN students s ON s.student_id = u.user_id
            LEFT JOIN groups g1 ON g1.group_id = s.group_id
            LEFT JOIN groups g2 ON g2.group_id = s.pending_group_id
            WHERE s.pending_group_id IS NOT NULL
            ORDER BY u.last_name, u.first_name
            """
        )
    return web.json_response(_jsonify_records(rows))

@admin_required
async def approve_group_change(request: web.Request):
    uid = int(request.match_info["user_id"])
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT pending_group_id FROM students WHERE student_id=$1 FOR UPDATE", uid)
            if not row or row["pending_group_id"] is None:
                raise web.HTTPNotFound(text='{"detail":"No pending group"}', content_type="application/json")
            await conn.execute(
                """
                UPDATE students
                SET group_id=$1, pending_group_id=NULL, group_change_requested_at=NULL
                WHERE student_id=$2
                """,
                row["pending_group_id"], uid
            )
    return web.json_response({"success": True})

@admin_required
async def reject_group_change(request: web.Request):
    uid = int(request.match_info["user_id"])
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE students
            SET pending_group_id=NULL, name_change_requested_at=NULL, group_change_requested_at=NULL
            WHERE student_id=$1
            """,
            uid
        )
    return web.json_response({"success": True})

@admin_required
async def pending_name_changes_get(request: web.Request):
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id,
                   first_name AS current_first_name, last_name AS current_last_name,
                   pending_first_name, pending_last_name
            FROM users
            WHERE pending_first_name IS NOT NULL
            ORDER BY last_name, first_name
            """
        )
    return web.json_response(_jsonify_records(rows))

@admin_required
async def approve_name_change(request: web.Request):
    uid = int(request.match_info["user_id"])
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT pending_first_name, pending_last_name FROM users WHERE user_id=$1 FOR UPDATE",
                uid
            )
            if not row or not row["pending_first_name"]:
                raise web.HTTPNotFound(text='{"detail":"No pending name"}', content_type="application/json")
            await conn.execute(
                """
                UPDATE users
                SET first_name=$1, last_name=$2,
                    pending_first_name=NULL, pending_last_name=NULL, name_change_requested_at=NULL
                WHERE user_id=$3
                """,
                row["pending_first_name"], row["pending_last_name"], uid
            )
    return web.json_response({"success": True})

@admin_required
async def reject_name_change(request: web.Request):
    uid = int(request.match_info["user_id"])
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET pending_first_name=NULL, pending_last_name=NULL, name_change_requested_at=NULL
            WHERE user_id=$1
            """,
            uid
        )
    return web.json_response({"success": True})

# --------- Attendance (per-date input) ----------

@admin_required
async def attendance_get(request: web.Request):
    q = request.query
    try:
        gid = int(q["group_id"])
        adate = date.fromisoformat(q["date"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text='{"detail":"Bad or missing group_id/date"}', content_type="application/json")

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT attendance_id, group_id, student_id, attendance_date, is_present
            FROM attendance
            WHERE group_id=$1 AND attendance_date=$2
            ORDER BY student_id
            """,
            gid, adate
        )
    return web.json_response(_jsonify_records(rows))

@admin_required
async def attendance_post(request: web.Request):
    data = await request.json()
    try:
        gid = int(data["group_id"])
        adate = date.fromisoformat(data["date"])
    except Exception:
        raise web.HTTPBadRequest(text='{"detail":"Bad group_id/date"}', content_type="application/json")

    items: List[Dict[str, Any]] = data.get("attendance") or []
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM attendance WHERE group_id=$1 AND attendance_date=$2", gid, adate)
            for item in items:
                await conn.execute(
                    """
                    INSERT INTO attendance(group_id, student_id, attendance_date, is_present)
                    VALUES($1,$2,$3,$4)
                    """,
                    gid, int(item["student_id"]), adate, bool(item.get("is_present", True))
                )
    return web.json_response({"success": True})

# --------- Attendance: stats & tracked dates ----------

@admin_required
async def attendance_tracked_dates(request: web.Request):
    """Список дат, когда в группе реально велась отметка (без «пропущенных» дней)."""
    q = request.query
    try:
        gid = int(q["group_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text='{"detail":"Bad or missing group_id"}', content_type="application/json")

    date_from = q.get("date_from")
    date_to = q.get("date_to")

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        sql = "SELECT DISTINCT attendance_date FROM attendance WHERE group_id=$1"
        args: List[Any] = [gid]
        if date_from:
            sql += f" AND attendance_date >= ${len(args)+1}"
            args.append(date.fromisoformat(date_from))
        if date_to:
            sql += f" AND attendance_date <= ${len(args)+1}"
            args.append(date.fromisoformat(date_to))
        sql += " ORDER BY attendance_date DESC"
        rows = await conn.fetch(sql, *args)
    return web.json_response([r["attendance_date"].isoformat() for r in rows])

@admin_required
async def attendance_stats_group(request: web.Request):
    """
    По группе: для каждого студента — всего отмеченных занятий, сколько присутствовал,
    и процент присутствия. Учитываются только даты, по которым есть записи в таблице attendance.
    """
    q = request.query
    try:
        gid = int(q["group_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text='{"detail":"Bad or missing group_id"}', content_type="application/json")

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.first_name, u.last_name,
                   COALESCE(SUM(CASE WHEN a.is_present THEN 1 ELSE 0 END), 0) AS total_present,
                   COALESCE(COUNT(a.attendance_id), 0) AS total_tracked
            FROM students s
            JOIN users u ON u.user_id = s.student_id
            LEFT JOIN attendance a
                   ON a.group_id = s.group_id
                  AND a.student_id = s.student_id
            WHERE s.group_id = $1
            GROUP BY u.user_id, u.first_name, u.last_name
            ORDER BY u.last_name, u.first_name
            """,
            gid
        )
    result = []
    for r in rows:
        total_tracked = int(r["total_tracked"])
        total_present = int(r["total_present"])
        percent = (total_present / total_tracked * 100.0) if total_tracked > 0 else None
        result.append({
            "user_id": r["user_id"],
            "first_name": r["first_name"],
            "last_name": r["last_name"],
            "total_present": total_present,
            "total_tracked": total_tracked,
            "percent_present": percent,
        })
    return web.json_response(result)

@admin_required
async def attendance_stats_student(request: web.Request):
    """
    По студенту: список всех дат, когда велась отметка для его группы,
    и признак присутствия на каждой дате.
    """
    try:
        sid = int(request.match_info["student_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text='{"detail":"Bad or missing student_id"}', content_type="application/json")

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        # определим группу студента
        st = await conn.fetchrow("SELECT group_id FROM students WHERE student_id=$1", sid)
        if not st or st["group_id"] is None:
            return web.json_response([])

        gid = int(st["group_id"])
        rows = await conn.fetch(
            """
            SELECT attendance_date, is_present
            FROM attendance
            WHERE group_id=$1 AND student_id=$2
            ORDER BY attendance_date DESC
            """,
            gid, sid
        )
    return web.json_response([{"date": r["attendance_date"].isoformat(), "is_present": r["is_present"]} for r in rows])

# --------- Assignments & Submissions ----------

@admin_required
async def assignments_get(request: web.Request):
    q = request.query
    gid = int(q["group_id"])
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT assignment_id, group_id, title, description,
                   file_id, file_type, due_date, accepting_submissions
            FROM assignments
            WHERE group_id=$1
            ORDER BY assignment_id DESC
            """,
            gid
        )
    return web.json_response(_jsonify_records(rows))

@admin_required
async def assignment_detail(request: web.Request):
    aid = int(request.match_info["assignment_id"])
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT assignment_id, group_id, title, description,
                   file_id, file_type, due_date, accepting_submissions
            FROM assignments
            WHERE assignment_id=$1
            """,
            aid
        )
    if not row:
        raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")
    return web.json_response(_jsonify_record(row))

@admin_required
async def send_assignment(request: web.Request):
    data = await request.json()
    admin_telegram_id = request.get("admin_user_id")

    try:
        group_id = int(data["group_id"])
        title = (data.get("title") or "").strip()
    except (KeyError, ValueError, TypeError):
        raise web.HTTPBadRequest(text='{"detail":"Missing or invalid group_id/title"}', content_type="application/json")

    if not title:
        raise web.HTTPBadRequest(text='{"detail":"Title cannot be empty"}', content_type="application/json")

    description = (data.get("description") or "").strip() or None
    file_id = (data.get("file_id") or "").strip() or None
    file_type = (data.get("file_type") or "").strip() or None
    due_date_str = (data.get("due_date") or "").strip()

    due_date = None
    if due_date_str:
        try:
            due_date = datetime.fromisoformat(due_date_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError) as e:
            logger.warning(f"Invalid due_date format: {due_date_str}, error: {e}")
            due_date = None

    pool: asyncpg.Pool = request.app["db_pool"]

    try:
        async with pool.acquire() as conn:
            admin_user = await conn.fetchrow(
                "SELECT user_id FROM users WHERE telegram_id = $1",
                admin_telegram_id
            )

            if not admin_user:
                logger.warning(f"Admin {admin_telegram_id} not in DB, creating record")
                admin_user = await conn.fetchrow(
                    """
                    INSERT INTO users (telegram_id, first_name, last_name, role, approved)
                    VALUES ($1, 'Admin', 'User', 'teacher', TRUE)
                    RETURNING user_id
                    """,
                    admin_telegram_id
                )
                await conn.execute(
                    "INSERT INTO teachers (teacher_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    admin_user['user_id']
                )

            admin_user_id = admin_user['user_id']

            if due_date:
                row = await conn.fetchrow(
                    """
                    INSERT INTO assignments(group_id, title, description, file_id, file_type, due_date, created_by, accepting_submissions)
                    VALUES($1, $2, $3, $4, $5, $6, $7, TRUE)
                    RETURNING assignment_id, group_id, title, description, file_id, file_type, due_date, accepting_submissions
                    """,
                    group_id, title, description, file_id, file_type, due_date, admin_user_id
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO assignments(group_id, title, description, file_id, file_type, created_by, accepting_submissions)
                    VALUES($1, $2, $3, $4, $5, $6, TRUE)
                    RETURNING assignment_id, group_id, title, description, file_id, file_type, due_date, accepting_submissions
                    """,
                    group_id, title, description, file_id, file_type, admin_user_id
                )

        result = _jsonify_record(row)

        await notify_bot_new_assignment({
            'assignment_id': result['assignment_id'],
            'group_id': result['group_id'],
            'title': result['title'],
            'description': result.get('description'),
            'due_date': result.get('due_date'),
            'file_id': result.get('file_id'),
            'file_type': result.get('file_type')
        })

        return web.json_response(result, status=201)

    except Exception as e:
        logger.exception(f"Error creating assignment: {e}")
        raise web.HTTPInternalServerError(text='{"detail":"Failed to create assignment"}', content_type="application/json")

@admin_required
async def assignment_submissions(request: web.Request):
    aid = int(request.match_info["assignment_id"])

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT submission_id, assignment_id, student_id,
                   file_id, submission_date, is_late,
                   submitted, grade, score1, score2, teacher_comment, grade_date
            FROM submissions
            WHERE assignment_id=$1
            ORDER BY submission_date DESC
            """,
            aid
        )
    # добавляем вычисляемое поле is_graded
    out = []
    for r in rows:
        d = _jsonify_record(r)
        d["is_graded"] = (r["grade"] is not None) or (r["teacher_comment"] is not None) or (r["grade_date"] is not None)
        out.append(d)
    return web.json_response(out)

@admin_required
async def toggle_submission(request: web.Request):
    aid = int(request.match_info["assignment_id"])
    data = await request.json()
    accept = data.get("accept")
    if not isinstance(accept, bool):
        raise web.HTTPBadRequest(text='{"detail":"Bad status"}', content_type="application/json")
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE assignments
            SET accepting_submissions=$1
            WHERE assignment_id=$2
            RETURNING assignment_id, accepting_submissions
            """,
            accept, aid
        )
    if not row:
        raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")
    return web.json_response(_jsonify_record(row))

@admin_required
async def submission_grade(request: web.Request):
    sid = int(request.match_info()["submission_id"])
    data = await request.json()
    grade = data.get("grade")
    comment = data.get("comment")

    if grade is not None:
        try:
            gi = int(grade)
            if not (0 <= gi <= 20):
                raise ValueError
            grade = gi
        except Exception:
            raise web.HTTPBadRequest(text='{"detail":"Bad grade"}', content_type="application/json")

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        details = await conn.fetchrow(
            """
            SELECT s.assignment_id, s.student_id, u.telegram_id, a.title
            FROM submissions s
            JOIN users u ON u.user_id = s.student_id
            JOIN assignments a ON a.assignment_id = s.assignment_id
            WHERE s.submission_id=$1
            """, sid
        )
        if not details:
            raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")

        row = await conn.fetchrow(
            """
            UPDATE submissions
            SET grade=$1, teacher_comment=$2, grade_date=NOW()
            WHERE submission_id=$3
            RETURNING submission_id, grade, teacher_comment, grade_date
            """,
            grade, comment, sid
        )
    if not row:
        raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")

    # уведомим бота, чтобы он отослал студенту оценку
    await enqueue_action("send_grade_to_student", {
        "student_telegram_id": int(details["telegram_id"]) if details["telegram_id"] else None,
        "assignment_id": int(details["assignment_id"]),
        "assignment_title": details["title"],
        "submission_id": sid,
        "grade": row["grade"],
        "comment": row["teacher_comment"]
    })

    resp = _jsonify_record(row)
    resp["is_graded"] = True
    return web.json_response(resp)

@admin_required
async def resend_submission_to_admin(request: web.Request):
    """Повторная отправка файла работы админу (кнопка «Показать работу»)."""
    sid = int(request.match_info["submission_id"])
    admin_tid = request.get("admin_user_id")
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        sub = await conn.fetchrow(
            """
            SELECT s.submission_id, s.file_id, s.student_id,
                   u.first_name, u.last_name
            FROM submissions s
            JOIN users u ON u.user_id = s.student_id
            WHERE s.submission_id=$1
            """, sid
        )
    if not sub or not sub["file_id"]:
        raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")

    # Определяем тип файла для бота
    file_type = _guess_type_from_file_id(sub["file_id"])
    caption = f"📎 Работа студента: {sub['last_name']} {sub['first_name']} (сдача ID: {sid})"

    await enqueue_action("resend_submission_to_admin", {
        "admin_telegram_id": int(admin_tid),
        "file_id": sub["file_id"],
        "file_type": file_type,
        "caption": caption
    })
    return web.json_response({"success": True})

# --------- Questions ----------

@admin_required
async def questions_get(request: web.Request):
    q = request.query
    answered = (q.get("answered") or "all").lower()
    
    # === ИСПРАВЛЕНИЕ ЛОГИКИ ФИЛЬТРАЦИИ ===
    where = ""
    if answered == "false":
        where = "WHERE q.answer_text IS NULL"
    elif answered == "true":
         where = "WHERE q.answer_text IS NOT NULL"
    #
    
    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT q.question_id, q.student_id, q.group_id, q.question_text, q.asked_at,
                   q.answer_text, q.answered_by, q.answered_at,
                   u.first_name AS student_first_name, u.last_name AS student_last_name, u.telegram_id AS student_telegram_id,
                   g.name AS group_name,
                   ab.first_name || ' ' || ab.last_name AS answerer_name
            FROM questions q
            JOIN users u ON u.user_id = q.student_id
            LEFT JOIN groups g ON g.group_id = q.group_id
            LEFT JOIN users ab ON ab.user_id = q.answered_by
            {where}
            ORDER BY q.asked_at DESC
            """
        )
    return web.json_response(_jsonify_records(rows))

@admin_required
async def answer_question(request: web.Request):
    """
    Сохраняет ответ преподавателя и ставит задачу в очередь notify_answer,
    чтобы бот отправил ответ студенту с оригинальным вопросом.
    """
    qid = int(request.match_info["question_id"])
    uid = request["admin_user_id"]
    data = await request.json()
    answer_text = (data.get("answer_text") or "").strip()
    if not answer_text:
        raise web.HTTPBadRequest(text='{"detail":"Missing answer_text"}', content_type="application/json")

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        # гарантируем, что преподаватель есть в users (+teachers)
        admin_user = await conn.fetchrow("SELECT user_id FROM users WHERE telegram_id = $1", uid)
        if not admin_user:
            admin_user = await conn.fetchrow(
                """
                INSERT INTO users (telegram_id, first_name, last_name, role, approved)
                VALUES ($1, 'Admin', 'User', 'teacher', TRUE)
                RETURNING user_id
                """,
                uid
            )
            await conn.execute(
                "INSERT INTO teachers (teacher_id) VALUES ($1) ON CONFLICT DO NOTHING",
                admin_user['user_id']
            )
        admin_user_id = admin_user['user_id']

        # Обновляем ответ и забираем необходимые поля
        row = await conn.fetchrow(
            """
            UPDATE questions
            SET answer_text=$1, answered_by=$2, answered_at=NOW()
            WHERE question_id=$3
            RETURNING question_id, student_id, question_text, answer_text, answered_at
            """,
            answer_text, admin_user_id, qid
        )

        if not row:
            raise web.HTTPNotFound(text='{"detail":"Not found"}', content_type="application/json")

        # Получаем telegram_id студента
        st = await conn.fetchrow("SELECT telegram_id FROM users WHERE user_id=$1", row["student_id"])
        student_tid = int(st["telegram_id"]) if st and st["telegram_id"] else None

    # Ставим задачу на отправку ответа студенту
    if student_tid:
        await enqueue_action("notify_answer", {
            "student_telegram_id": student_tid,
            "question_text": row["question_text"],
            "answer_text": row["answer_text"],
        })

    return web.json_response(_jsonify_record(row))

# --------- Broadcasts (универсальные рассылки преподавателя) ----------

@admin_required
async def send_broadcast(request: web.Request):
    """
    Универсальная отправка сообщения по группе:
    {
      "group_id": 4,
      "title": "Объявление",
      "text": "Завтра консультация в 15:00",
      "file_id": "local:...." | "BQACAgQAAxkBA...",
      "file_type": "document" | "photo" | null
    }
    """
    data = await request.json()
    try:
        group_id = int(data["group_id"])
    except Exception:
        raise web.HTTPBadRequest(text='{"detail":"Missing/invalid group_id"}', content_type="application/json")

    title = (data.get("title") or "").strip() or None
    text = (data.get("text") or "").strip() or ""
    file_id = (data.get("file_id") or "").strip() or None
    file_type = (data.get("file_type") or "").strip() or None
    if not file_type and file_id:
        file_type = _guess_type_from_file_id(file_id)

    payload = {
        "group_id": group_id,
        "title": title,
        "text": text,
        "file_id": file_id,
        "file_type": file_type or "document"
    }
    await enqueue_action("send_broadcast_to_group", payload)
    return web.json_response({"success": True})


# =================================================================
# ===== ИСПРАВЛЕНИЕ: ПЕРЕМЕЩАЕМ ЭТИ ФУНКЦИИ ВВЕРХ ==================
# =================================================================

# --------- Materials ----------

@admin_required
async def materials_get(request: web.Request):
    q = request.query
    try:
        gid = int(q["group_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text='{"detail":"Bad or missing group_id"}', content_type="application/json")

    category = (q.get("category") or "").strip() or None

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        base = """
            SELECT
                m.material_id,
                m.group_id,
                m.category,
                m.title,
                m.description,
                m.created_at,
                COALESCE(f.cnt, 0) AS files_count,
                COALESCE(l.cnt, 0) AS links_count
            FROM materials m
            LEFT JOIN (
                SELECT material_id, COUNT(*) AS cnt
                FROM material_files
                GROUP BY material_id
            ) f ON f.material_id = m.material_id
            LEFT JOIN (
                SELECT material_id, COUNT(*) AS cnt
                FROM material_links
                GROUP BY material_id
            ) l ON l.material_id = m.material_id
            WHERE m.group_id = $1
        """
        args: List[Any] = [gid]
        if category:
            base += f" AND m.category = ${len(args)+1}"
            args.append(category)
        base += " ORDER BY m.material_id DESC"
        rows = await conn.fetch(base, *args)
    return web.json_response(_jsonify_records(rows))


@admin_required
async def send_material(request: web.Request):
    """
    Принимает:
    {
      group_id: int,
      category: str,
      title: str,
      description: str|null,
      links: [url, ...],
      files: [{file_id, file_type, name?, mime?}, ...],
      notify: bool,
      pin: bool
    }
    """
    data = await request.json()
    admin_telegram_id = request.get("admin_user_id")

    try:
        group_id = int(data["group_id"])
        category = (data.get("category") or "").strip()
        title = (data.get("title") or "").strip()
    except (KeyError, ValueError, TypeError):
        raise web.HTTPBadRequest(text='{"detail":"Missing or invalid group_id/category/title"}', content_type="application/json")

    if not title:
        raise web.HTTPBadRequest(text='{"detail":"Title cannot be empty"}', content_type="application/json")

    description = (data.get("description") or "").strip() or None
    links = data.get("links") or []
    files = data.get("files") or []
    notify = bool(data.get("notify", True))
    pin = bool(data.get("pin", False))

    pool: asyncpg.Pool = request.app["db_pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            # убеждаемся, что админ есть в БД (как и в send_assignment)
            admin_user = await conn.fetchrow(
                "SELECT user_id FROM users WHERE telegram_id = $1",
                admin_telegram_id
            )
            if not admin_user:
                admin_user = await conn.fetchrow(
                    """
                    INSERT INTO users (telegram_id, first_name, last_name, role, approved)
                    VALUES ($1, 'Admin', 'User', 'teacher', TRUE)
                    RETURNING user_id
                    """,
                    admin_telegram_id
                )
                await conn.execute(
                    "INSERT INTO teachers (teacher_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    admin_user['user_id']
                )
            admin_user_id = admin_user['user_id']

            # создаём запись материала
            mat_row = await conn.fetchrow(
                """
                INSERT INTO materials (group_id, category, title, description, created_by, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                RETURNING material_id, group_id, category, title, description, created_at
                """,
                group_id, category, title, description, admin_user_id
            )
            material_id = int(mat_row["material_id"])

            # сохраняем файлы (если есть)
            for f in files:
                fid = (f.get("file_id") or "").strip()
                ftype = (f.get("file_type") or "").strip() or _guess_type_from_file_id(fid)
                name = (f.get("name") or "").strip() or None
                mime = (f.get("mime") or "").strip() or None
                if not fid:
                    continue
                await conn.execute(
                    """
                    INSERT INTO material_files (material_id, file_id, file_type, name, mime)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    material_id, fid, ftype, name, mime
                )

            # сохраняем ссылки (если есть)
            for url in links:
                u = (url or "").strip()
                if not u:
                    continue
                await conn.execute(
                    "INSERT INTO material_links (material_id, url) VALUES ($1, $2)",
                    material_id, u
                )

    # нотификация бота (по аналогии с заданиями)
    if notify:
        try:
            await enqueue_action("send_material_to_group", {
                "group_id": group_id,
                "category": category,
                "title": title,
                "description": description,
                "links": links,
                "pin": pin,
                "files": files
            })
            logger.info("Material notification enqueued")
        except Exception:
            logger.exception("Failed to enqueue material notification")

    return web.json_response({"material_id": material_id, "success": True}, status=201)

# =================================================================
# ===== КОНЕЦ ПЕРЕМЕЩЕННОГО БЛОКА =================================
# =================================================================


# -------------------------------------------------
# Middlewares
# -------------------------------------------------

@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException as e:
        resp = web.json_response({"detail": str(e.text or e.reason)}, status=e.status)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp
    except Exception:
        logger.exception("Unhandled error on %s %s", request.method, request.path)
        resp = web.json_response({"detail": "Internal Server Error"}, status=500)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age": "3600",
            }
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response

# --------- Uploads ----------

@admin_required
async def upload_file(request: web.Request):
    ctype = request.headers.get("Content-Type", "")
    if not ctype.startswith("multipart/form-data"):
        return web.json_response({"detail": "Content-Type must be multipart/form-data"}, status=415)

    reader = await request.multipart()
    files_meta: List[Dict[str, Any]] = []

    try:
        while True:
            part = await reader.next()
            if part is None:
                break

            if not getattr(part, "filename", None):
                await part.read(decode=False)
                continue

            filename = _safe_filename(part.filename)
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in ALLOWED_EXTS:
                return web.json_response({"detail": f"File type .{ext} not allowed"}, status=400)

            stored_name = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
            dest_path = os.path.join(UPLOAD_DIR, stored_name)

            size = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)

            file_type, mime = _ext_type(filename)
            files_meta.append({
                "file_id": f"local:{stored_name}",
                "file_type": file_type,
                "name": filename,
                "stored_name": stored_name,
                "size": size,
                "mime": mime
            })

    except Exception:
        logger.exception("Upload parse error")
        return web.json_response({"detail": "Bad upload"}, status=400)

    if not files_meta:
        return web.json_response({"detail": "No file"}, status=400)

    resp: Dict[str, Any] = {"files": files_meta}
    if len(files_meta) == 1:
        resp.update({
            "file_id": files_meta[0]["file_id"],
            "file_type": files_meta[0]["file_type"]
        })
    return web.json_response(resp)

# -------------------------------------------------
# App wiring
# -------------------------------------------------

def make_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware, error_middleware])
    app.cleanup_ctx.append(create_pool)

    # Routes
    app.router.add_get("/health", health_handler)
    app.router.add_get("/", index_handler)
    app.router.add_get("/api/me", me_handler)

    app.router.add_get("/api/groups", groups_get)
    app.router.add_post("/api/groups", groups_post)
    app.router.add_delete("/api/groups/{group_id}", group_delete)

    app.router.add_get("/api/users", users_get)
    app.router.add_patch("/api/users/{user_id}/approve", user_approve)
    app.router.add_patch("/api/users/{user_id}/role", user_role)
    app.router.add_patch("/api/users/{user_id}/group", user_group)

    app.router.add_get("/api/group_changes/pending", pending_group_changes_get)
    app.router.add_post("/api/group_changes/{user_id}/approve", approve_group_change)
    app.router.add_post("/api/group_changes/{user_id}/reject", reject_group_change)

    app.router.add_get("/api/name_changes/pending", pending_name_changes_get)
    app.router.add_post("/api/name_changes/{user_id}/approve", approve_name_change)
    app.router.add_post("/api/name_changes/{user_id}/reject", reject_name_change)

    app.router.add_get("/api/attendance", attendance_get)
    app.router.add_post("/api/attendance", attendance_post)
    app.router.add_get("/api/attendance/tracked_dates", attendance_tracked_dates)
    app.router.add_get("/api/attendance/stats/group", attendance_stats_group)
    app.router.add_get("/api/attendance/stats/student/{student_id}", attendance_stats_student)

    app.router.add_get("/api/assignments", assignments_get)
    app.router.add_post("/api/assignments/send", send_assignment)
    app.router.add_get("/api/assignments/{assignment_id}", assignment_detail)
    app.router.add_get("/api/assignments/{assignment_id}/submissions", assignment_submissions)
    app.router.add_post("/api/assignments/{assignment_id}/toggle_submission", toggle_submission)

    app.router.add_patch("/api/submissions/{submission_id}/grade", submission_grade)
    app.router.add_post("/api/submissions/{submission_id}/resend_to_admin", resend_submission_to_admin)

    app.router.add_get("/api/questions", questions_get)
    app.router.add_post("/api/questions/{question_id}/answer", answer_question)

    app.router.add_post("/api/broadcasts/send", send_broadcast)

    app.router.add_post("/api/upload_file", upload_file)
    
    # Материалы — функции определены выше (исправление порядка)
    app.router.add_get("/api/materials", materials_get)
    app.router.add_post("/api/materials/send", send_material)

    return app


async def main_backend():
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    logger.info("Backend running on 0.0.0.0:8080")
    await site.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down backend")

if __name__ == "__main__":
    asyncio.run(main_backend())
