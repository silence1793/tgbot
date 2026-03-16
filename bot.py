import os
import asyncio
from datetime import datetime

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "repairs.db"

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN")

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🆕 Новый ремонт")],
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="🔎 Найти")],
    ],
    resize_keyboard=True
)


class AddRepair(StatesGroup):
    waiting_photo = State()
    waiting_data = State()


class AddHistory(StatesGroup):
    waiting_photo = State()
    waiting_data = State()


class FindRepair(StatesGroup):
    waiting_seal = State()


def today_str():
    return datetime.now().strftime("%d.%m.%Y")


def delete_card_kb(parent_repair_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить карточку",
                    callback_data=f"delete_card:{parent_repair_id}"
                )
            ]
        ]
    )


async def send_main_menu(message: Message | CallbackQuery, text: str = "Выбери действие:"):
    if isinstance(message, CallbackQuery):
        return await message.message.answer(text, reply_markup=main_kb)
    return await message.answer(text, reply_markup=main_kb)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS repairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER,
                photo_file_id TEXT,
                seal_number TEXT NOT NULL,
                work_done TEXT NOT NULL,
                amount TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS repair_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_repair_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                user_id INTEGER,
                photo_file_id TEXT,
                seal_number TEXT NOT NULL,
                work_done TEXT NOT NULL,
                amount TEXT,
                FOREIGN KEY(parent_repair_id) REFERENCES repairs(id)
            )
        """)

        await db.commit()


async def save_repair(user_id: int, photo_file_id: str, seal_number: str, work_done: str, amount: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO repairs (
                created_at, user_id, photo_file_id, seal_number, work_done, amount
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            today_str(),
            user_id,
            photo_file_id,
            seal_number,
            work_done,
            amount
        ))
        await db.commit()
        return cursor.lastrowid


async def save_history(parent_repair_id: int, user_id: int, photo_file_id: str, seal_number: str, work_done: str, amount: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO repair_history (
                parent_repair_id, created_at, user_id, photo_file_id, seal_number, work_done, amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            parent_repair_id,
            today_str(),
            user_id,
            photo_file_id,
            seal_number,
            work_done,
            amount
        ))
        await db.commit()


async def get_today_repairs(user_id: int):
    today = today_str()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, created_at, seal_number, work_done, amount
            FROM repairs
            WHERE created_at = ? AND user_id = ?
            ORDER BY id DESC
        """, (today, user_id))
        return await cursor.fetchall()


async def get_repair_by_any_seal(user_id: int, seal_number: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, created_at, photo_file_id, seal_number, work_done, amount
            FROM repairs
            WHERE seal_number = ? AND user_id = ?
            LIMIT 1
        """, (seal_number, user_id))
        row = await cursor.fetchone()
        if row:
            return row

        cursor = await db.execute("""
            SELECT r.id, r.created_at, r.photo_file_id, r.seal_number, r.work_done, r.amount
            FROM repair_history h
            JOIN repairs r ON r.id = h.parent_repair_id
            WHERE h.seal_number = ? AND r.user_id = ?
            ORDER BY h.id DESC
            LIMIT 1
        """, (seal_number, user_id))
        return await cursor.fetchone()


async def get_repair_history(user_id: int, parent_repair_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT created_at, photo_file_id, seal_number, work_done, amount, 'main' as record_type, 0 as sort_id
            FROM repairs
            WHERE id = ? AND user_id = ?

            UNION ALL

            SELECT h.created_at, h.photo_file_id, h.seal_number, h.work_done, h.amount, 'history' as record_type, h.id as sort_id
            FROM repair_history h
            JOIN repairs r ON r.id = h.parent_repair_id
            WHERE h.parent_repair_id = ? AND r.user_id = ?

            ORDER BY sort_id ASC
        """, (parent_repair_id, user_id, parent_repair_id, user_id))
        return await cursor.fetchall()


async def delete_repair_card(user_id: int, parent_repair_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id
            FROM repairs
            WHERE id = ? AND user_id = ?
        """, (parent_repair_id, user_id))
        row = await cursor.fetchone()

        if not row:
            return False

        await db.execute("DELETE FROM repair_history WHERE parent_repair_id = ?", (parent_repair_id,))
        await db.execute("DELETE FROM repairs WHERE id = ? AND user_id = ?", (parent_repair_id, user_id))
        await db.commit()
        return True


def parse_new_repair_line(text: str):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        return None

    seal_number, amount, work_done = parts
    if not seal_number or not amount or not work_done:
        return None

    return {
        "seal_number": seal_number,
        "amount": amount,
        "work_done": work_done
    }


def parse_history_line(text: str):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        return None

    seal_number, amount, work_done = parts
    if not seal_number or not amount or not work_done:
        return None

    return {
        "seal_number": seal_number,
        "amount": amount,
        "work_done": work_done
    }


async def safe_delete_message(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


async def safe_delete_by_id(bot: Bot, chat_id: int, message_id):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет.\n\n"
        "🆕 Новый ремонт — создать запись\n"
        "🔎 Найти — поиск по пломбе\n"
        "📅 Сегодня — записи за сегодня\n\n"
        "Для повторного ремонта:\n"
        "<code>/add 1542</code>",
        reply_markup=main_kb
    )


@dp.message(Command("today"))
@dp.message(F.text == "📅 Сегодня")
async def cmd_today(message: Message):
    rows = await get_today_repairs(message.from_user.id)
    if not rows:
        await message.answer("За сегодня записей нет.", reply_markup=main_kb)
        return

    parts = ["<b>Записи за сегодня:</b>\n"]
    for row in rows[:20]:
        _, created_at, seal_number, work_done, amount = row
        parts.append(
            f"• <b>{created_at}</b>\n"
            f"Пломба: {seal_number}\n"
            f"Сумма: {amount}\n"
            f"Ремонт: {work_done}\n"
        )

    await message.answer("\n".join(parts), reply_markup=main_kb)


async def show_card_by_seal(message: Message, user_id: int, seal_number: str):
    repair = await get_repair_by_any_seal(user_id, seal_number)

    if not repair:
        await message.answer(f"По пломбе <b>{seal_number}</b> ничего не найдено.", reply_markup=main_kb)
        return

    parent_repair_id = repair[0]
    history_rows = await get_repair_history(user_id, parent_repair_id)

    if not history_rows:
        await message.answer(f"По пломбе <b>{seal_number}</b> ничего не найдено.", reply_markup=main_kb)
        return

    first_row = history_rows[0]
    base_seal = first_row[2]

    header = await message.answer(
        f"<b>История по карточке</b>\n"
        f"Основная пломба: <b>{base_seal}</b>\n"
        f"Этапов: <b>{len(history_rows)}</b>",
        reply_markup=main_kb
    )

    shown_ids = [header.message_id]

    for index, row in enumerate(history_rows, start=1):
        created_at, photo_file_id, hist_seal, work_done, amount, record_type, _ = row

        stage_title = f"Этап {index} (первичный ремонт)" if record_type == "main" else f"Этап {index} (повторный ремонт)"

        caption = (
            f"<b>{stage_title}</b>\n"
            f"<b>Дата:</b> {created_at}\n"
            f"<b>Пломба:</b> {hist_seal}\n"
            f"<b>Сумма:</b> {amount}\n"
            f"<b>Ремонт:</b> {work_done}"
        )

        if photo_file_id:
            msg = await message.answer_photo(photo=photo_file_id, caption=caption)
        else:
            msg = await message.answer(caption)

        shown_ids.append(msg.message_id)

    action_msg = await message.answer(
        "Что сделать с этой карточкой?",
        reply_markup=delete_card_kb(parent_repair_id)
    )
    shown_ids.append(action_msg.message_id)

    return shown_ids


@dp.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Напиши так: <code>/find 1542</code>", reply_markup=main_kb)
        return

    seal_number = command.args.strip()
    await show_card_by_seal(message, message.from_user.id, seal_number)


@dp.message(F.text == "🔎 Найти")
async def find_button(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FindRepair.waiting_seal)
    await message.answer("Введите номер пломбы:", reply_markup=main_kb)


@dp.message(FindRepair.waiting_seal)
async def process_find_seal(message: Message, state: FSMContext):
    seal_number = (message.text or "").strip()

    if not seal_number:
        await message.answer("Введите номер пломбы:", reply_markup=main_kb)
        return

    await state.clear()
    await show_card_by_seal(message, message.from_user.id, seal_number)


@dp.callback_query(F.data.startswith("delete_card:"))
async def delete_card_callback(callback: CallbackQuery):
    try:
        parent_repair_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("Ошибка удаления", show_alert=True)
        return

    deleted = await delete_repair_card(callback.from_user.id, parent_repair_id)

    if not deleted:
        await callback.answer("Карточка не найдена или уже удалена", show_alert=True)
        return

    try:
        await callback.message.edit_text("🗑 Карточка удалена из базы")
    except Exception:
        pass

    await callback.answer("Удалено")
    await send_main_menu(callback)


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, state: FSMContext):
    if not command.args:
        await message.answer("Напиши так: <code>/add 1542</code>", reply_markup=main_kb)
        return

    seal_number = command.args.strip()
    repair = await get_repair_by_any_seal(message.from_user.id, seal_number)

    if not repair:
        await message.answer(
            f"Не нашел карточку по пломбе <b>{seal_number}</b>.\n"
            f"Сначала создай первую запись обычным ремонтом.",
            reply_markup=main_kb
        )
        return

    parent_repair_id = repair[0]
    await state.clear()
    await state.update_data(parent_repair_id=parent_repair_id)

    ask_msg = await message.answer(
        f"Карточка найдена по пломбе <b>{seal_number}</b>.\n\n"
        f"Теперь отправь новое фото для повторного ремонта.",
        reply_markup=main_kb
    )

    await state.update_data(add_ask_message_id=ask_msg.message_id)
    await state.set_state(AddHistory.waiting_photo)


@dp.message(AddHistory.waiting_photo, F.photo)
async def handle_add_photo(message: Message, state: FSMContext):
    photo_file_id = message.photo[-1].file_id
    data = await state.get_data()
    old_ask_message_id = data.get("add_ask_message_id")

    await state.update_data(history_photo_file_id=photo_file_id)

    ask_msg = await message.answer(
        "Фото получено.\n\n"
        "Теперь напиши одной строкой:\n"
        "<code>новая пломба, сумма, что сделал</code>\n\n"
        "Пример:\n"
        "<code>1881, 2500, заменил разъем питания</code>",
        reply_markup=main_kb
    )

    await state.update_data(history_data_ask_message_id=ask_msg.message_id)
    await state.set_state(AddHistory.waiting_data)

    await safe_delete_message(message)
    await safe_delete_by_id(bot, message.chat.id, old_ask_message_id)


@dp.message(AddHistory.waiting_photo)
async def handle_add_photo_invalid(message: Message):
    warn_msg = await message.answer("Нужно отправить именно фото.", reply_markup=main_kb)
    await asyncio.sleep(3)
    await safe_delete_message(warn_msg)


@dp.message(AddHistory.waiting_data)
async def handle_add_data(message: Message, state: FSMContext):
    parsed = parse_history_line(message.text or "")

    if not parsed:
        warn_msg = await message.answer(
            "Не смог понять данные.\n\n"
            "Напиши строго так:\n"
            "<code>новая пломба, сумма, что сделал</code>\n\n"
            "Пример:\n"
            "<code>1881, 2500, заменил разъем питания</code>",
            reply_markup=main_kb
        )
        await asyncio.sleep(4)
        await safe_delete_message(warn_msg)
        return

    data = await state.get_data()
    parent_repair_id = data.get("parent_repair_id")
    photo_file_id = data.get("history_photo_file_id")
    ask_message_id = data.get("history_data_ask_message_id")

    if not parent_repair_id or not photo_file_id:
        await state.clear()
        msg = await message.answer("Что-то потерялось. Начни заново через /add 1542", reply_markup=main_kb)
        await asyncio.sleep(3)
        await safe_delete_message(message)
        await safe_delete_message(msg)
        return

    await save_history(
        parent_repair_id=parent_repair_id,
        user_id=message.from_user.id,
        photo_file_id=photo_file_id,
        seal_number=parsed["seal_number"],
        work_done=parsed["work_done"],
        amount=parsed["amount"]
    )

    await safe_delete_message(message)
    await safe_delete_by_id(bot, message.chat.id, ask_message_id)

    ok_msg = await message.answer(
        "✅ Повторный ремонт добавлен\n"
        f"Новая пломба: {parsed['seal_number']}\n"
        f"Сумма: {parsed['amount']}\n"
        f"Ремонт: {parsed['work_done']}",
        reply_markup=main_kb
    )

    await state.clear()
    await asyncio.sleep(2)
    await safe_delete_message(ok_msg)
    await send_main_menu(message)


@dp.message(F.text == "🆕 Новый ремонт")
async def new_repair_button(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddRepair.waiting_photo)
    await message.answer("Отправьте фото.", reply_markup=main_kb)


@dp.message(AddRepair.waiting_photo, F.photo)
async def handle_new_photo_from_state(message: Message, state: FSMContext):
    photo_file_id = message.photo[-1].file_id
    await state.update_data(photo_file_id=photo_file_id)

    ask_msg = await message.answer(
        "Фото получено.\n\n"
        "Теперь напиши одной строкой:\n"
        "<code>номер пломбы, сумма, тип ремонта</code>\n\n"
        "Пример:\n"
        "<code>1542, 3500, замена блока питания</code>",
        reply_markup=main_kb
    )

    await state.update_data(ask_message_id=ask_msg.message_id)
    await state.set_state(AddRepair.waiting_data)

    await safe_delete_message(message)


@dp.message(AddRepair.waiting_photo)
async def handle_new_photo_invalid(message: Message):
    warn_msg = await message.answer("Нужно отправить именно фото.", reply_markup=main_kb)
    await asyncio.sleep(3)
    await safe_delete_message(warn_msg)


@dp.message(AddRepair.waiting_data)
async def handle_new_data(message: Message, state: FSMContext):
    parsed = parse_new_repair_line(message.text or "")

    if not parsed:
        warn_msg = await message.answer(
            "Не смог понять данные.\n\n"
            "Напиши строго так:\n"
            "<code>номер пломбы, сумма, тип ремонта</code>\n\n"
            "Пример:\n"
            "<code>1542, 3500, замена блока питания</code>",
            reply_markup=main_kb
        )
        await asyncio.sleep(4)
        await safe_delete_message(warn_msg)
        return

    data = await state.get_data()
    photo_file_id = data.get("photo_file_id")
    ask_message_id = data.get("ask_message_id")

    if not photo_file_id:
        await state.clear()
        msg = await message.answer("Сначала отправь фото.", reply_markup=main_kb)
        await asyncio.sleep(3)
        await safe_delete_message(message)
        await safe_delete_message(msg)
        return

    await save_repair(
        user_id=message.from_user.id,
        photo_file_id=photo_file_id,
        seal_number=parsed["seal_number"],
        work_done=parsed["work_done"],
        amount=parsed["amount"]
    )

    await safe_delete_message(message)
    await safe_delete_by_id(bot, message.chat.id, ask_message_id)

    ok_msg = await message.answer(
        "✅ Запись сохранена\n"
        f"Пломба: {parsed['seal_number']}\n"
        f"Сумма: {parsed['amount']}\n"
        f"Ремонт: {parsed['work_done']}",
        reply_markup=main_kb
    )

    await state.clear()
    await asyncio.sleep(2)
    await safe_delete_message(ok_msg)
    await send_main_menu(message)


@dp.message(F.photo)
async def fallback_photo(message: Message):
    warn_msg = await message.answer(
        "Сначала нажми кнопку <b>🆕 Новый ремонт</b>.",
        reply_markup=main_kb
    )
    await asyncio.sleep(3)
    await safe_delete_message(warn_msg)


@dp.message()
async def fallback(message: Message):
    await send_main_menu(message)
    

async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())