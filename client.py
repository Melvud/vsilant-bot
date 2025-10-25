import asyncio
import json
import logging
import os
from contextlib import suppress
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import asyncpg
from aiogram import Bot, Dispatcher, F, Router, html, types
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.middleware.base import BaseMiddleware
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
                           User, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.utils.media_group import MediaGroupBuilder
from dotenv import load_dotenv

import api

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com/webapp.html")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

class Registration(StatesGroup):
    getting_first_name = State()
    getting_last_name = State()
    choosing_group = State()

class StudentActions(StatesGroup):
    submitting_assignment_file = State()
    changing_group = State()
    asking_question = State()
    changing_name_first = State()
    changing_name_last = State()

main_router = Router()
registration_router = Router()
student_router = Router()
teacher_router = Router()

db_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    global db_pool
    if db_pool is None:
        try:
            db_pool = await asyncpg.create_pool(DATABASE_URL, max_size=20)
            logger.info("Пул БД создан")
        except Exception as e:
            logger.critical(f"Ошибка пула БД: {e}", exc_info=True)
            raise
    return db_pool

class ApprovalMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Union[Message, CallbackQuery],
        data: Dict[str, Any],
    ) -> Any:
        user: Optional[User] = data.get("event_from_user")
        if not user: return await handler(event, data)

        pool = await get_db_pool()
        state: Optional[FSMContext] = data.get("state")
        current_state_str = await state.get_state() if state else None

        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        if current_state_str and current_state_str.startswith(Registration.__name__):
            return await handler(event, data)

        async with pool.acquire() as conn:
            db_user = await api.get_user(conn, user.id)

        if not db_user:
            msg_text = "Вы не зарегистрированы. Используйте /start"
            if isinstance(event, Message): await event.answer(msg_text)
            elif isinstance(event, CallbackQuery): await event.answer(msg_text, show_alert=True)
            return

        if not db_user.get('approved'):
            msg_text = "⏳ Аккаунт ожидает подтверждения."
            if isinstance(event, Message): await event.answer(msg_text)
            elif isinstance(event, CallbackQuery): await event.answer(msg_text, show_alert=True)
            return

        data["user_db_id"] = db_user.get('user_id')
        data["user_role"] = db_user.get('role')
        data["user_info"] = db_user

        return await handler(event, data)

async def get_groups_keyboard(db_pool: asyncpg.Pool, purpose: str = "register") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    async with db_pool.acquire() as conn: groups = await api.get_groups(conn)
    if groups:
        for group in groups:
            prefix = "register_group_" if purpose == "register" else "change_to_group_"
            builder.button(text=group['name'], callback_data=f"{prefix}{group['group_id']}")
        builder.adjust(2)
    return builder.as_markup()

def get_student_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📝 Сдать задание")
    builder.button(text="❓ Задать вопрос (Q&A)")
    builder.button(text="📊 Мои Оценки")
    builder.button(text="🔄 Сменить группу")
    builder.button(text="👤 Сменить имя")
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Действие:")

def get_teacher_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="⚙️ Админ-панель", web_app=WebAppInfo(url=WEBAPP_URL))
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Админ-панель:")

@main_router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    await state.clear()
    pool = await get_db_pool()
    async with pool.acquire() as conn: user = await api.get_user(conn, message.from_user.id)
    if user:
        name = user.get('first_name', 'Пользователь')
        if user.get('approved'):
            role = user.get('role', 'pending')
            if role == 'student':
                 group = user.get('group_name')
                 greet = f"С возвращением, {html.bold(name)}! 🎓" + (f"\nГруппа: {html.bold(html.quote(group))}" if group else "\nВы не в группе.")
                 await message.answer(greet, reply_markup=get_student_main_keyboard(), parse_mode=ParseMode.HTML)
            elif role == 'teacher':
                await message.answer(f"Здравствуйте, {html.bold(name)}! 🧑‍🏫", reply_markup=get_teacher_main_keyboard(), parse_mode=ParseMode.HTML)
            else: await message.answer(f"Здравствуйте, {html.bold(name)}! ⏳ Ожидайте подтверждения.", parse_mode=ParseMode.HTML)
        else: await message.answer(f"Здравствуйте, {html.bold(name)}! ⏳ Ваш аккаунт ожидает подтверждения.", parse_mode=ParseMode.HTML)
    else:
        await message.answer("Добро пожаловать! Введите **Имя**:", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
        await state.set_state(Registration.getting_first_name)

@registration_router.message(Registration.getting_first_name, F.text)
async def process_first_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) > 100: await message.reply("Некорректное имя."); return
    await state.update_data(first_name=name)
    await message.answer(f"Отлично, {html.bold(name)}! Введите **Фамилию**:", parse_mode=ParseMode.HTML)
    await state.set_state(Registration.getting_last_name)

@registration_router.message(Registration.getting_last_name, F.text)
async def process_last_name(message: Message, state: FSMContext, db_pool: asyncpg.Pool):
    name = message.text.strip()
    if not name or len(name) > 100: await message.reply("Некорректная фамилия."); return
    await state.update_data(last_name=name)
    kbd = await get_groups_keyboard(db_pool, "register")
    if not kbd.inline_keyboard:
         await message.answer("Групп нет. Регистрация без группы.", reply_markup=ReplyKeyboardRemove())
         user_data = await state.get_data()
         user_info = {'telegram_id': message.from_user.id, 'username': message.from_user.username, 'first_name': user_data.get('first_name'), 'last_name': name}
         try:
            async with db_pool.acquire() as conn, conn.transaction():
                db_user = await api.add_user(conn, user_info)
                await api.add_student(conn, db_user['user_id'], group_id=None)
            await message.answer(f"✅ Готово! Ожидайте подтверждения.", parse_mode=ParseMode.HTML)
         except Exception as e: logger.error(f"Reg err (no group): {e}"); await message.answer("❌ Ошибка /start.")
         finally: await state.clear()
    else:
        await message.answer("Выберите **группу**:", reply_markup=kbd, parse_mode=ParseMode.MARKDOWN)
        await state.set_state(Registration.choosing_group)

@registration_router.callback_query(Registration.choosing_group, F.data.startswith("register_group_"))
async def process_group_choice(query: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool):
    group_id = int(query.data.split("_")[-1])
    user_data = await state.get_data()
    user_info = {'telegram_id': query.from_user.id, 'username': query.from_user.username, 'first_name': user_data.get('first_name'), 'last_name': user_data.get('last_name')}
    group_name = "?"
    try:
        async with db_pool.acquire() as conn:
            group_rec = await api.get_group_by_id(conn, group_id);
            if group_rec: group_name = group_rec['name']
            async with conn.transaction():
                db_user = await api.add_user(conn, user_info)
                await api.add_student(conn, db_user['user_id'], group_id=group_id)
        await query.message.edit_text(f"✅ Готово! Группа: **{html.quote(group_name)}**.\n⏳ Ожидайте подтверждения.", parse_mode=ParseMode.HTML)
        await query.message.answer("Данные приняты.", reply_markup=ReplyKeyboardRemove())
    except asyncpg.exceptions.ForeignKeyViolationError: await query.message.edit_text("❌ Группа не найдена. /start")
    except Exception as e: logger.error(f"Reg err (group {group_id}): {e}"); await query.message.edit_text("❌ Ошибка /start.")
    finally: await state.clear(); await query.answer()

@main_router.message(Command("menu"))
async def show_menu(message: Message, user_role: str, user_info: dict):
     kbd = get_teacher_main_keyboard() if user_role == 'teacher' else get_student_main_keyboard()
     text = "Админ-меню:" if user_role == 'teacher' else "Меню:"
     if user_role == 'student' and user_info.get('group_name'): text += f"\nГруппа: {html.bold(html.quote(user_info['group_name']))}"
     await message.answer(text, reply_markup=kbd, parse_mode=ParseMode.HTML)

async def get_assignments_for_student_keyboard(db_pool: asyncpg.Pool, user_info: dict) -> InlineKeyboardMarkup:
     builder = InlineKeyboardBuilder()
     group_id = user_info.get('group_id')
     assignments = []
     if group_id:
         async with db_pool.acquire() as conn: assignments = await api.get_assignments_for_group(conn, group_id)
     if assignments:
         for a in assignments: builder.button(text=a['title'], callback_data=f"view_assignment_{a['assignment_id']}")
         builder.adjust(1)
     return builder.as_markup()

@student_router.message(F.text == "📝 Сдать задание")
@student_router.message(Command("submit"))
async def submit_assignment_list(message: Message, user_role: str, user_info: dict, db_pool: asyncpg.Pool):
    if user_role != 'student': return
    if not user_info.get('group_id'): await message.answer("Вы не в группе."); return
    kbd = await get_assignments_for_student_keyboard(db_pool, user_info)
    if kbd.inline_keyboard: await message.answer("Выберите задание:", reply_markup=kbd)
    else: await message.answer("Заданий нет.")

@student_router.callback_query(F.data.startswith("view_assignment_"))
async def student_view_assignment(query: CallbackQuery, user_role: str, state: FSMContext, db_pool: asyncpg.Pool):
    if user_role != 'student': return
    assignment_id = int(query.data.split("_")[-1])
    async with db_pool.acquire() as conn: a = await api.get_assignment(conn, assignment_id)
    if not a: await query.answer("Задание?", show_alert=True); return

    text = f"📝 **{html.quote(a['title'])}**\n\n"
    if a.get('description'): text += f"{html.quote(a['description'])}\n\n"
    deadline_text = "Нет"
    if a.get('due_date'):
        try: deadline_text = a['due_date'].strftime('%Y-%m-%d %H:%M')
        except: deadline_text = str(a['due_date'])
    text += f"🕒 Срок сдачи: {deadline_text}\n"
    accepting = a.get('accepting_submissions', True)
    text += f"Прием работ: {'Открыт ✅' if accepting else 'Закрыт ❌'}\n\n"

    builder = InlineKeyboardBuilder()
    if accepting: builder.button(text="📎 Прикрепить решение", callback_data=f"submit_now_{assignment_id}")
    builder.adjust(1)
    await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
    await query.answer()

@student_router.callback_query(F.data.startswith("submit_now_"))
async def student_submit_now_start(query: CallbackQuery, user_role: str, state: FSMContext, db_pool: asyncpg.Pool):
    if user_role != 'student': return
    assignment_id = int(query.data.split("_")[-1])
    async with db_pool.acquire() as conn: a = await api.get_assignment(conn, assignment_id)
    if not a or not a.get('accepting_submissions', True):
        await query.answer("Прием работ закрыт.", show_alert=True); return

    await state.update_data(assignment_id=assignment_id)
    await state.set_state(StudentActions.submitting_assignment_file)
    await query.message.edit_text(f"Прикрепите **файл**.\n(Задание: {html.quote(a.get('title','?'))})", parse_mode=ParseMode.HTML)
    await query.answer()

@student_router.message(StudentActions.submitting_assignment_file, F.content_type.in_({
    ContentType.DOCUMENT, ContentType.PHOTO, ContentType.AUDIO, ContentType.VIDEO,
    ContentType.VOICE, ContentType.ANIMATION, ContentType.VIDEO_NOTE
}))
async def student_submit_assignment_file(message: Message, state: FSMContext, bot: Bot, user_db_id: int, user_info: dict, db_pool: asyncpg.Pool):
    data = await state.get_data(); assignment_id = data.get("assignment_id")
    if not assignment_id: await state.clear(); await message.reply("Ошибка FSM.", reply_markup=get_student_main_keyboard()); return

    file_id = None; media = getattr(message, message.content_type.value, None)
    if isinstance(media, list): media = media[-1]
    if hasattr(media, 'file_id'): file_id = media.file_id
    if not file_id: await message.reply("Не вижу файла."); return

    try:
        async with db_pool.acquire() as conn:
            a = await api.get_assignment(conn, assignment_id)
            if not a or not a.get('accepting_submissions', True):
                 await message.answer("Прием работ уже закрыт.", reply_markup=get_student_main_keyboard())
                 await state.clear(); return

            sub_res = await api.add_submission(conn, assignment_id, user_db_id, file_id)
            if not sub_res: raise Exception("Submit fail")

        await message.answer(f"✅ Решение отправлено!", reply_markup=get_student_main_keyboard())

        teachers = await api.get_teachers_ids(db_pool)
        s_name = f"{user_info.get('first_name')} {user_info.get('last_name')}"
        g_name = user_info.get('group_name', '?')
        a_title = a.get('title', f"ID {assignment_id}") if a else f"ID {assignment_id}"
        sub_time = sub_res.get('submission_date', datetime.now())
        late_mark = " (ОПОЗДАНИЕ)" if sub_res.get('is_late') else ""
        caption = f"📥 Новая работа!{late_mark}\nСтд: {s_name} ({g_name})\nЗад: {a_title}\nВремя: {sub_time.strftime('%Y-%m-%d %H:%M')}\nID: {sub_res.get('submission_id')}"

        for tid in teachers:
            try:
                await message.forward(chat_id=tid)
                await bot.send_message(chat_id=tid, text=caption)
            except TelegramAPIError as e: logger.error(f"Notify teacher {tid} fail: {e}")

    except Exception as e: logger.error(f"Submit err a={assignment_id} s={user_db_id}: {e}", exc_info=True); await message.reply("❌ Ошибка отправки.")
    finally: await state.clear()


@student_router.message(StudentActions.submitting_assignment_file)
async def student_submit_assignment_incorrect_type(message: Message): await message.reply("Нужен **файл**.")

@student_router.message(F.text == "❓ Задать вопрос (Q&A)")
@student_router.message(Command("ask"))
async def ask_question_start(message: Message, state: FSMContext, user_role: str):
    if user_role != 'student': return
    await message.answer("Введите вопрос:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(StudentActions.asking_question)

@student_router.message(StudentActions.asking_question, F.text)
async def process_question(message: Message, state: FSMContext, bot: Bot, user_db_id: int, user_info: dict, db_pool: asyncpg.Pool):
    q_text = message.text;
    if not q_text or len(q_text) < 5: await message.reply("Слишком коротко."); return
    try:
        g_id = user_info.get('group_id')
        async with db_pool.acquire() as conn: q_res = await api.add_question(conn, user_db_id, g_id, q_text)
        if not q_res: raise Exception("Fail add Q")
        await message.answer("✅ Вопрос отправлен.", reply_markup=get_student_main_keyboard())
        teachers = await api.get_teachers_ids(db_pool)
        s_name = f"{user_info.get('first_name')} {user_info.get('last_name')}"; g_name = user_info.get('group_name', '?')
        q_id = q_res.get('question_id'); notify = f"❓ Вопрос (ID: {q_id}) от {s_name} ({g_name})\n\n{q_text}"
        for tid in teachers:
            try: await bot.send_message(tid, notify)
            except TelegramAPIError as e: logger.error(f"Notify teacher {tid} Q fail: {e}")
    except Exception as e: logger.error(f"Ask Q err {user_db_id}: {e}"); await message.answer("❌ Ошибка.", reply_markup=get_student_main_keyboard())
    finally: await state.clear()

@student_router.message(F.text == "📊 Мои Оценки")
async def student_view_my_grades(message: Message, user_role: str, user_db_id: int, db_pool: asyncpg.Pool):
    if user_role != 'student': return
    async with db_pool.acquire() as conn: grades = await api.get_grades_for_student(conn, user_db_id)
    if not grades: await message.answer("Оценок нет."); return
    resp = "📊 *Ваши оценки:*\n\n" + "\n".join([
        f"📄 *{html.bold(html.quote(g.get('assignment_title', '?')))}*: " +
        (f"{html.bold(str(g['grade']))}" + (f" ({html.quote(g['teacher_comment'])})" if g.get('teacher_comment') else "") if g.get('grade') is not None else "Не оценено")
        for g in grades
    ])
    MAX_L = 4096
    for i in range(0, len(resp), MAX_L): await message.answer(resp[i:i + MAX_L], parse_mode=ParseMode.HTML)

@student_router.message(F.text == "🔄 Сменить группу")
async def change_group_button_handler(message: Message, state: FSMContext, user_role: str, user_info: dict, db_pool: asyncpg.Pool):
     await change_group_start(message, state, user_role, user_info, db_pool)

@student_router.message(F.text == "👤 Сменить имя")
@student_router.message(Command("change_name"))
async def change_name_start(message: Message, state: FSMContext, user_role: str, user_info: dict, db_pool: asyncpg.Pool):
     if user_role != 'student': return
     async with db_pool.acquire() as conn: u = await api.get_user_by_db_id(conn, user_info['user_id'])
     if u and u.get('pending_first_name'):
         await message.answer(f"⏳ Запрос на смену имени на **{html.quote(u['pending_first_name'])} {html.quote(u['pending_last_name'])}** уже отправлен.", parse_mode=ParseMode.HTML); return
     await message.answer("Введите **новое Имя**:", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
     await state.set_state(StudentActions.changing_name_first)

@student_router.message(StudentActions.changing_name_first, F.text)
async def process_change_name_first(message: Message, state: FSMContext):
     name = message.text.strip()
     if not name or len(name) > 100: await message.reply("Некорректное имя."); return
     await state.update_data(new_first_name=name)
     await message.answer("Введите **новую Фамилию**:", parse_mode=ParseMode.MARKDOWN)
     await state.set_state(StudentActions.changing_name_last)

@student_router.message(StudentActions.changing_name_last, F.text)
async def process_change_name_last(message: Message, state: FSMContext, user_db_id: int, db_pool: asyncpg.Pool):
    last_name = message.text.strip()
    if not last_name or len(last_name) > 100: await message.reply("Некорректная фамилия."); return
    data = await state.get_data(); first_name = data.get('new_first_name')
    try:
        async with db_pool.acquire() as conn: success = await api.request_name_change(conn, user_db_id, first_name, last_name)
        if success: await message.answer(f"⏳ Запрос на смену имени на **{html.quote(first_name)} {html.quote(last_name)}** отправлен.", reply_markup=get_student_main_keyboard(), parse_mode=ParseMode.HTML)
        else: await message.answer("Не удалось отправить запрос.", reply_markup=get_student_main_keyboard())
    except Exception as e: logger.error(f"Name change err {user_db_id}: {e}"); await message.answer("❌ Ошибка.", reply_markup=get_student_main_keyboard())
    finally: await state.clear()

@teacher_router.message(F.text == "⚙️ Админ-панель")
async def teacher_open_webapp(message: Message, user_role: str):
     if user_role != 'teacher': return
     await message.answer("Кнопка:", reply_markup=get_teacher_main_keyboard())

@teacher_router.message(F.web_app_data)
async def handle_webapp_data(message: Message, bot: Bot, user_role: str, user_db_id: int, db_pool: asyncpg.Pool):
    if user_role != 'teacher': return
    try:
        data = json.loads(message.web_app_data.data); logger.info(f"WebApp Data: {data}")
        action = data.get('action')

        if action == 'get_submission_file' and 'file_id' in data:
            fid = data['file_id']; sid = data.get('submission_id'); cap = f"Файл (сдача ID: {sid})"
            try: await bot.send_document(message.chat.id, fid, caption=cap)
            except TelegramAPIError as e: logger.error(f"File forward err {fid}: {e}"); await message.answer(f"❌ Файл `{fid}`?")

        elif action == 'send_assignment_to_group' and all(k in data for k in ['group_id', 'title', 'assignment_id']):
            gid = data['group_id']; title = data['title']; desc = data.get('description',''); due = data.get('due_date')
            fid = data.get('file_id'); ftype = data.get('file_type'); aid = data['assignment_id']
            async with db_pool.acquire() as conn: students = await api.get_users(conn, role='student', group_id=gid, approved=True)

            text = f"🔔 Новое задание: **{html.quote(title)}** (ID: {aid})\n\n"
            if desc: text += f"{html.quote(desc)}\n\n"
            if due: try: text += f"Срок: {datetime.fromisoformat(due).strftime('%Y-%m-%d %H:%M')}\n\n" except: pass
            text += "Используйте 'Сдать задание'."

            send_method = bot.send_document if fid else bot.send_message
            payload = {'document': fid, 'caption': text, 'parse_mode': ParseMode.HTML} if fid else {'text': text, 'parse_mode': ParseMode.HTML}
            s, f = 0, 0
            for st in students:
                try: await send_method(st['telegram_id'], **payload); s += 1; await asyncio.sleep(0.05) # Rate limit
                except TelegramAPIError as e: logger.warning(f"Send assign {aid} to {st['telegram_id']} fail: {e}"); f += 1
            await message.answer(f"Задание {aid} отправлено {s} студентам. Ошибок: {f}.")

        elif action == 'notify_answer' and all (k in data for k in ['student_telegram_id', 'question_text', 'answer_text']):
             st_tid = data['student_telegram_id']; q = data['question_text']; a = data['answer_text']
             noti = f"💡 Ответ на вопрос:\n\n*Вопрос:* {html.quote(q)}\n*Ответ:* {html.quote(a)}"
             try: await bot.send_message(st_tid, noti, parse_mode=ParseMode.HTML)
             except TelegramAPIError as e: logger.error(f"Notify answer fail {st_tid}: {e}"); await message.answer(f"Студенту {st_tid} не ушло.")

        else: await message.answer("Неизвестное действие.")

    except json.JSONDecodeError: logger.error(f"WebApp JSON err: {message.web_app_data.data}"); await message.answer("Ошибка JSON.")
    except Exception as e: logger.error(f"WebApp err: {e}", exc_info=True); await message.answer("Ошибка.")


async def main():
    bot = Bot(token=BOT_TOKEN, default=types.DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = MemoryStorage(); dp = Dispatcher(storage=storage)
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn: await conn.fetchval("SELECT 1")
        logger.info("БД OK")
    except Exception as e: logger.critical(f"БД Ошибка: {e}", exc_info=True); return

    dp["db_pool"] = pool
    dp.update.outer_middleware(ApprovalMiddleware())
    dp.include_router(registration_router); dp.include_router(teacher_router)
    dp.include_router(student_router); dp.include_router(main_router)
    logger.info("Запуск...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e: logger.critical(f"Polling Ошибка: {e}", exc_info=True)
    finally:
        await bot.session.close()
        if pool: await pool.close(); logger.info("Пул БД закрыт")

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Стоп.")
    except Exception as e: logger.critical(f"Крит. Ошибка: {e}", exc_info=True)