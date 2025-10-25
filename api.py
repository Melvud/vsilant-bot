import asyncpg
from typing import Optional, Dict, List, Any, Union
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)

def _record_to_dict(record: Optional[asyncpg.Record]) -> Optional[Dict]:
    return dict(record) if record else None

def _records_to_list_dicts(records: List[asyncpg.Record]) -> List[Dict]:
    return [dict(record) for record in records]

async def add_group(conn: asyncpg.Connection, name: str) -> Dict:
    query = "INSERT INTO groups (name) VALUES ($1) RETURNING group_id, name"
    try:
        result = await conn.fetchrow(query, name)
        return _record_to_dict(result)
    except asyncpg.UniqueViolationError: raise

async def get_groups(conn: asyncpg.Connection) -> List[Dict]:
    query = "SELECT group_id, name FROM groups ORDER BY name"
    results = await conn.fetch(query)
    return _records_to_list_dicts(results)

async def delete_group(conn: asyncpg.Connection, group_id: int) -> bool:
    query = "DELETE FROM groups WHERE group_id = $1 RETURNING group_id"
    result = await conn.fetchval(query, group_id)
    return result is not None

async def get_group_by_id(conn: asyncpg.Connection, group_id: int) -> Optional[Dict]:
    query = "SELECT group_id, name FROM groups WHERE group_id = $1"
    result = await conn.fetchrow(query, group_id)
    return _record_to_dict(result)

async def add_user(conn: asyncpg.Connection, user_data: Dict) -> Dict:
    query = """
    INSERT INTO users (telegram_id, username, first_name, last_name, role, approved)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (telegram_id) DO UPDATE SET
        username = EXCLUDED.username, first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name
    RETURNING user_id, telegram_id, username, first_name, last_name, role, approved;
    """
    result = await conn.fetchrow(
        query, user_data['telegram_id'], user_data.get('username'),
        user_data.get('first_name'), user_data.get('last_name'), 'pending', False
    )
    if not result: return await get_user(conn, user_data['telegram_id'])
    return _record_to_dict(result)

async def add_student(conn: asyncpg.Connection, user_id: int, group_id: Optional[int]):
    query = """
    INSERT INTO students (student_id, group_id, pending_group_id, group_change_requested_at)
    VALUES ($1, $2, NULL, NULL) ON CONFLICT (student_id) DO NOTHING;
    """
    await conn.execute(query, user_id, group_id)

async def add_teacher(conn: asyncpg.Connection, user_id: int):
    query = "INSERT INTO teachers (teacher_id) VALUES ($1) ON CONFLICT (teacher_id) DO NOTHING"
    await conn.execute(query, user_id)

async def get_user(conn: asyncpg.Connection, telegram_id: int) -> Optional[Dict]:
    query = """
    SELECT u.*, s.group_id, g.name AS group_name, s.pending_group_id, pg.name as pending_group_name,
           s.group_change_requested_at, u.pending_first_name, u.pending_last_name, u.name_change_requested_at
    FROM users u
    LEFT JOIN students s ON u.user_id = s.student_id
    LEFT JOIN groups g ON s.group_id = g.group_id
    LEFT JOIN groups pg ON s.pending_group_id = pg.group_id
    WHERE u.telegram_id = $1
    """
    result = await conn.fetchrow(query, telegram_id)
    return _record_to_dict(result)

async def get_user_by_db_id(conn: asyncpg.Connection, user_id: int) -> Optional[Dict]:
    query = """
    SELECT u.*, s.group_id, g.name AS group_name, s.pending_group_id, pg.name as pending_group_name,
           s.group_change_requested_at, u.pending_first_name, u.pending_last_name, u.name_change_requested_at
    FROM users u
    LEFT JOIN students s ON u.user_id = s.student_id
    LEFT JOIN groups g ON s.group_id = g.group_id
    LEFT JOIN groups pg ON s.pending_group_id = pg.group_id
    WHERE u.user_id = $1
    """
    result = await conn.fetchrow(query, user_id)
    return _record_to_dict(result)

async def set_student_group(conn: asyncpg.Connection, student_user_id: int, group_id: Optional[int]) -> bool:
    query = "UPDATE students SET group_id = $1, pending_group_id = NULL, group_change_requested_at = NULL WHERE student_id = $2 RETURNING student_id;"
    result = await conn.fetchval(query, group_id, student_user_id)
    return result is not None

async def request_group_change(conn: asyncpg.Connection, student_user_id: int, new_group_id: int) -> bool:
    query = "UPDATE students SET pending_group_id = $1, group_change_requested_at = CURRENT_TIMESTAMP WHERE student_id = $2 RETURNING student_id;"
    result = await conn.fetchval(query, new_group_id, student_user_id)
    return result is not None

async def get_pending_group_changes(conn: asyncpg.Connection) -> List[Dict]:
    query = """
    SELECT u.user_id, u.first_name, u.last_name, s.group_id, cg.name AS current_group_name,
           s.pending_group_id, pg.name AS pending_group_name, s.group_change_requested_at
    FROM users u JOIN students s ON u.user_id = s.student_id
    LEFT JOIN groups cg ON s.group_id = cg.group_id
    JOIN groups pg ON s.pending_group_id = pg.group_id
    WHERE s.pending_group_id IS NOT NULL ORDER BY s.group_change_requested_at ASC;
    """
    return _records_to_list_dicts(await conn.fetch(query))

async def approve_group_change(conn: asyncpg.Connection, student_user_id: int) -> bool:
    async with conn.transaction():
        pid = await conn.fetchval("SELECT pending_group_id FROM students WHERE student_id = $1 AND pending_group_id IS NOT NULL FOR UPDATE", student_user_id)
        if pid is None: return False
        status = await conn.execute("UPDATE students SET group_id = $1, pending_group_id = NULL, group_change_requested_at = NULL WHERE student_id = $2;", pid, student_user_id)
        return status == 'UPDATE 1'

async def reject_group_change(conn: asyncpg.Connection, student_user_id: int) -> bool:
    query = "UPDATE students SET pending_group_id = NULL, group_change_requested_at = NULL WHERE student_id = $1 AND pending_group_id IS NOT NULL RETURNING student_id;"
    result = await conn.fetchval(query, student_user_id)
    return result is not None

async def request_name_change(conn: asyncpg.Connection, user_id: int, first_name: str, last_name: str) -> bool:
    query = "UPDATE users SET pending_first_name = $1, pending_last_name = $2, name_change_requested_at = CURRENT_TIMESTAMP WHERE user_id = $3 RETURNING user_id;"
    result = await conn.fetchval(query, first_name, last_name, user_id)
    return result is not None

async def get_pending_name_changes(conn: asyncpg.Connection) -> List[Dict]:
    query = """
    SELECT user_id, first_name AS current_first_name, last_name AS current_last_name,
           pending_first_name, pending_last_name, name_change_requested_at
    FROM users WHERE pending_first_name IS NOT NULL ORDER BY name_change_requested_at ASC;
    """
    return _records_to_list_dicts(await conn.fetch(query))

async def approve_name_change(conn: asyncpg.Connection, user_id: int) -> bool:
     async with conn.transaction():
        pending_names = await conn.fetchrow("SELECT pending_first_name, pending_last_name FROM users WHERE user_id = $1 AND pending_first_name IS NOT NULL FOR UPDATE", user_id)
        if not pending_names: return False
        status = await conn.execute("UPDATE users SET first_name = $1, last_name = $2, pending_first_name = NULL, pending_last_name = NULL, name_change_requested_at = NULL WHERE user_id = $3;",
                                    pending_names['pending_first_name'], pending_names['pending_last_name'], user_id)
        return status == 'UPDATE 1'

async def reject_name_change(conn: asyncpg.Connection, user_id: int) -> bool:
    query = "UPDATE users SET pending_first_name = NULL, pending_last_name = NULL, name_change_requested_at = NULL WHERE user_id = $1 AND pending_first_name IS NOT NULL RETURNING user_id;"
    result = await conn.fetchval(query, user_id)
    return result is not None

async def update_user_name(conn: asyncpg.Connection, user_id: int, first_name: str, last_name: str) -> bool:
    # Используется только при одобрении запроса
    query = "UPDATE users SET first_name = $1, last_name = $2 WHERE user_id = $3"
    status = await conn.execute(query, first_name, last_name, user_id)
    return status == 'UPDATE 1'

async def add_assignment_and_notify_bot(conn: asyncpg.Connection, group_id: int, title: str, created_by: int, description: Optional[str]=None, due_date: Optional[datetime]=None, file_id: Optional[str]=None, file_type: Optional[str]=None) -> Dict:
    query = """
    INSERT INTO assignments (group_id, title, description, due_date, created_by, file_id, file_type)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    RETURNING assignment_id, group_id, title, description, due_date, file_id, file_type;
    """
    result = await conn.fetchrow(query, group_id, title, description, due_date, created_by, file_id, file_type)
    return _record_to_dict(result)

async def get_assignment(conn: asyncpg.Connection, assignment_id: int) -> Optional[Dict]:
    query = "SELECT *, (due_date < CURRENT_TIMESTAMP) as is_deadline_passed FROM assignments WHERE assignment_id = $1"
    result = await conn.fetchrow(query, assignment_id)
    return _record_to_dict(result)

async def get_assignments_for_group(conn: asyncpg.Connection, group_id: int) -> List[Dict]:
    query = "SELECT assignment_id, title, due_date, accepting_submissions FROM assignments WHERE group_id = $1 ORDER BY creation_date DESC"
    results = await conn.fetch(query, group_id)
    return _records_to_list_dicts(results)

async def toggle_accept_submissions(conn: asyncpg.Connection, assignment_id: int, accept: bool) -> Optional[Dict]:
    query = "UPDATE assignments SET accepting_submissions = $1 WHERE assignment_id = $2 RETURNING assignment_id, accepting_submissions;"
    result = await conn.fetchrow(query, accept, assignment_id)
    return _record_to_dict(result)

async def add_submission(conn: asyncpg.Connection, assignment_id: int, student_user_id: int, file_id: str) -> Dict:
    async with conn.transaction():
        assignment = await conn.fetchrow("SELECT due_date FROM assignments WHERE assignment_id = $1 FOR UPDATE", assignment_id)
        due_date = assignment['due_date'] if assignment else None
        is_late = due_date is not None and datetime.now(due_date.tzinfo) > due_date

        query = """
        INSERT INTO submissions (assignment_id, student_id, file_id, submission_date, is_late, submitted, grade, score1, score2, graded_by, grade_date, teacher_comment)
        VALUES ($1, $2, $3, CURRENT_TIMESTAMP, $4, TRUE, NULL, NULL, NULL, NULL, NULL, NULL)
        ON CONFLICT (assignment_id, student_id) DO UPDATE SET
            file_id = EXCLUDED.file_id, submission_date = CURRENT_TIMESTAMP, is_late = EXCLUDED.is_late, submitted = TRUE,
            grade = NULL, score1 = NULL, score2 = NULL, graded_by = NULL, grade_date = NULL, teacher_comment = NULL
        RETURNING submission_id, assignment_id, student_id, submission_date, is_late;
        """
        result = await conn.fetchrow(query, assignment_id, student_user_id, file_id, is_late)
        return _record_to_dict(result)

async def get_submissions_for_assignment(conn: asyncpg.Connection, assignment_id: int) -> List[Dict]:
    query = """
    SELECT s.*, u.first_name AS student_first_name, u.last_name AS student_last_name
    FROM submissions s JOIN users u ON s.student_id = u.user_id
    WHERE s.assignment_id = $1 ORDER BY s.submission_date DESC;
    """
    return _records_to_list_dicts(await conn.fetch(query, assignment_id))

async def update_submission_grade(conn: asyncpg.Connection, submission_id: int, grade: Optional[int], teacher_user_id: int, comment: Optional[str]=None, score1: Optional[int]=None, score2: Optional[int]=None) -> Optional[Dict]:
    query = """
    UPDATE submissions SET grade = $1, score1 = $2, score2 = $3, graded_by = $4, teacher_comment = $5,
           grade_date = CASE WHEN $1 IS NOT NULL THEN CURRENT_TIMESTAMP ELSE NULL END
    WHERE submission_id = $6 RETURNING submission_id, grade, score1, score2, grade_date;
    """
    result = await conn.fetchrow(query, grade, score1, score2, teacher_user_id, comment, submission_id)
    return _record_to_dict(result)

async def get_attendance(conn: asyncpg.Connection, group_id: int, attendance_date: date) -> List[Dict]:
    query = "SELECT student_id, is_present FROM attendance WHERE group_id = $1 AND attendance_date = $2;"
    return _records_to_list_dicts(await conn.fetch(query, group_id, attendance_date))

async def save_attendance(conn: asyncpg.Connection, group_id: int, attendance_date: date, attendance_list: List[Dict], marked_by: int):
    query = """
    INSERT INTO attendance (group_id, student_id, attendance_date, is_present, marked_by, marked_at)
    VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
    ON CONFLICT (group_id, student_id, attendance_date) DO UPDATE SET
        is_present = EXCLUDED.is_present, marked_by = EXCLUDED.marked_by, marked_at = CURRENT_TIMESTAMP;
    """
    await conn.executemany(query, [
        (group_id, att['student_id'], attendance_date, att['is_present'], marked_by)
        for att in attendance_list
    ])

async def add_question(conn: asyncpg.Connection, student_id: int, group_id: Optional[int], question_text: str) -> Dict:
    query = """
    INSERT INTO questions (student_id, group_id, question_text) VALUES ($1, $2, $3)
    RETURNING question_id;
    """
    result = await conn.fetchrow(query, student_id, group_id, question_text)
    return _record_to_dict(result)

async def get_questions(conn: asyncpg.Connection, answered: Optional[bool]=False) -> List[Dict]:
    base_query = """
    SELECT q.*, s.first_name AS student_first_name, s.last_name AS student_last_name, g.name AS group_name,
           a.first_name AS answerer_first_name, a.last_name AS answerer_last_name, s.telegram_id AS student_telegram_id
    FROM questions q JOIN users s ON q.student_id = s.user_id
    LEFT JOIN groups g ON q.group_id = g.group_id
    LEFT JOIN users a ON q.answered_by = a.user_id
    """
    conditions = []
    params = []
    if answered is False: conditions.append("q.answered_at IS NULL")
    elif answered is True: conditions.append("q.answered_at IS NOT NULL")

    if conditions: base_query += " WHERE " + " AND ".join(conditions)
    base_query += " ORDER BY q.asked_at DESC;"

    return _records_to_list_dicts(await conn.fetch(base_query, *params))

async def add_answer(conn: asyncpg.Connection, question_id: int, answer_text: str, teacher_user_id: int) -> Optional[Dict]:
    query = """
    UPDATE questions SET answer_text = $1, answered_by = $2, answered_at = CURRENT_TIMESTAMP
    WHERE question_id = $3 AND answered_at IS NULL
    RETURNING question_id, student_id, question_text, answer_text;
    """
    result = await conn.fetchrow(query, answer_text, teacher_user_id, question_id)
    return _record_to_dict(result)

async def get_users(conn: asyncpg.Connection, approved: Optional[bool]=None, role: Optional[str]=None, group_id: Optional[int]=None) -> List[Dict]:
    base_query = """
    SELECT u.*, s.group_id, g.name AS group_name, s.pending_group_id, pg.name as pending_group_name,
           s.group_change_requested_at, u.pending_first_name, u.pending_last_name, u.name_change_requested_at
    FROM users u LEFT JOIN students s ON u.user_id = s.student_id
    LEFT JOIN groups g ON s.group_id = g.group_id LEFT JOIN groups pg ON s.pending_group_id = pg.group_id
    """
    conditions = []; params = []; idx = 1
    if approved is not None: conditions.append(f"u.approved = ${idx}"); params.append(approved); idx += 1
    if role is not None: conditions.append(f"u.role = ${idx}"); params.append(role); idx += 1
    if group_id is not None: conditions.append(f"s.group_id = ${idx}"); params.append(group_id); idx += 1
    if conditions: query = f"{base_query} WHERE {' AND '.join(conditions)}"
    else: query = base_query
    query += " ORDER BY u.last_name, u.first_name;"
    return _records_to_list_dicts(await conn.fetch(query, *params))

async def set_user_approved(conn: asyncpg.Connection, user_id: int, status: bool) -> Optional[Dict]:
    query = "UPDATE users SET approved = $1 WHERE user_id = $2 RETURNING user_id, approved;"
    result = await conn.fetchrow(query, status, user_id)
    return _record_to_dict(result)

async def set_user_role(conn: asyncpg.Connection, user_id: int, role: str) -> Optional[Dict]:
    allowed = ('student', 'teacher', 'pending')
    if role not in allowed: raise ValueError(f"Bad role: {role}")
    async with conn.transaction():
        res = await conn.fetchrow("UPDATE users SET role = $1 WHERE user_id = $2 RETURNING user_id, role;", role, user_id)
        if not res: return None
        if role == 'student':
            await conn.execute("DELETE FROM teachers WHERE teacher_id = $1", user_id)
            await conn.execute("INSERT INTO students (student_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        elif role == 'teacher':
            await conn.execute("DELETE FROM students WHERE student_id = $1", user_id)
            await conn.execute("INSERT INTO teachers (teacher_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        else:
            await conn.execute("DELETE FROM students WHERE student_id = $1", user_id)
            await conn.execute("DELETE FROM teachers WHERE teacher_id = $1", user_id)
    return _record_to_dict(res)

async def get_teachers_ids(db_pool: asyncpg.Pool) -> List[int]:
    query = "SELECT u.telegram_id FROM users u JOIN teachers t ON u.user_id = t.teacher_id WHERE u.approved = TRUE;"
    async with db_pool.acquire() as conn:
        results = await conn.fetch(query)
    return [r['telegram_id'] for r in results]

async def get_grades_for_student(conn: asyncpg.Connection, student_user_id: int) -> List[Dict]:
    query = """
    SELECT s.submission_id, a.title AS assignment_title, s.grade, s.teacher_comment
    FROM submissions s JOIN assignments a ON s.assignment_id = a.assignment_id
    WHERE s.student_id = $1 ORDER BY a.creation_date DESC, s.submission_date DESC;
    """
    return _records_to_list_dicts(await conn.fetch(query, student_user_id))