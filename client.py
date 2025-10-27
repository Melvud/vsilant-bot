import asyncio
import json
import logging
import os
import uuid
from contextlib import suppress
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import asyncpg
from aiogram import Bot, Dispatcher, F, Router, html, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    User,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

import api

# -------------------- ENV / LOGGING --------------------
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: Set[int] = set()
if ADMIN_IDS_STR:
    try:
        ADMIN_IDS = {int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(",") if admin_id.strip()}
        logging.info(f"Загружены ADMIN_IDS: {ADMIN_IDS}")
    except ValueError:
        logging.error("Не удалось разобрать ADMIN_IDS.")
else:
    logging.warning("ADMIN_IDS не указаны.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# -------------------- Константы --------------------
ALLOWED_DOC_EXTS = {"pptx", "pdf", "docx"}

MATERIAL_CATEGORIES = {
    "lectures": "Лекции",
    "announcements": "Объявления",
    "figures": "Графики/Рисунки",
    "video": "Видео",
    "links": "Ссылки",
    "library": "Библиотека",
}

# -------------------- FSM --------------------
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


# -------------------- Routers --------------------
main_router = Router()
registration_router = Router()
student_router = Router()
teacher_router = Router()
profile_router = Router()
materials_router = Router()

# -------------------- Globals --------------------
db_pool: Optional[asyncpg.Pool] = None
bot_instance: Optional[Bot] = None

LAST_MATERIALS: Dict[int, List[str]] = {}
MAX_LAST_MATERIALS = 5
LAST_MATERIALS_BY_CAT: Dict[int, Dict[str, List[str]]] = {}

# -------------------- DB --------------------
async def get_db_pool() -> asyncpg.Pool:
    global db_pool
    if db_pool is None:
        try:
            db_url = DATABASE_URL
            if db_url and db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
            db_pool = await asyncpg.create_pool(db_url, max_size=20)
            logger.info("Пул БД создан")
        except Exception as e:
            logger.critical(f"Ошибка пула БД: {e}", exc_info=True)
            raise
    return db_pool


# -------------------- Menu Button --------------------
async def apply_menu_button(bot: Bot, user_id: int, is_admin: bool):
    try:
        if is_admin and WEBAPP_URL:
            mb = types.MenuButtonWebApp(text="Админ-панель", web_app=WebAppInfo(url=WEBAPP_URL))
        else:
            mb = types.MenuButtonDefault()
        await bot.set_chat_menu_button(chat_id=user_id, menu_button=mb)
    except Exception as e:
        logger.warning(f"Не удалось применить меню для {user_id}: {e}")


# -------------------- Middleware --------------------
class ApprovalMiddleware(BaseMiddleware):
    """
    Пропускаем:
      - /start всегда
      - шаги Registration.*
      - callback 'register_to_group_*'
    Блокируем ТОЛЬКО если регистрация завершена (student + валидная группа + имя/фамилия), но approved=False.
    Во всех незавершённых состояниях принудительно считаем ролью 'registering'.
    """

    @staticmethod
    def _needs_registration(u: Optional[dict], tg_first_name: str = "", tg_last_name: str = "") -> bool:
        if not u:
            return True
        role = (u.get("role") or "pending").lower()
        g = u.get("group_id")
        try:
            g_ok = g is not None and int(g) > 0
        except Exception:
            g_ok = bool(g)
        has_names = bool((u.get("first_name") or "").strip() or tg_first_name) and \
                    bool((u.get("last_name") or "").strip() or tg_last_name)
        return (role != "student") or (not g_ok) or (not has_names)

    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        user: Optional[User] = data.get("event_from_user")
        bot: Bot = data["bot"]

        if not user:
            return await handler(event, data)

        pool = await get_db_pool()
        data["db_pool"] = pool

        state: Optional[FSMContext] = data.get("state")
        current_state_str = await state.get_state() if state else None

        # Всегда пропускаем /start
        if isinstance(event, Message) and isinstance(event.text, str) and event.text.startswith("/start"):
            return await handler(event, data)

        # Всегда пропускаем шаги FSM регистрации
        if current_state_str and current_state_str.startswith(Registration.__name__):
            return await handler(event, data)

        # Всегда пропускаем выбор группы для регистрации
        if isinstance(event, CallbackQuery) and isinstance(event.data, str) and event.data.startswith("register_to_group_"):
            return await handler(event, data)

        # Админ — всегда пропускаем
        if user.id in ADMIN_IDS:
            data["user_role"] = "teacher"
            data["user_db_id"] = None
            data["user_info"] = {
                "telegram_id": user.id,
                "role": "teacher",
                "approved": True,
                "first_name": user.first_name or "Admin",
                "last_name": user.last_name or "",
            }
            with suppress(Exception):
                await apply_menu_button(bot, user.id, True)
            return await handler(event, data)

        # Ищем пользователя в БД
        db_user = None
        try:
            async with pool.acquire() as conn:
                db_user = await api.get_user(conn, user.id)
        except Exception as e:
            logger.error(f"DB error in middleware getting user {user.id}: {e}")

        # Новый — считаем 'registering'
        if not db_user:
            data["user_role"] = "registering"
            data["user_db_id"] = None
            data["user_info"] = {"telegram_id": user.id, "role": "pending", "approved": False}
            with suppress(Exception):
                await apply_menu_button(bot, user.id, False)
            return await handler(event, data)

        # Если регистрация НЕ завершена — явно помечаем ролью 'registering' и пропускаем
        if self._needs_registration(db_user, user.first_name or "", user.last_name or ""):
            data["user_db_id"] = db_user.get("user_id")
            data["user_role"] = "registering"  # <— ключевое
            data["user_info"] = db_user
            with suppress(Exception):
                await apply_menu_button(bot, user.id, False)
            return await handler(event, data)

        # Регистрация завершена, но не подтверждён — блокируем всё нерегистрационное
        if not db_user.get("approved"):
            with suppress(Exception):
                await apply_menu_button(bot, user.id, False)
            # Без спама — просто мягкий ответ и блок
            try:
                if isinstance(event, Message):
                    await event.answer("⏳ Ваш аккаунт ожидает подтверждения администратором. После одобрения используйте /start.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("⏳ Ожидается подтверждение администратором.", show_alert=False)
            except TelegramAPIError as e:
                logger.warning(f"Failed to notify unapproved {user.id}: {e}")
            return

        # Подтверждён — пропускаем
        data["user_db_id"] = db_user.get("user_id")
        data["user_role"] = db_user.get("role") or "student"
        data["user_info"] = db_user
        with suppress(Exception):
            await apply_menu_button(bot, user.id, False)
        return await handler(event, data)

# -------------------- Keyboards --------------------
def get_student_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📝 Сдать задание")
    builder.button(text="❓ Задать вопрос (Q&A)")
    builder.button(text="📁 Материалы")
    builder.button(text="📊 Мои Оценки")
    builder.button(text="👤 Профиль")
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Действие:")


def get_admin_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📝 Сдать задание")
    builder.button(text="❓ Задать вопрос (Q&A)")
    builder.button(text="📁 Материалы")
    builder.button(text="📊 Мои Оценки")
    builder.button(text="👤 Профиль")
    if WEBAPP_URL:
        builder.button(text="⚙️ Админ-панель", web_app=WebAppInfo(url=WEBAPP_URL))
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Админ / Действие:")


def get_profile_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="🔄 Сменить группу")
    builder.button(text="👤 Сменить имя")
    builder.button(text="⬅️ Назад")
    builder.adjust(2, 1)
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Профиль:")


def get_materials_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📘 Лекции")
    builder.button(text="📣 Объявления")
    builder.button(text="📊 Графики/Рисунки")
    builder.button(text="🎬 Видео")
    builder.button(text="🔗 Ссылки")
    builder.button(text="📚 Библиотека")
    builder.button(text="⬅️ Назад")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Материалы:")


async def get_groups_keyboard(db_pool: asyncpg.Pool, prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    try:
        async with db_pool.acquire() as conn:
            groups = await api.get_groups(conn)
        if groups:
            for g in groups:
                builder.button(text=g["name"], callback_data=f"{prefix}_to_group_{g['group_id']}")
            builder.adjust(2)
        return builder.as_markup()
    except Exception as e:
        logger.error(f"Ошибка получения групп: {e}")
        return builder.as_markup()


# -------------------- Handlers --------------------
@main_router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext, db_pool: asyncpg.Pool):
    """
    ЖЁСТКО: пока пользователь не доберёт ФИО и группу — всегда ведём его по регистрации.
    Сообщение «ожидание подтверждения» показываем только когда он уже student c присвоенной группой.
    """
    await state.clear()
    user = message.from_user
    user_id = user.id
    first_name_tg = user.first_name or "Пользователь"
    last_name_tg = user.last_name or ""
    is_admin = user_id in ADMIN_IDS

    await apply_menu_button(message.bot, user_id, is_admin)

    # строгая проверка нужды регистрации
    def needs_registration(u: Optional[dict]) -> bool:
        if not u:
            return True
        role = (u.get("role") or "pending").lower()
        # кривые значения group_id считаем как отсутствие
        g = u.get("group_id")
        try:
            g_ok = g is not None and int(g) > 0
        except Exception:
            g_ok = bool(g)
        has_names = bool(u.get("first_name")) and bool(u.get("last_name"))
        # если не student или нет валидной группы или нет имён — ещё рега
        # причём даже если approved=False — сначала рега
        return (role != "student") or (not g_ok) or (not has_names)

    db_user = None
    try:
        async with db_pool.acquire() as conn:
            db_user = await api.get_user(conn, user_id)
    except Exception as e:
        logger.error(f"DB error on /start for {user_id}: {e}")
        await message.answer("⚠️ Ошибка доступа к БД. Попробуйте позже.")
        return

    # 1) Новый — имя
    if not db_user:
        await message.answer(
            "👋 Добро пожаловать!\n\n📝 Начнём регистрацию.\n\nВведите ваше <b>Имя</b>:",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(Registration.getting_first_name)
        return

    # 2) Есть в БД, но регистрация не завершена — сразу к выбору группы (имя/фамилию возьмём из TG, если нужно)
    if needs_registration(db_user):
        kbd = await get_groups_keyboard(db_pool, "register")
        if not kbd or not getattr(kbd, "inline_keyboard", None):
            await message.answer("❗️ Нет ни одной группы для регистрации. Обратитесь к администратору.")
            return
        await state.update_data(
            first_name=db_user.get("first_name") or first_name_tg,
            last_name=db_user.get("last_name") or last_name_tg
        )
        await message.answer("🏫 Выберите вашу группу из списка:", reply_markup=kbd)
        await state.set_state(Registration.choosing_group)
        return

    # 3) Регистрация завершена:
    if db_user.get("approved"):
        role = db_user.get("role", "student")
        name = db_user.get("first_name", first_name_tg)
        if is_admin:
            await message.answer(
                f"С возвращением, {html.bold(name)}! 👑 (Администратор)",
                reply_markup=get_admin_main_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        elif role == "student":
            group = db_user.get("group_name")
            greet = f"С возвращением, {html.bold(name)}! 🎓"
            if group:
                greet += f"\nГруппа: {html.bold(html.quote(group))}"
            await message.answer(greet, reply_markup=get_student_main_keyboard(), parse_mode=ParseMode.HTML)
        elif role == "teacher":
            await message.answer(f"Здравствуйте, {html.bold(name)}! 🧑‍🏫", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.HTML)
        else:
            await message.answer(f"Здравствуйте! Ваш статус: {role}.", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.HTML)
    else:
        # ВАЖНО: сюда попадаем ТОЛЬКО если юзер уже student И есть рабочая группа (т.е. рега завершена)
        await message.answer(
            "⏳ Ваша заявка отправлена администратору.\n"
            "Вы получите уведомление после подтверждения.",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )


@main_router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, bot: Bot):
    user = update.from_user
    if not user:
        return
    is_admin = user.id in ADMIN_IDS
    await apply_menu_button(bot, user.id, is_admin)


@main_router.message(Command("menu"))
@main_router.message(F.text == "⬅️ Назад")
async def show_menu(message: Message, user_role: Optional[str] = None, user_info: Optional[dict] = None):
    is_admin = message.from_user.id in ADMIN_IDS
    role = (user_role or "pending").lower()
    kbd = None

    # Для незавершённой регистрации — НЕ показываем «ожидание», а просим запустить /start
    if role in {"pending", "registering"}:
        await message.answer(
            "📝 Регистрация не завершена.\nНажмите /start и укажите имя, фамилию и выберите группу.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if is_admin:
        kbd = get_admin_main_keyboard()
        text = "👑 Админ-меню:"
        if user_info and user_info.get("group_name") and user_info.get("role") == "student":
            text += f"\n(Ваша группа студента: {html.bold(html.quote(user_info['group_name']))})"
        await message.answer(text, reply_markup=kbd, parse_mode=ParseMode.HTML)
        return

    if role == "student":
        kbd = get_student_main_keyboard()
        text = "🎓 Меню студента:"
        if user_info and user_info.get("group_name"):
            text += f"\nГруппа: {html.bold(html.quote(user_info['group_name']))}"
        elif user_info:
            text += "\nГруппа: не назначена"
        await message.answer(text, reply_markup=kbd, parse_mode=ParseMode.HTML)
        return

    if role == "teacher":
        await message.answer("🧑‍🏫 Меню преподавателя:", reply_markup=ReplyKeyboardRemove())
        return

    # На всякий случай: если сюда попали, значит юзер подтверждения ждёт, но уже зарегистрирован.
    await message.answer("⏳ Ваш аккаунт ожидает подтверждения администратором. После одобрения используйте /start.")


@main_router.callback_query(F.data == "open_menu")
async def open_menu_cb(query: CallbackQuery, user_role: Optional[str] = None, user_info: Optional[dict] = None):
    if query.message:
        await show_menu(query.message, user_role=user_role, user_info=user_info)
    with suppress(Exception):
        await query.answer()

# -------------------- Регистрация --------------------
@registration_router.message(Registration.getting_first_name, F.text)
async def reg_getting_first_name(message: Message, state: FSMContext):
    first_name = message.text.strip()
    if not first_name or len(first_name) > 100:
        await message.reply("❌ Некорректное имя. Введите настоящее имя (до 100 символов):")
        return
    await state.update_data(first_name=first_name)
    await message.answer("👍 Теперь введите вашу <b>Фамилию</b>:", parse_mode=ParseMode.HTML)
    await state.set_state(Registration.getting_last_name)


@registration_router.message(Registration.getting_last_name, F.text)
async def reg_getting_last_name(message: Message, state: FSMContext, db_pool: asyncpg.Pool):
    last_name = message.text.strip()
    if not last_name or len(last_name) > 100:
        await message.reply("❌ Некорректная фамилия. Введите настоящую фамилию (до 100 символов):")
        return
    await state.update_data(last_name=last_name)

    kbd = await get_groups_keyboard(db_pool, "register")
    if not kbd or not getattr(kbd, "inline_keyboard", None):
        await message.answer("❗️ Не найдено ни одной группы. Обратитесь к администратору.")
        await state.clear()
        return

    await message.answer("🏫 Выберите вашу группу из списка:", reply_markup=kbd)
    await state.set_state(Registration.choosing_group)


@registration_router.callback_query(Registration.choosing_group, F.data.startswith("register_to_group_"))
async def reg_choosing_group_callback(query: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool):
    try:
        group_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        await query.message.edit_text("❌ Ошибка выбора группы. Запустите /start заново.")
        await state.clear()
        return

    data = await state.get_data()
    first_name = (data.get("first_name") or query.from_user.first_name or "Пользователь").strip()
    last_name = (data.get("last_name") or query.from_user.last_name or "").strip()
    user = query.from_user

    try:
        async with db_pool.acquire() as conn:
            db_user = await api.get_user(conn, user.id)

            if not db_user:
                # Полный новый
                user_data = {
                    "telegram_id": user.id,
                    "username": user.username,
                    "first_name": first_name,
                    "last_name": last_name,
                }
                new_user = await api.add_user(conn, user_data)
                user_db_id = new_user["user_id"]
                await api.set_user_role(conn, user_db_id, "student")
                await api.add_student(conn, user_db_id, group_id)
                await api.set_student_group(conn, user_db_id, group_id)
            else:
                user_db_id = db_user["user_id"]
                # Обновим имя/фамилию если пустые
                try:
                    if not db_user.get("first_name") or not db_user.get("last_name"):
                        await conn.execute(
                            "UPDATE users SET first_name=$1, last_name=$2 WHERE user_id=$3",
                            first_name, last_name, user_db_id
                        )
                except Exception as e:
                    logger.debug(f"Skip update names: {e}")

                # Роль student
                try:
                    await api.set_user_role(conn, user_db_id, "student")
                except Exception:
                    pass

                # students + группа
                try:
                    await api.add_student(conn, user_db_id, group_id)
                except Exception:
                    pass
                try:
                    await api.set_student_group(conn, user_db_id, group_id)
                except Exception as e:
                    logger.warning(f"set_student_group failed for {user_db_id}: {e}")

        await query.message.edit_text(
            "✅ <b>Регистрация завершена!</b>\n\n"
            "⏳ Заявка отправлена администратору на подтверждение. "
            "Вы получите уведомление, когда она будет одобрена.",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error(f"Ошибка финальной регистрации для {user.id}: {e}", exc_info=True)
        await query.answer("❌ Ошибка при регистрации. Попробуйте /start позже.", show_alert=True)
    finally:
        await state.clear()
        with suppress(Exception):
            await query.answer()

# -------------------- Профиль: меню и операции --------------------
@profile_router.message(F.text == "👤 Профиль")
@profile_router.message(Command("profile"))
async def open_profile(message: Message, user_role: str, user_info: dict, db_pool: asyncpg.Pool):
    if user_role == "student" or message.from_user.id in ADMIN_IDS:
        await send_profile_card(message.chat.id, message.bot, user_info, db_pool)
        await message.answer("Меню профиля:", reply_markup=get_profile_menu_keyboard())
    else:
        await send_profile_card(message.chat.id, message.bot, user_info, db_pool)


@profile_router.message(F.text == "🔄 Сменить группу")
async def profile_change_group_start(message: Message, state: FSMContext, user_role: str, user_info: dict, db_pool: asyncpg.Pool):
    if user_role == "student":
        await change_group_start_from_profile_message(message, state, user_info, db_pool)
    else:
        await message.reply("Эта функция доступна только студентам.")


@profile_router.message(F.text == "👤 Сменить имя")
async def profile_change_name_start(message: Message, state: FSMContext, user_role: str, user_info: dict, db_pool: asyncpg.Pool):
    if user_role == "student":
        await change_name_start_from_profile_message(message, state, user_info, db_pool)
    else:
        await message.reply("Эта функция доступна только студентам.")


async def send_profile_card(chat_id: int, bot: Bot, user_info: dict, db_pool: asyncpg.Pool):
    fresh_user_info = user_info
    try:
        async with db_pool.acquire() as conn:
            tid = user_info.get("telegram_id")
            if tid:
                fresh = await api.get_user(conn, tid)
                if fresh:
                    fresh_user_info = fresh
    except Exception as e:
        logger.warning(f"Failed to refresh profile for {chat_id}: {e}")

    fn = fresh_user_info.get("first_name") or "—"
    ln = fresh_user_info.get("last_name") or "—"
    group_name = fresh_user_info.get("group_name") or "—"
    role = fresh_user_info.get("role") or "неизвестно"
    approved = "✅ Да" if fresh_user_info.get("approved") else "❌ Нет"
    pending_group_name = fresh_user_info.get("pending_group_name")
    pending_fn = fresh_user_info.get("pending_first_name")
    pending_ln = fresh_user_info.get("pending_last_name")

    text = (
        "👤 <b>Ваш Профиль</b>\n\n"
        f"Имя: <b>{html.quote(fn)}</b>\n"
        f"Фамилия: <b>{html.quote(ln)}</b>\n"
        f"Роль: {html.quote(role.capitalize())}\n"
        f"Подтверждён: {approved}\n"
    )
    if fresh_user_info.get("role") == "student":
        text += f"Группа: <b>{html.quote(group_name)}</b>\n"

    if pending_group_name:
        text += f"\n⏳ <i>Запрос на смену группы → {html.quote(pending_group_name)}</i>\n"
    if pending_fn:
        text += f"\n⏳ <i>Запрос на смену имени → {html.quote(pending_fn)} {html.quote(pending_ln or '')}</i>\n"

    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


# -------- Helpers: assignments list for student --------
async def get_assignments_for_student_keyboard(
    db_pool: asyncpg.Pool, user_info: dict
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    group_id = user_info.get("group_id")
    assignments = []
    if group_id:
        try:
            async with db_pool.acquire() as conn:
                assignments = await api.get_assignments_for_group(conn, group_id)
        except Exception as e:
            logger.error(f"Ошибка получения заданий для группы {group_id}: {e}")

    if assignments:
        for a in assignments:
            status_icon = "🆕" if a.get("accepting_submissions", True) else "🔒"
            button_text = f'{status_icon} {a["title"]}'
            builder.button(text=button_text, callback_data=f"view_assignment_{a['assignment_id']}")
        builder.adjust(1)
    return builder.as_markup()


# -------------------- Student: Submit assignment --------------------
@student_router.message(F.text == "📝 Сдать задание")
@student_router.message(Command("submit"))
async def submit_assignment_list(
    message: Message, user_role: str, user_info: dict, db_pool: asyncpg.Pool
):
    is_admin = message.from_user.id in ADMIN_IDS
    if user_role != "student" and not is_admin:
        await message.reply("Эта функция доступна только студентам.")
        return

    group_id = user_info.get("group_id")
    if not group_id:
        if user_info.get("pending_group_id"):
            await message.answer("⏳ Ваш запрос на смену группы ещё не одобрен.")
        else:
            await message.answer("❌ Вы не состоите в группе. Смените группу в профиле или обратитесь к админу.")
        return

    kbd = await get_assignments_for_student_keyboard(db_pool, user_info)
    if kbd and getattr(kbd, "inline_keyboard", None):
        await message.answer("📋 Выберите задание для сдачи:", reply_markup=kbd)
    else:
        await message.answer("ℹ️ Для вашей группы пока нет доступных заданий.")


@student_router.callback_query(F.data.startswith("view_assignment_"))
async def student_view_assignment(
    query: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, user_role: str, user_info: dict
):
    is_admin = query.from_user.id in ADMIN_IDS
    try:
        assignment_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        await query.answer("Ошибка ID задания", show_alert=True)
        return

    try:
        async with db_pool.acquire() as conn:
            a = await api.get_assignment(conn, assignment_id)
        if not a:
            await query.answer("Задание не найдено", show_alert=True)
            return

        text = f"📝 <b>{html.quote(a['title'])}</b>\n\n"
        if a.get("description"):
            text += f"{html.quote(a['description'])}\n\n"
        deadline_text = "Не установлен"
        due_date = a.get("due_date")
        is_past_due = False
        if due_date:
            try:
                due_date_dt = datetime.fromisoformat(str(due_date)) if isinstance(due_date, str) else due_date
                deadline_text = due_date_dt.strftime("%d.%m.%Y %H:%M")
                now = datetime.now(due_date_dt.tzinfo)
                if now > due_date_dt:
                    is_past_due = True
                    deadline_text += " (Прошло)"
            except Exception as e:
                logger.warning(f"Error formatting due date {due_date}: {e}")
                deadline_text = str(due_date)
        text += f"🕒 Срок сдачи: <b>{deadline_text}</b>\n"

        accepting = a.get("accepting_submissions", True)
        status_text = "<b>Открыт ✅</b>" if accepting else ("<b>Закрыт (срок истек) ❌</b>" if is_past_due else "<b>Закрыт (администратором) ❌</b>")
        text += f"📬 Прием работ: {status_text}\n"

        submission_info = ""
        user_db_id = user_info.get("user_id")
        if user_role == "student" and user_db_id:
            try:
                async with db_pool.acquire() as conn:
                    sub = await conn.fetchrow(
                        "SELECT submission_date, is_late, grade FROM submissions WHERE assignment_id=$1 AND student_id=$2",
                        assignment_id, user_db_id
                    )
                    if sub:
                        sub_time = sub['submission_date'].strftime("%d.%m.%Y %H:%M")
                        late_mark = " (с опозданием)" if sub['is_late'] else ""
                        grade_mark = f", Оценка: {sub['grade']}/20" if sub['grade'] is not None else ", ещё не оценено"
                        submission_info = f"\n\n✅ <b>Вы сдали {sub_time}{late_mark}{grade_mark}.</b> Можно пересдать."
            except Exception as e:
                logger.error(f"Error checking submission status: {e}")
        text += submission_info

        builder = InlineKeyboardBuilder()
        if accepting and (user_role == "student" or is_admin):
            button_text = "📎 Прикрепить решение" if not submission_info else "🔄 Пересдать работу"
            builder.button(text=button_text, callback_data=f"submit_now_{assignment_id}")
        builder.adjust(1)

        if query.message:
            await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())

    except Exception as e:
        logger.error(f"Ошибка просмотра задания {assignment_id}: {e}", exc_info=True)
        await query.answer("❌ Ошибка при загрузке задания", show_alert=True)
    finally:
        with suppress(Exception):
            await query.answer()


@student_router.callback_query(F.data.startswith("submit_now_"))
async def student_submit_now_start(
    query: CallbackQuery, state: FSMContext, db_pool: asyncpg.Pool, user_role: str
):
    is_admin = query.from_user.id in ADMIN_IDS
    if user_role != "student" and not is_admin:
        await query.answer("Доступ запрещен", show_alert=True)
        return

    try:
        assignment_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        await query.answer("Ошибка ID задания", show_alert=True)
        return

    try:
        async with db_pool.acquire() as conn:
            a = await api.get_assignment(conn, assignment_id)
        if not a:
            await query.answer("⚠️ Задание больше недоступно.", show_alert=True)
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
            return
        if not a.get("accepting_submissions", True):
            await query.answer("⚠️ Приём работ закрыт.", show_alert=True)
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
            return
        title = a.get("title", f"Задание #{assignment_id}")
    except Exception as e:
        logger.error(f"Ошибка получения задания {assignment_id}: {e}")
        await query.answer("❌ Ошибка получения информации о задании.", show_alert=True)
        return

    await state.update_data(assignment_id=assignment_id, assignment_title=title)
    await state.set_state(StudentActions.submitting_assignment_file)

    if query.message:
        await query.message.edit_text(
            f"📎 Отправьте <b>один документ</b> с решением.\n"
            f"Допустимые форматы: <b>{', '.join('.' + ext for ext in ALLOWED_DOC_EXTS)}</b>.\n\n"
            f"📝 Задание: <b>{html.quote(title)}</b>\n\n"
            f"<i>(Если передумали, отправьте /cancel)</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=None
        )
    with suppress(Exception):
        await query.answer()


@student_router.message(Command("cancel"), StudentActions.submitting_assignment_file)
async def cancel_submission(message: Message, state: FSMContext, user_role: str):
    await state.clear()
    is_admin = message.from_user.id in ADMIN_IDS
    kbd = get_admin_main_keyboard() if is_admin else get_student_main_keyboard()
    await message.answer("Отправка решения отменена.", reply_markup=kbd)


@student_router.message(
    StudentActions.submitting_assignment_file,
    F.content_type.in_({ContentType.DOCUMENT})
)
async def student_submit_assignment_file(
    message: Message,
    state: FSMContext,
    bot: Bot,
    user_db_id: int,
    user_info: dict,
    db_pool: asyncpg.Pool,
):
    data = await state.get_data()
    assignment_id = data.get("assignment_id")
    assignment_title = data.get("assignment_title", "?")
    is_admin = message.from_user.id in ADMIN_IDS
    submitter_db_id = user_db_id
    if not submitter_db_id:
        logger.error(f"Submission attempt without db_id: TG_ID={message.from_user.id}")
        await message.reply("❌ Ошибка: не удалось определить ваш ID. Попробуйте /start.")
        await state.clear()
        return

    kbd = get_admin_main_keyboard() if is_admin else get_student_main_keyboard()

    if not assignment_id:
        await state.clear()
        await message.reply("❌ Ошибка состояния FSM. Начните сдачу заново.", reply_markup=kbd)
        return

    if not message.document:
        await message.reply(
            f"❌ Отправьте <b>документ</b> в формате <b>{', '.join('.' + ext for ext in ALLOWED_DOC_EXTS)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    file_name = (message.document.file_name or "file").lower()
    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else ""
    if ext not in ALLOWED_DOC_EXTS:
        await message.reply(
            f"❌ Недопустимый формат (.{ext}). Разрешены: <b>{', '.join('.' + ext for ext in ALLOWED_DOC_EXTS)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    file_id = message.document.file_id

    try:
        async with db_pool.acquire() as conn:
            sub_res = await api.add_submission(conn, assignment_id, submitter_db_id, file_id)
            if not sub_res:
                await message.answer("⚠️ Не удалось отправить. Возможно, приём работ закрыт.", reply_markup=kbd)
                await state.clear()
                return

        await message.answer("✅ Решение отправлено!", reply_markup=kbd)
        await state.clear()

        try:
            teachers_db_ids = await api.get_teachers_ids(db_pool)
            all_notify_ids = set(teachers_db_ids) | ADMIN_IDS

            submitter_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip() or f"ID: {submitter_db_id}"
            submitter_group = user_info.get("group_name", "Неизвестно")

            sub_time = sub_res.get("submission_date")
            sub_time_str = "неизвестно"
            if sub_time:
                sub_time_dt = datetime.fromisoformat(str(sub_time)) if isinstance(sub_time, str) else sub_time
                sub_time_str = sub_time_dt.strftime('%d.%m.%Y %H:%M:%S')

            late_mark = " ⚠️ <b>ОПОЗДАНИЕ</b>" if sub_res.get("is_late") else ""
            submission_id = sub_res.get("submission_id", "???")

            caption = (
                f"📥 <b>Новая работа!</b>{late_mark}\n\n"
                f"👤 От: <b>{html.quote(submitter_name)}</b> (ID: {submitter_db_id})\n"
                f"📚 Группа: <b>{html.quote(submitter_group)}</b>\n"
                f"📝 Задание: <b>{html.quote(assignment_title)}</b> (ID: {assignment_id})\n"
                f"🕐 Время: {sub_time_str}\n"
                f"🆔 ID сдачи: <code>{submission_id}</code>"
            )

            sent_to = set()
            for tid in all_notify_ids:
                if tid == message.from_user.id or tid in sent_to:
                    continue
                try:
                    await bot.send_document(chat_id=tid, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
                    sent_to.add(tid)
                    await asyncio.sleep(0.05)
                except TelegramAPIError as e:
                    logger.error(f"Notify {tid} about submission {submission_id} fail: {e}")
        except Exception as e:
            logger.error(f"Error during notify phase: {e}", exc_info=True)

    except asyncpg.exceptions.RaiseError as db_error:
        logger.error(f"DB raise error during submission a={assignment_id} s={submitter_db_id}: {db_error}")
        await message.reply(f"❌ Ошибка базы данных: {db_error.message}", reply_markup=kbd)
        await state.clear()
    except Exception as e:
        logger.error(f"Submit err a={assignment_id} s={submitter_db_id}: {e}", exc_info=True)
        if "closed" in str(e).lower():
            await message.reply("⚠️ Не удалось отправить. Приём работ закрыт.", reply_markup=kbd)
        else:
            await message.reply("❌ Непредвиденная ошибка. Попробуйте позже.", reply_markup=kbd)
        await state.clear()


@student_router.message(StudentActions.submitting_assignment_file)
async def student_submit_assignment_incorrect_type(message: Message):
    await message.reply(
        f"❌ Ожидается файл-документ. Отправьте один из форматов: <b>{', '.join('.' + ext for ext in ALLOWED_DOC_EXTS)}</b> или /cancel.",
        parse_mode=ParseMode.HTML
    )


# -------------------- Student: Q&A --------------------
@student_router.message(F.text == "❓ Задать вопрос (Q&A)")
@student_router.message(Command("ask"))
async def ask_question_start(message: Message, state: FSMContext, user_role: str):
    is_admin = message.from_user.id in ADMIN_IDS
    if user_role == "student" or is_admin:
        await message.answer("❓ Введите ваш вопрос преподавателям:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(StudentActions.asking_question)
    else:
        await message.reply("Эта функция доступна только студентам.")


@student_router.message(Command("cancel"), StudentActions.asking_question)
async def cancel_question(message: Message, state: FSMContext, user_role: str):
    await state.clear()
    is_admin = message.from_user.id in ADMIN_IDS
    kbd = get_admin_main_keyboard() if is_admin else get_student_main_keyboard()
    await message.answer("Отправка вопроса отменена.", reply_markup=kbd)


@student_router.message(StudentActions.asking_question, F.text)
async def process_question(
    message: Message,
    state: FSMContext,
    bot: Bot,
    user_db_id: int,
    user_info: dict,
    db_pool: asyncpg.Pool,
):
    q_text = message.text.strip()
    is_admin = message.from_user.id in ADMIN_IDS
    submitter_db_id = user_db_id
    if not submitter_db_id:
        logger.error(f"Question attempt without db_id: TG_ID={message.from_user.id}")
        await message.reply("❌ Ошибка: не удалось определить ваш ID. Попробуйте /start.")
        await state.clear()
        return

    kbd = get_admin_main_keyboard() if is_admin else get_student_main_keyboard()

    if not q_text or len(q_text) < 5:
        await message.reply("❌ Вопрос слишком короткий. Опишите подробнее (минимум 5 символов) или /cancel.")
        return

    try:
        g_id = user_info.get("group_id")
        async with db_pool.acquire() as conn:
            q_res = await api.add_question(conn, submitter_db_id, g_id, q_text)

        if not q_res or "question_id" not in q_res:
            raise Exception("Failed to add question to DB or missing question_id")

        await message.answer("✅ Ваш вопрос отправлен преподавателям и администраторам.", reply_markup=kbd)
        await state.clear()

        try:
            teachers_db_ids = await api.get_teachers_ids(db_pool)
            all_notify_ids = set(teachers_db_ids) | ADMIN_IDS

            submitter_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip() or f"ID: {submitter_db_id}"
            g_name = user_info.get("group_name", "Не в группе")
            q_id = q_res["question_id"]
            notify_text = (
                f"❓ <b>Новый вопрос</b> (ID: {q_id})\n\n"
                f"👤 От: <b>{html.quote(submitter_name)}</b> (ID: {submitter_db_id})\n"
                f"📚 Группа: <b>{html.quote(g_name)}</b>\n\n"
                f"<i>{html.quote(q_text)}</i>"
            )

            sent_to = set()
            for tid in all_notify_ids:
                if tid == message.from_user.id or tid in sent_to:
                    continue
                try:
                    await bot.send_message(tid, notify_text, parse_mode=ParseMode.HTML)
                    sent_to.add(tid)
                    await asyncio.sleep(0.05)
                except TelegramAPIError as e:
                    logger.error(f"Notify teacher/admin {tid} about question {q_id} fail: {e}")
        except Exception as e:
            logger.error(f"Error during Q&A notification phase: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Ask Q err user_db_id={submitter_db_id}: {e}", exc_info=True)
        await message.answer("❌ Ошибка при отправке вопроса. Попробуйте позже.", reply_markup=kbd)
        await state.clear()


# -------------------- Student: Grades --------------------
@student_router.message(F.text == "📊 Мои Оценки")
async def student_view_my_grades(
    message: Message, user_role: str, user_db_id: int, db_pool: asyncpg.Pool
):
    is_admin = message.from_user.id in ADMIN_IDS
    if user_role != "student" and not is_admin:
        await message.reply("Эта функция доступна только студентам.")
        return

    submitter_db_id = user_db_id
    if not submitter_db_id:
        logger.error(f"Grade view attempt without db_id: TG_ID={message.from_user.id}")
        await message.reply("❌ Ошибка: не удалось определить ваш ID. Попробуйте /start.")
        return

    try:
        async with db_pool.acquire() as conn:
            grades = await api.get_grades_for_student(conn, submitter_db_id)

        if not grades:
            await message.answer("ℹ️ У вас пока нет оцененных работ.")
            return

        response_lines = ["📊 <b>Ваши оценки:</b>\n"]
        for g in grades:
            title = html.quote(g.get('assignment_title', 'Неизвестное задание'))
            grade_val = g.get('grade')
            comment = html.quote(g.get('teacher_comment', '')).strip()

            line = f"📄 <b>{title}</b>: "
            if grade_val is not None:
                line += f"<b>{grade_val}/20</b>"
                if comment:
                    line += f" ({comment})"
            elif comment:
                line += f"<i>Проверено</i> ({comment})"
            else:
                line += "<i>На проверке</i>"
            response_lines.append(line)

        full_response = "\n".join(response_lines)
        MAX_LEN = 4096
        if len(full_response) <= MAX_LEN:
            await message.answer(full_response, parse_mode=ParseMode.HTML)
        else:
            for i in range(0, len(full_response), MAX_LEN):
                await message.answer(full_response[i: i + MAX_LEN], parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка получения оценок для user_db_id={submitter_db_id}: {e}", exc_info=True)
        await message.answer("❌ Не удалось загрузить ваши оценки. Попробуйте позже.")


# -------------------- Профиль: смена группы/имени --------------------
async def change_group_start_from_profile_message(
    message: Message, state: FSMContext, user_info: dict, db_pool: asyncpg.Pool
):
    user_db_id = user_info.get("user_id")
    if not user_db_id:
        await message.reply("Ошибка: не найден ID пользователя.")
        return

    current_group_id = user_info.get("group_id")
    pending_group_id = user_info.get("pending_group_id")
    pending_group_name = user_info.get("pending_group_name")

    if pending_group_id:
        p_group_name = pending_group_name or f"ID {pending_group_id}"
        await message.answer(
            f"⏳ Ваш запрос на смену группы в <b>{html.quote(p_group_name)}</b> уже отправлен.",
            parse_mode=ParseMode.HTML
        )
        return

    kbd = await get_groups_keyboard(db_pool, "change")
    valid_rows = []
    if kbd and getattr(kbd, "inline_keyboard", None):
        for row in kbd.inline_keyboard:
            new_row = [b for b in row if not b.callback_data.endswith(f"_{current_group_id}")]
            if new_row:
                valid_rows.append(new_row)

    if not valid_rows:
        await message.answer("ℹ️ Нет других доступных групп для смены.")
        return

    filtered_kbd = InlineKeyboardMarkup(inline_keyboard=valid_rows)
    await message.answer(
        "🔄 Выберите <b>новую группу</b> из списка:",
        reply_markup=filtered_kbd,
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(StudentActions.changing_group)


@student_router.callback_query(StudentActions.changing_group, F.data.startswith("change_to_group_"))
async def process_group_change_request(query: CallbackQuery, state: FSMContext, user_db_id: int, db_pool: asyncpg.Pool):
    if not user_db_id:
        logger.error(f"Group change request without db_id: TG_ID={query.from_user.id}")
        await query.answer("❌ Ошибка: не удалось определить ваш ID.", show_alert=True)
        await state.clear()
        return

    try:
        new_group_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        await query.message.edit_text("❌ Ошибка выбора группы.")
        await state.clear()
        return

    group_name = f"группу ID {new_group_id}"
    try:
        async with db_pool.acquire() as conn:
            new_group = await api.get_group_by_id(conn, new_group_id)
            if new_group:
                group_name = f"группу «{new_group['name']}»"
            success = await api.request_group_change(conn, user_db_id, new_group_id)
            if success:
                await query.message.edit_text(
                    f"✅ Ваш запрос на переход в <b>{html.quote(group_name)}</b> отправлен администратору.",
                    parse_mode=ParseMode.HTML
                )
            else:
                user_check = await api.get_user_by_db_id(conn, user_db_id)
                if user_check and user_check.get("pending_group_id") == new_group_id:
                    await query.message.edit_text(
                        f"⏳ Запрос на переход в <b>{html.quote(group_name)}</b> уже отправлен и ждёт одобрения.",
                        parse_mode=ParseMode.HTML
                    )
                elif user_check and user_check.get("pending_group_id"):
                    await query.message.edit_text(
                        f"❌ У вас уже есть активный запрос на смену группы. Дождитесь рассмотрения.",
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await query.message.edit_text("❌ Не удалось отправить запрос. Попробуйте позже.")
    except asyncpg.exceptions.ForeignKeyViolationError:
        await query.message.edit_text("❌ Выбранная группа не найдена.")
    except Exception as e:
        logger.error(f"Group change req err user_db_id={user_db_id}, group={new_group_id}: {e}", exc_info=True)
        await query.message.answer("❌ Ошибка при отправке запроса.")
    finally:
        await state.clear()
        with suppress(Exception):
            await query.answer()


async def change_name_start_from_profile_message(message: Message, state: FSMContext, user_info: dict, db_pool: asyncpg.Pool):
    user_db_id = user_info.get("user_id")
    if not user_db_id:
        await message.reply("Ошибка: не найден ID пользователя.")
        return

    pending_first = user_info.get("pending_first_name")
    pending_last = user_info.get("pending_last_name")
    try:
        async with db_pool.acquire() as conn:
            u = await api.get_user_by_db_id(conn, user_db_id)
            if u:
                pending_first = u.get("pending_first_name")
                pending_last = u.get("pending_last_name")
    except Exception as e:
        logger.warning(f"Failed to refresh pending name status for {user_db_id}: {e}")

    if pending_first:
        await message.answer(
            f"⏳ Запрос на смену имени на <b>{html.quote(pending_first)} {html.quote(pending_last or '')}</b> уже отправлен.",
            parse_mode=ParseMode.HTML
        )
        return

    await message.answer("👤 Введите <b>новое Имя</b>:", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.HTML)
    await state.set_state(StudentActions.changing_name_first)


@student_router.message(Command("cancel"), StateFilter(StudentActions.changing_name_first, StudentActions.changing_name_last))
async def cancel_name_change(message: Message, state: FSMContext, user_role: str):
    await state.clear()
    is_admin = message.from_user.id in ADMIN_IDS
    kbd = get_admin_main_keyboard() if is_admin else get_student_main_keyboard()
    await message.answer("Смена имени отменена.", reply_markup=kbd)


@student_router.message(StudentActions.changing_name_first, F.text)
async def process_change_name_first(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) > 100:
        await message.reply("❌ Некорректное имя (макс. 100 символов). Попробуйте ещё раз или /cancel:")
        return
    await state.update_data(new_first_name=name)
    await message.answer("Теперь введите <b>новую Фамилию</b>:", parse_mode=ParseMode.HTML)
    await state.set_state(StudentActions.changing_name_last)


@student_router.message(StudentActions.changing_name_last, F.text)
async def process_change_name_last(message: Message, state: FSMContext, user_db_id: int, db_pool: asyncpg.Pool):
    if not user_db_id:
        logger.error(f"Name change request without db_id: TG_ID={message.from_user.id}")
        await message.reply("❌ Ошибка: не удалось определить ваш ID.")
        await state.clear()
        return

    last_name = message.text.strip()
    if not last_name or len(last_name) > 100:
        await message.reply("❌ Некорректная фамилия (макс. 100 символов). Попробуйте ещё раз или /cancel:")
        return

    data = await state.get_data()
    first_name = data.get("new_first_name")
    if not first_name:
        await message.reply("❌ Ошибка состояния. Начните заново /start.")
        await state.clear()
        return

    is_admin = message.from_user.id in ADMIN_IDS
    kbd = get_admin_main_keyboard() if is_admin else get_student_main_keyboard()

    try:
        async with db_pool.acquire() as conn:
            success = await api.request_name_change(conn, user_db_id, first_name, last_name)
        if success:
            await message.answer(
                f"✅ Запрос на смену имени на <b>{html.quote(first_name)} {html.quote(last_name)}</b> отправлен администратору.",
                reply_markup=kbd,
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer("❌ Не удалось отправить запрос. Возможно, уже есть активный запрос.", reply_markup=kbd)
    except Exception as e:
        logger.error(f"Name change err user_db_id={user_db_id}: {e}", exc_info=True)
        await message.answer("❌ Ошибка при отправке запроса.", reply_markup=kbd)
    finally:
        await state.clear()


# -------------------- Материалы --------------------
@materials_router.message(F.text == "📁 Материалы")
async def open_materials_menu(message: Message, user_role: str):
    if user_role == "student" or message.from_user.id in ADMIN_IDS:
        await message.answer("Выберите раздел материалов:", reply_markup=get_materials_menu_keyboard())


@materials_router.message(F.text.in_({"📘 Лекции", "📣 Объявления", "📊 Графики/Рисунки", "🎬 Видео", "🔗 Ссылки", "📚 Библиотека"}))
async def show_materials_by_category(message: Message):
    title_to_key = {v: k for k, v in MATERIAL_CATEGORIES.items()}
    category_key = title_to_key.get(message.text)
    if not category_key:
        await message.reply("Неизвестная категория.")
        return

    user_id = message.from_user.id
    category_storage = LAST_MATERIALS_BY_CAT.get(user_id, {})
    items = category_storage.get(category_key, [])
    category_name = MATERIAL_CATEGORIES.get(category_key, "Материалы")

    if not items:
        await message.answer(f"ℹ️ В разделе «{category_name}» пока нет новых материалов.")
        return

    text = f"🆕 <b>Последние материалы — {category_name}</b> (до {MAX_LAST_MATERIALS} шт.):\n\n"
    text += "\n".join(f"• {html.quote(summary)}" for summary in items)
    text += "\n\n📌 Новые материалы обычно закрепляются вверху чата для быстрого доступа."
    await message.answer(text, parse_mode=ParseMode.HTML)


# -------------------- Очередь действий от backend --------------------
def _fmt_grade_message(title: str, grade: Optional[int], comment: Optional[str]) -> str:
    txt = f"✅ <b>Оценка за «{html.quote(title or 'Задание')}»</b>\n\n"
    grade_str = f"<b>{grade}/20</b>" if grade is not None else "<i>Без оценки</i>"
    comment_str = f"\nКомментарий: {html.quote(comment)}" if comment else ""
    txt += grade_str + comment_str
    if grade is None and not comment:
        txt += "Статус: <b>Проверено</b>"
    return txt


def _remember_material(telegram_id: int, summary: str):
    arr = LAST_MATERIALS.setdefault(telegram_id, [])
    if not arr or arr[-1] != summary:
        arr.append(summary)
    if len(arr) > MAX_LAST_MATERIALS:
        del arr[0: len(arr) - MAX_LAST_MATERIALS]


def _remember_material_by_cat(telegram_id: int, category: str, summary: str):
    cat_map = LAST_MATERIALS_BY_CAT.setdefault(telegram_id, {})
    arr = cat_map.setdefault(category, [])
    if not arr or arr[-1] != summary:
        arr.append(summary)
    if len(arr) > MAX_LAST_MATERIALS:
        del arr[0: len(arr) - MAX_LAST_MATERIALS]


async def _send_and_pin(
    bot: Bot,
    chat_id: int,
    text: Optional[str] = None,
    file_id: Optional[str] = None,
    file_type: Optional[str] = None,
    parse_mode: Optional[ParseMode] = ParseMode.HTML,
    disable_notification: bool = False,
    pin_message: bool = True
) -> Optional[int]:
    sent_message: Optional[Message] = None
    try:
        if file_id and file_id.startswith("local:"):
            logger.warning(f"Cannot send local file {file_id}. Sending text only for chat {chat_id}.")
            file_id = None

        MAX_CAPTION = 1024
        caption = text or ""
        if len(caption) > MAX_CAPTION:
            caption = caption[:MAX_CAPTION - 3] + "..."
            logger.warning(f"Caption truncated for chat {chat_id}, file {file_id}")

        if file_id:
            effective_type = file_type or "document"
            try:
                if effective_type == "photo":
                    sent_message = await bot.send_photo(chat_id, file_id, caption=caption, parse_mode=parse_mode)
                elif effective_type == "video":
                    sent_message = await bot.send_video(chat_id, file_id, caption=caption, parse_mode=parse_mode)
                else:
                    sent_message = await bot.send_document(chat_id, file_id, caption=caption, parse_mode=parse_mode)
            except TelegramAPIError as send_error:
                logger.error(f"Failed to send file {file_id} ({effective_type}) to {chat_id}: {send_error}")
                if text:
                    logger.info(f"Falling back to sending text only to {chat_id}")
                else:
                    return None

        if text and not sent_message:
            MAX_TEXT = 4096
            if len(text) <= MAX_TEXT:
                try:
                    sent_message = await bot.send_message(chat_id, text, parse_mode=parse_mode)
                except TelegramAPIError as text_error:
                    logger.error(f"Failed to send text message to {chat_id}: {text_error}")
                    return None
            else:
                pin_message = False
                try:
                    for i in range(0, len(text), MAX_TEXT):
                        await bot.send_message(chat_id, text[i: i + MAX_TEXT], parse_mode=parse_mode)
                        await asyncio.sleep(0.1)
                    return None
                except TelegramAPIError as chunk_error:
                    logger.error(f"Failed to send text chunk to {chat_id}: {chunk_error}")
                    return None

        if sent_message and pin_message:
            try:
                await bot.pin_chat_message(chat_id, sent_message.message_id, disable_notification=disable_notification)
            except TelegramAPIError as pin_error:
                logger.warning(f"Failed to pin message {sent_message.message_id} in chat {chat_id}: {pin_error}")

        return sent_message.message_id if sent_message else None

    except Exception as e:
        logger.error(f"Unexpected error in _send_and_pin for {chat_id}: {e}", exc_info=True)
        return None


async def process_assignment_queue(bot: Bot, db_pool: asyncpg.Pool):
    queue_file = "/tmp/bot_queue.json"
    while True:
        await asyncio.sleep(2)
        current_queue = []
        if os.path.exists(queue_file):
            try:
                with open(queue_file, "r+", encoding="utf-8") as f:
                    try:
                        content = f.read()
                        if content:
                            current_queue = json.loads(content)
                        else:
                            current_queue = []
                        f.seek(0)
                        f.truncate()
                        if not isinstance(current_queue, list):
                            current_queue = []
                    except json.JSONDecodeError:
                        logger.error(f"Error decoding JSON from {queue_file}. File cleared.")
                        f.seek(0)
                        f.truncate()
                        current_queue = []
                    except Exception as read_err:
                        logger.error(f"Error reading/clearing queue file {queue_file}: {read_err}")
                        current_queue = []
            except Exception as outer_err:
                logger.error(f"Unexpected error accessing queue file {queue_file}: {outer_err}")
                current_queue = []

        if not current_queue:
            continue
        logger.info(f"Processing {len(current_queue)} actions from queue...")

        for item in current_queue:
            action_id = item.get("timestamp", str(uuid.uuid4()))
            action = item.get("action")
            data = item.get("data") or {}
            try:
                if action == "send_assignment_to_group":
                    gid, aid, title = data.get("group_id"), data.get("assignment_id"), data.get("title", "Задание")
                    if not gid or not aid:
                        logger.warning(f"Skipping {action}: missing gid/aid")
                        continue
                    async with db_pool.acquire() as conn:
                        students = await api.get_users(conn, role="student", group_id=gid, approved=True)
                    if not students:
                        logger.warning(f"No students for {action} {aid} in group {gid}")
                        continue
                    text = f"🔔 <b>Новое задание: {html.quote(title)}</b>\n📌 ID Задания: {aid}\n\n..."
                    sent_count = 0
                    for st in students:
                        tid = st.get("telegram_id")
                        if tid:
                            mid = await _send_and_pin(bot, tid, text=text, file_id=data.get("file_id"), file_type=data.get("file_type"), pin_message=True)
                            if mid:
                                sent_count += 1
                            await asyncio.sleep(0.04)
                    logger.info(f"Sent {action} {aid} to {sent_count}/{len(students)} students in group {gid}.")

                elif action == "send_material_to_group":
                    gid, cat, title = data.get("group_id"), data.get("category", "other"), data.get("title", "Материал")
                    if not gid:
                        logger.warning(f"Skipping {action}: missing gid")
                        continue
                    async with db_pool.acquire() as conn:
                        students = await api.get_users(conn, role="student", group_id=gid, approved=True)
                    if not students:
                        logger.warning(f"No students for {action} '{title}' in group {gid}")
                        continue
                    body = f"🆕 <b>{html.quote(MATERIAL_CATEGORIES.get(cat, cat.capitalize()))}: {html.quote(title)}</b>\n\n..."
                    files = data.get("files", [])
                    fid, ftype = (files[0]['file_id'], files[0].get('file_type')) if files else (None, None)
                    sent_count = 0
                    for st in students:
                        tid = st.get("telegram_id")
                        if tid:
                            mid = await _send_and_pin(bot, tid, text=body, file_id=fid, file_type=ftype, pin_message=data.get("pin", True))
                            if mid:
                                sent_count += 1
                            await asyncio.sleep(0.04)
                    logger.info(f"Sent {action} '{title}' to {sent_count}/{len(students)} students in group {gid}.")

                elif action == "send_grade_to_student":
                    st_tid = data.get("student_telegram_id")
                    if st_tid:
                        msg = _fmt_grade_message(data.get("assignment_title", "?"), data.get("grade"), data.get("comment"))
                        try:
                            await bot.send_message(st_tid, msg, parse_mode=ParseMode.HTML)
                            logger.info(f"Sent grade to {st_tid}")
                        except TelegramAPIError as e:
                            logger.error(f"Failed send grade to {st_tid}: {e}")
                    else:
                        logger.warning(f"Skipping {action}: missing student_telegram_id")

                elif action == "resend_submission_to_admin":
                    admin_tid, fid = data.get("admin_telegram_id"), data.get("file_id")
                    if admin_tid and fid:
                        try:
                            await _send_and_pin(bot, admin_tid, text=data.get("caption"), file_id=fid, file_type=data.get("file_type"), pin_message=False)
                            logger.info(f"Resent submission {fid} to admin {admin_tid}")
                        except Exception as e:
                            logger.error(f"Error resending submission to admin {admin_tid}: {e}")
                    else:
                        logger.warning(f"Skipping {action}: missing admin_tid/file_id")

                elif action == "notify_answer":
                    st_tid = data.get("student_telegram_id")
                    if st_tid:
                        msg = f"💡 <b>Ответ на ваш вопрос</b>\n\n<b>Вопрос:</b> <i>{html.quote(data.get('question_text','?'))}</i>\n\n<b>Ответ:</b> {html.quote(data.get('answer_text','?'))}"
                        try:
                            await bot.send_message(st_tid, msg, parse_mode=ParseMode.HTML)
                            logger.info(f"Sent answer to {st_tid}")
                        except TelegramAPIError as e:
                            logger.error(f"Failed send answer to {st_tid}: {e}")
                    else:
                        logger.warning(f"Skipping {action}: missing student_telegram_id")

                elif action == "notify_user_approval":
                    st_tid = data.get("student_telegram_id")
                    if st_tid:
                        builder = InlineKeyboardBuilder()
                        builder.button(text=data.get("button_text", "Меню"), callback_data="open_menu")
                        markup = builder.as_markup()
                        text = "✅ <b>Ваша регистрация одобрена!</b>\n\nТеперь вам доступно главное меню и функции бота."
                        try:
                            await bot.send_message(st_tid, text, parse_mode=ParseMode.HTML, reply_markup=markup)
                            logger.info(f"Sent approval to {st_tid}")
                        except TelegramAPIError as e:
                            logger.error(f"Failed send approval to {st_tid}: {e}")
                    else:
                        logger.warning(f"Skipping {action}: missing student_telegram_id")

                else:
                    logger.warning(f"Unknown queue action: '{action}'. Skipping.")

            except Exception as e:
                logger.error(f"Error processing action '{action}' (ID: {action_id}): {e}", exc_info=True)

# -------------------- Админские --------------------
@teacher_router.message(F.text == "⚙️ Админ-панель")
async def admin_open_webapp(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("Эта кнопка доступна только администраторам.")
        return
    if not WEBAPP_URL:
        await message.answer("❌ URL Веб-приложения не настроен в конфигурации бота.")
        return
    await message.answer("Открываю админ-панель...", reply_markup=get_admin_main_keyboard())


@teacher_router.message(F.web_app_data)
async def handle_webapp_data(message: Message, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        logger.warning(f"Received web_app_data from non-admin {message.from_user.id}. Ignoring.")
        return
    try:
        data_str = message.web_app_data.data
        data = json.loads(data_str)
        action = data.get("action")
        logger.info(f"Received WebApp data from {message.from_user.id}: action={action}")
        if action:
            await message.answer(f"✅ Данные WebApp (действие: {action}) получены и обрабатываются сервером.")
        else:
            await message.answer(f"ℹ️ Получены данные из WebApp.")
            logger.info(f"Received unknown WebApp data structure: {data_str}")
    except json.JSONDecodeError:
        logger.error(f"WebApp JSON decode error from {message.from_user.id}: {message.web_app_data.data}")
        await message.answer("❌ Ошибка обработки данных из WebApp (неверный JSON).")
    except Exception as e:
        logger.error(f"WebApp data handling error from {message.from_user.id}: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при обработке данных из WebApp.")


# -------------------- Новые материалы (кнопка) — общий список --------------------
@main_router.message(Command("materials"))
async def show_new_materials(message: Message):
    user_id = message.from_user.id
    items = LAST_MATERIALS.get(user_id, [])
    if not items:
        await message.answer(
            "ℹ️ Новых общих материалов пока нет.\n"
            "Используйте меню '📁 Материалы' для просмотра по категориям."
        )
        return
    text = f"🆕 <b>Последние материалы ({len(items)} шт.)</b>:\n\n"
    text += "\n".join(f"• {html.quote(summary)}" for summary in items)
    text += "\n\n📌 Новые материалы обычно закрепляются вверху чата."
    await message.answer(text, parse_mode=ParseMode.HTML)


# -------------------- Точка входа --------------------
async def main():
    global bot_instance

    if not BOT_TOKEN:
        logger.critical("TELEGRAM_API_TOKEN не задан!")
        return
    if not DATABASE_URL:
        logger.critical("DATABASE_URL не задан!")
        return
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не указаны!")
    if not WEBAPP_URL:
        logger.warning("WEBAPP_URL не задан.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_instance = bot
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        logger.info("✅ База данных успешно подключена.")
    except Exception as e:
        logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА: Не удалось подключиться к БД: {e}", exc_info=True)
        return

    with suppress(Exception):
        await bot.set_chat_menu_button(menu_button=types.MenuButtonDefault())

    dp.update.outer_middleware(ApprovalMiddleware())

    dp.include_router(registration_router)
    dp.include_router(profile_router)
    dp.include_router(materials_router)
    dp.include_router(teacher_router)
    dp.include_router(student_router)
    dp.include_router(main_router)

    @dp.message()
    async def handle_unknown(message: Message, state: FSMContext, user_role: Optional[str] = None, user_info: Optional[dict] = None):
        current_state = await state.get_state()
        if current_state:
            if current_state.startswith(StudentActions.__name__):
                await message.reply("Пожалуйста, следуйте инструкциям или используйте /cancel для отмены.")
            elif current_state.startswith(Registration.__name__):
                await message.reply("Пожалуйста, завершите регистрацию или начните заново с /start.")
            else:
                await message.reply("Неизвестное состояние. Используйте /cancel или /start.")
            return
        logger.info(f"Unhandled message from {message.from_user.id}: {message.text[:50]}")
        await show_menu(message, user_role=user_role, user_info=user_info)

    logger.info("🚀 Запуск бота...")
    pool = await get_db_pool()
    queue_task = asyncio.create_task(process_assignment_queue(bot, pool))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except (KeyboardInterrupt, SystemExit):
        logger.info("⏹ Остановка бота...")
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка в polling: {e}", exc_info=True)
    finally:
        logger.info("Завершение фоновых задач...")
        queue_task.cancel()
        with suppress(asyncio.CancelledError):
            await queue_task
        logger.info("Закрытие сессии бота...")
        if bot and bot.session:
            await bot.session.close()
        logger.info("Закрытие пула БД...")
        global db_pool
        if db_pool:
            await db_pool.close()
            db_pool = None
            logger.info("✅ Пул БД закрыт.")
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот принудительно остановлен (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка при запуске main(): {e}", exc_info=True)
