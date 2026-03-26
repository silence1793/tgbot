import os
import asyncio
import csv
import json
import hmac
import hashlib
import io
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

import aiosqlite
from dotenv import load_dotenv
from aiohttp import web, ClientSession

from aiogram import Bot, Dispatcher, F
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
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
    FSInputFile,
    MenuButtonWebApp,
    WebAppInfo,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "repairs.db")
AUTO_DELETE_SECONDS = 300
CHAT_CLEANUP_SECONDS = 180
CHAT_SWEEP_BACK_MESSAGES = 5000
MAIN_MESSAGE_TEXT = "Я бот записи и учета ремонтов"
NO_SEAL_PREFIX = "__NOSEAL__"
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").rstrip("/")

chat_last_message_id: dict[int, int] = {}
chat_cleanup_tasks: dict[int, asyncio.Task] = {}
chat_main_message_id: dict[int, int] = {}
chat_user_ids: dict[int, int] = {}
webapp_menu_set_chats: set[int] = set()

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN")


def build_main_keyboard():
    rows = [
        [KeyboardButton(text="🆕 Новый ремонт"), KeyboardButton(text="🔎 Найти")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False
    )


main_kb = build_main_keyboard()


class AddRepair(StatesGroup):
    waiting_photo = State()
    waiting_data = State()


class AddHistory(StatesGroup):
    waiting_photo = State()
    waiting_data = State()


class AddQuick(StatesGroup):
    waiting_data = State()


class FindRepair(StatesGroup):
    waiting_seal = State()


class EditSeal(StatesGroup):
    waiting_new_seal = State()


def today_str():
    return datetime.now().strftime("%d.%m.%Y")


def parse_money(value: str | None) -> float:
    raw = (value or "").strip()
    if not raw or raw == "-":
        return 0.0
    normalized = raw.replace(" ", "").replace(",", ".")
    cleaned = re.sub(r"[^0-9.\-]", "", normalized)
    if cleaned.count(".") > 1:
        first = cleaned.find(".")
        cleaned = cleaned[:first + 1] + cleaned[first + 1:].replace(".", "")
    try:
        return float(cleaned)
    except Exception:
        return 0.0


def has_explicit_amount(value: str | None) -> bool:
    raw = (value or "").strip()
    if not raw or raw == "-":
        return False
    return any(ch.isdigit() for ch in raw)


def format_money(value: float) -> str:
    if abs(value - round(value)) < 0.000001:
        return str(int(round(value)))
    return f"{value:.2f}"


DEFAULT_USER_SETTINGS = {
    "expense_percent": 40,
    "show_seal": True,
    "show_amount": True,
    "show_work": True,
    "show_part_cost": True,
    "show_history": True,
    "chat_cleanup_enabled": True,
    "chat_cleanup_minutes": 3,
}


def normalize_expense_percent(value) -> int:
    try:
        percent = int(value)
    except Exception:
        return DEFAULT_USER_SETTINGS["expense_percent"]
    if percent not in (30, 35, 40, 45, 50):
        return DEFAULT_USER_SETTINGS["expense_percent"]
    return percent


def parse_bool_flag(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_chat_cleanup_minutes(value) -> int:
    try:
        minutes = int(value)
    except Exception:
        return DEFAULT_USER_SETTINGS["chat_cleanup_minutes"]
    if minutes not in (1, 3, 5, 10):
        return DEFAULT_USER_SETTINGS["chat_cleanup_minutes"]
    return minutes


def parse_card_date(value: str | None):
    try:
        return datetime.strptime((value or "").strip(), "%d.%m.%Y")
    except Exception:
        return None


def parse_iso_date(value: str | None):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def is_in_period(created_at: str | None, period_days: int) -> bool:
    dt = parse_card_date(created_at)
    if not dt:
        return False
    start_date = (datetime.now() - timedelta(days=period_days - 1)).date()
    return dt.date() >= start_date


def is_in_date_range(created_at: str | None, date_from=None, date_to=None) -> bool:
    dt = parse_card_date(created_at)
    if not dt:
        return False
    current = dt.date()
    if date_from and current < date_from:
        return False
    if date_to and current > date_to:
        return False
    return True


def matches_cabinet_filter(created_at: str | None, period_days: int | None = None, date_from=None, date_to=None) -> bool:
    if date_from or date_to:
        return is_in_date_range(created_at, date_from, date_to)
    return is_in_period(created_at, period_days or 7)


def make_virtual_seal() -> str:
    return f"{NO_SEAL_PREFIX}{int(datetime.now().timestamp() * 1000)}"


def is_virtual_seal(seal_number: str | None) -> bool:
    return (seal_number or "").startswith(NO_SEAL_PREFIX)


def display_seal(seal_number: str | None) -> str:
    if is_virtual_seal(seal_number):
        return "без пломбы"
    return (seal_number or "-").strip() or "-"


def resolve_local_photo_path(photo_ref: str | None) -> str | None:
    raw = (photo_ref or "").strip()
    if not raw:
        return None

    candidates: list[Path] = []
    raw_path = Path(raw)
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.extend([
            raw_path,
            Path("/home/admin") / raw,
            Path("/home/admin/ChatExport_2026-03-18") / raw,
            Path("/opt/tgbot") / raw,
        ])

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.exists() and candidate.is_file():
            return normalized
    return None


def resolve_message_photo(photo_ref: str | None):
    raw = (photo_ref or "").strip()
    if not raw:
        return None

    local_path = resolve_local_photo_path(raw)
    if local_path:
        return FSInputFile(local_path)

    if raw.startswith(("http://", "https://")):
        return raw

    if "/" in raw or "\\" in raw:
        return None

    return raw


def card_actions_kb(parent_repair_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить пломбу",
                    callback_data=f"edit_seal:{parent_repair_id}"
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить карточку",
                    callback_data=f"delete_card:{parent_repair_id}"
                )
            ]
        ]
    )


async def send_main_menu(message: Message | CallbackQuery, text: str = "Выбери действие:"):
    return None


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
                amount TEXT,
                part_cost TEXT
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
                part_cost TEXT,
                FOREIGN KEY(parent_repair_id) REFERENCES repairs(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS repair_seal_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_repair_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                seal_number TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(parent_repair_id, seal_number),
                FOREIGN KEY(parent_repair_id) REFERENCES repairs(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_main_messages (
                chat_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                expense_percent INTEGER NOT NULL DEFAULT 40,
                show_seal INTEGER NOT NULL DEFAULT 1,
                show_amount INTEGER NOT NULL DEFAULT 1,
                show_work INTEGER NOT NULL DEFAULT 1,
                show_part_cost INTEGER NOT NULL DEFAULT 1,
                show_history INTEGER NOT NULL DEFAULT 1,
                chat_cleanup_enabled INTEGER NOT NULL DEFAULT 1,
                chat_cleanup_minutes INTEGER NOT NULL DEFAULT 3
            )
        """)

        cursor = await db.execute("PRAGMA table_info(repairs)")
        repair_cols = {row[1] for row in await cursor.fetchall()}
        if "part_cost" not in repair_cols:
            await db.execute("ALTER TABLE repairs ADD COLUMN part_cost TEXT")

        cursor = await db.execute("PRAGMA table_info(repair_history)")
        history_cols = {row[1] for row in await cursor.fetchall()}
        if "part_cost" not in history_cols:
            await db.execute("ALTER TABLE repair_history ADD COLUMN part_cost TEXT")

        cursor = await db.execute("PRAGMA table_info(user_settings)")
        settings_cols = {row[1] for row in await cursor.fetchall()}
        if "expense_percent" not in settings_cols:
            await db.execute("ALTER TABLE user_settings ADD COLUMN expense_percent INTEGER NOT NULL DEFAULT 40")
        if "show_seal" not in settings_cols:
            await db.execute("ALTER TABLE user_settings ADD COLUMN show_seal INTEGER NOT NULL DEFAULT 1")
        if "show_amount" not in settings_cols:
            await db.execute("ALTER TABLE user_settings ADD COLUMN show_amount INTEGER NOT NULL DEFAULT 1")
        if "show_work" not in settings_cols:
            await db.execute("ALTER TABLE user_settings ADD COLUMN show_work INTEGER NOT NULL DEFAULT 1")
        if "show_part_cost" not in settings_cols:
            await db.execute("ALTER TABLE user_settings ADD COLUMN show_part_cost INTEGER NOT NULL DEFAULT 1")
        if "show_history" not in settings_cols:
            await db.execute("ALTER TABLE user_settings ADD COLUMN show_history INTEGER NOT NULL DEFAULT 1")
        if "chat_cleanup_enabled" not in settings_cols:
            await db.execute("ALTER TABLE user_settings ADD COLUMN chat_cleanup_enabled INTEGER NOT NULL DEFAULT 1")
        if "chat_cleanup_minutes" not in settings_cols:
            await db.execute("ALTER TABLE user_settings ADD COLUMN chat_cleanup_minutes INTEGER NOT NULL DEFAULT 3")

        await db.commit()


async def get_saved_main_message_id(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT message_id
            FROM chat_main_messages
            WHERE chat_id = ?
            LIMIT 1
        """, (chat_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return int(row[0])


async def get_saved_main_chat_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT chat_id
            FROM chat_main_messages
        """)
        rows = await cursor.fetchall()
        return [int(row[0]) for row in rows if row and row[0] is not None]


async def save_main_message_id(chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO chat_main_messages (chat_id, message_id)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET message_id = excluded.message_id
        """, (chat_id, message_id))
        await db.commit()

    chat_main_message_id[chat_id] = message_id


async def load_main_message_id(chat_id: int):
    message_id = chat_main_message_id.get(chat_id)
    if not message_id:
        message_id = await get_saved_main_message_id(chat_id)
        if message_id:
            chat_main_message_id[chat_id] = message_id
    return message_id


async def get_user_settings(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT expense_percent, show_seal, show_amount, show_work, show_part_cost, show_history,
                   chat_cleanup_enabled, chat_cleanup_minutes
            FROM user_settings
            WHERE user_id = ?
            LIMIT 1
        """, (user_id,))
        row = await cursor.fetchone()
        if not row:
            return DEFAULT_USER_SETTINGS.copy()
        return {
            "expense_percent": normalize_expense_percent(row[0]),
            "show_seal": bool(row[1]),
            "show_amount": bool(row[2]),
            "show_work": bool(row[3]),
            "show_part_cost": bool(row[4]),
            "show_history": bool(row[5]),
            "chat_cleanup_enabled": bool(row[6]),
            "chat_cleanup_minutes": normalize_chat_cleanup_minutes(row[7]),
        }


async def save_user_settings(user_id: int, settings: dict):
    merged = DEFAULT_USER_SETTINGS.copy()
    merged.update({
        "expense_percent": normalize_expense_percent(settings.get("expense_percent")),
        "show_seal": parse_bool_flag(settings.get("show_seal"), True),
        "show_amount": parse_bool_flag(settings.get("show_amount"), True),
        "show_work": parse_bool_flag(settings.get("show_work"), True),
        "show_part_cost": parse_bool_flag(settings.get("show_part_cost"), True),
        "show_history": parse_bool_flag(settings.get("show_history"), True),
        "chat_cleanup_enabled": parse_bool_flag(settings.get("chat_cleanup_enabled"), True),
        "chat_cleanup_minutes": normalize_chat_cleanup_minutes(settings.get("chat_cleanup_minutes")),
    })

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO user_settings (
                user_id,
                expense_percent,
                show_seal,
                show_amount,
                show_work,
                show_part_cost,
                show_history,
                chat_cleanup_enabled,
                chat_cleanup_minutes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expense_percent = excluded.expense_percent,
                show_seal = excluded.show_seal,
                show_amount = excluded.show_amount,
                show_work = excluded.show_work,
                show_part_cost = excluded.show_part_cost,
                show_history = excluded.show_history,
                chat_cleanup_enabled = excluded.chat_cleanup_enabled,
                chat_cleanup_minutes = excluded.chat_cleanup_minutes
        """, (
            user_id,
            merged["expense_percent"],
            int(merged["show_seal"]),
            int(merged["show_amount"]),
            int(merged["show_work"]),
            int(merged["show_part_cost"]),
            int(merged["show_history"]),
            int(merged["chat_cleanup_enabled"]),
            int(merged["chat_cleanup_minutes"]),
        ))
        await db.commit()

    return merged


async def ensure_main_message(chat_id: int):
    message_id = await load_main_message_id(chat_id)

    if message_id:
        return message_id

    msg = await bot.send_message(chat_id=chat_id, text=MAIN_MESSAGE_TEXT, reply_markup=main_kb)
    await save_main_message_id(chat_id, msg.message_id)
    return msg.message_id


async def refresh_main_message(chat_id: int):
    old_message_id = await load_main_message_id(chat_id)
    if old_message_id:
        await safe_delete_by_id(bot, chat_id, old_message_id, allow_main_message=True)

    msg = await bot.send_message(chat_id=chat_id, text=MAIN_MESSAGE_TEXT, reply_markup=main_kb)
    await save_main_message_id(chat_id, msg.message_id)
    return msg.message_id


async def refresh_saved_main_messages():
    for chat_id in await get_saved_main_chat_ids():
        try:
            await refresh_main_message(chat_id)
        except Exception:
            continue


def is_main_message(chat_id: int, message_id: int):
    if not chat_id or not message_id:
        return False
    return chat_main_message_id.get(chat_id) == message_id


def build_webapp_url():
    if not WEBAPP_URL:
        return None
    return f"{WEBAPP_URL}/cabinet"


async def set_webapp_menu_button(chat_id: int | None = None):
    webapp_url = build_webapp_url()
    if not webapp_url:
        return False

    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="👤",
                web_app=WebAppInfo(url=webapp_url)
            )
        )
        if chat_id:
            await bot.set_chat_menu_button(
                chat_id=chat_id,
                menu_button=MenuButtonWebApp(
                    text="👤",
                    web_app=WebAppInfo(url=webapp_url)
                )
            )
            webapp_menu_set_chats.add(chat_id)
        return True
    except Exception:
        return False


def validate_webapp_init_data(init_data: str):
    init_data = (init_data or "").strip()
    if not init_data:
        return None

    items = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = items.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={items[k]}" for k in sorted(items.keys()))
    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode("utf-8"),
        hashlib.sha256
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    user_raw = items.get("user")
    if not user_raw:
        return None

    try:
        user_obj = json.loads(user_raw)
        user_id = int(user_obj.get("id"))
    except Exception:
        return None

    return user_id


async def get_cabinet_dashboard(user_id: int, period_days: int, date_from=None, date_to=None):
    settings = await get_user_settings(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT
                r.id AS card_id,
                r.created_at,
                r.seal_number,
                r.amount,
                r.work_done,
                r.part_cost,
                r.photo_file_id,
                0 AS sort_id
            FROM repairs r
            WHERE r.user_id = ?

            UNION ALL

            SELECT
                h.parent_repair_id AS card_id,
                h.created_at,
                h.seal_number,
                h.amount,
                h.work_done,
                h.part_cost,
                h.photo_file_id,
                h.id AS sort_id
            FROM repair_history h
            JOIN repairs r ON r.id = h.parent_repair_id
            WHERE r.user_id = ?

            ORDER BY card_id DESC, sort_id ASC
        """, (user_id, user_id))
        rows = await cursor.fetchall()

    cards_map = {}
    gross = 0.0
    details_total = 0.0
    transactions_count = 0
    ledger_items = []

    for row in rows:
        card_id, created_at, seal_number, amount, work_done, part_cost, photo_file_id, sort_id = row
        if card_id not in cards_map:
            cards_map[card_id] = {
                "card_id": card_id,
                "latest_created_at": created_at,
                "latest_seal_number": display_seal(seal_number),
                "latest_has_photo": bool(photo_file_id),
                "latest_photo_ref": photo_file_id if photo_file_id else None,
                "all_seals": [],
                "stages": [],
            }

        stage = {
            "created_at": created_at,
            "seal_number": display_seal(seal_number),
            "amount": amount,
            "work_done": work_done,
            "part_cost": part_cost,
            "has_photo": bool(photo_file_id),
            "stage_type": "main" if sort_id == 0 else "history"
        }
        cards_map[card_id]["stages"].append(stage)
        cards_map[card_id]["latest_created_at"] = created_at
        cards_map[card_id]["latest_seal_number"] = display_seal(seal_number)
        cards_map[card_id]["latest_has_photo"] = bool(photo_file_id)
        cards_map[card_id]["latest_photo_ref"] = photo_file_id if photo_file_id else None
        seal_display = display_seal(seal_number)
        if seal_display not in cards_map[card_id]["all_seals"]:
            cards_map[card_id]["all_seals"].append(seal_display)

        if matches_cabinet_filter(created_at, period_days, date_from, date_to):
            amount_num = parse_money(amount)
            part_num = parse_money(part_cost)
            if has_explicit_amount(amount):
                gross += amount_num
                details_total += part_num
                transactions_count += 1
            if has_explicit_amount(amount) or part_num > 0:
                ledger_items.append({
                    "created_at": created_at,
                    "work_done": (work_done or "-").strip() or "-",
                    "plus_amount": format_money(amount_num) if has_explicit_amount(amount) else "0",
                    "minus_part": format_money(part_num),
                    "net": format_money(amount_num - part_num),
                    "has_amount": has_explicit_amount(amount)
                })

    cards = list(cards_map.values())
    for card in cards:
        latest = card.get("latest_seal_number") or "—"
        rest = [s for s in card.get("all_seals", []) if s != latest]
        card["all_seals_view"] = [latest] + rest
    cards.sort(key=lambda x: x["card_id"], reverse=True)
    ledger_items.sort(key=lambda x: (parse_card_date(x["created_at"]) or datetime.min), reverse=True)

    net_without_parts = gross - details_total
    expense_percent = normalize_expense_percent(settings.get("expense_percent"))
    expense_percent_cost = net_without_parts * (expense_percent / 100)
    net_after_percent = net_without_parts - expense_percent_cost

    summary = {
        "gross_revenue": format_money(gross),
        "parts_cost": format_money(details_total),
        "net_without_parts": format_money(net_without_parts),
        "expense_percent": expense_percent,
        "expense_percent_cost": format_money(expense_percent_cost),
        "net_after_percent": format_money(net_after_percent),
        "transactions_count": transactions_count
    }
    return cards, summary, ledger_items, settings


WEBAPP_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Личный кабинет</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: var(--tg-theme-secondary-bg-color, #F2F2F7);
      --card: var(--tg-theme-bg-color, #FFFFFF);
      --text: var(--tg-theme-text-color, #1C1C1E);
      --muted: var(--tg-theme-hint-color, #8E8E93);
      --line: #C6C6C8;
      --accent: var(--tg-theme-button-color, #007AFF);
      --danger: #FF3B30;
      --danger-soft: rgba(255,59,48,.1);
      --shadow: 0 10px 30px rgba(28,28,30,.08);
      --shadow-soft: 0 2px 8px rgba(0,0,0,.12);
      --chip-bg: rgba(118,118,128,.12);
      --placeholder-grad: linear-gradient(135deg, #E5E5EA 0%, #D1D1D6 100%);
    }
    body.dark-theme {
      --bg: var(--tg-theme-secondary-bg-color, #000000);
      --card: var(--tg-theme-bg-color, #1C1C1E);
      --text: var(--tg-theme-text-color, #FFFFFF);
      --muted: var(--tg-theme-hint-color, #8E8E93);
      --line: #38383A;
      --accent: var(--tg-theme-button-color, #0A84FF);
      --danger: #FF453A;
      --danger-soft: rgba(255,69,58,.12);
      --shadow: 0 16px 32px rgba(0,0,0,.28);
      --shadow-soft: 0 2px 8px rgba(0,0,0,.2);
      --chip-bg: rgba(118,118,128,.24);
      --placeholder-grad: linear-gradient(135deg, #3A3A3C 0%, #2C2C2E 100%);
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }
    button,
    summary,
    .stat.clickable,
    .toggle-row,
    .chip-btn,
    .m-btn,
    .tab-btn,
    .sw-btn {
      transition: opacity .1s ease, transform .16s ease, box-shadow .18s ease, border-color .18s ease, background-color .18s ease;
    }
    button:active,
    summary:active,
    .stat.clickable:active,
    .toggle-row:active,
    .chip-btn:active,
    .m-btn:active,
    .tab-btn:active,
    .sw-btn:active {
      opacity: .7;
    }
    .wrap { max-width: 980px; margin: 0 auto; padding: 16px 16px 102px; }
    .page-view {
      display: grid;
      gap: 12px;
      opacity: 1;
      transform: translateY(0);
      animation: fadeSlideIn .22s ease;
    }
    .page-view.hidden { display: none; }
    @keyframes fadeSlideIn {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .head {
      background: var(--card);
      border-radius: 20px;
      padding: 18px 16px 16px;
      box-shadow: var(--shadow);
    }
    h1 { font-size: 22px; margin: 0 0 6px; letter-spacing: -0.02em; }
    .meta { color: var(--muted); font-size: 13px; }
    .tab-btn {
      border: 1px solid var(--line);
      background: var(--card);
      border-radius: 16px;
      padding: 10px 10px 9px;
      font-size: 14px;
      min-height: 58px;
      min-width: 0;
      display: grid;
      gap: 4px;
      justify-items: center;
      color: var(--muted);
    }
    .tab-btn .ico {
      min-height: 20px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .tab-btn .lbl { font-size: 12px; line-height: 1.1; }
    .tab-btn.active {
      background: var(--card);
      color: var(--accent);
      border-color: var(--accent);
      box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent), var(--shadow-soft);
    }
    .tabs-panel {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      background: var(--bg);
      border-top: 1px solid var(--line);
      padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
      z-index: 20;
    }
    .tabs-shell { max-width: 980px; margin: 0 auto; }
    .tabs {
      display: grid;
      grid-template-columns: repeat(3, minmax(0,1fr));
      gap: 8px;
      background: color-mix(in srgb, var(--card) 84%, var(--bg));
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 8px;
      box-shadow: var(--shadow);
    }
    .switch {
      margin-top: 14px;
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      padding: 4px;
      background: var(--chip-bg);
      border-radius: 16px;
    }
    .sw-btn {
      border: 0;
      background: transparent;
      color: var(--text);
      border-radius: 12px;
      padding: 10px 14px;
      font-size: 13px;
      font-weight: 600;
      flex: 1 1 auto;
    }
    .sw-btn.active {
      background: var(--card);
      color: var(--text);
      box-shadow: var(--shadow-soft);
    }
    .cal-btn {
      width: 40px;
      padding: 10px 0;
      line-height: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .icon-svg {
      width: 18px;
      height: 18px;
      display: block;
      stroke: currentColor;
      fill: none;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
      flex: 0 0 auto;
    }
    .stats { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; margin-bottom: 4px; }
    .stat {
      background: var(--card);
      border-radius: 18px;
      border: 1px solid color-mix(in srgb, var(--line) 72%, transparent);
      padding: 14px;
      box-shadow: var(--shadow);
    }
    .stat.hero {
      grid-column: 1 / -1;
      border-left: 4px solid var(--accent);
    }
    .stat .k { color: var(--muted); font-size: 12px; letter-spacing: -0.01em; }
    .stat .v { margin-top: 6px; font-size: 24px; font-weight: 700; line-height: 1.05; }
    .stat.clickable { cursor: pointer; }
    .stat.clickable:active { transform: scale(0.99); }
    .stat.clickable.active { border-color: var(--accent); box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent), var(--shadow); }
    .cards-toolbar { margin-bottom: 4px; padding: 0 0 4px; }
    .cards-toolbar .search-input { margin-bottom: 0; }
    .search-input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--card);
      color: var(--text);
      padding: 13px 14px;
      font-size: 15px;
      outline: none;
      box-shadow: var(--shadow);
    }
    .search-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent), var(--shadow);
    }
    .grid { display: grid; gap: 12px; }
    .cards-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; padding: 4px 0 0; }
    .hidden { display: none; }
    details.item {
      background: var(--card);
      border: 1px solid color-mix(in srgb, var(--line) 72%, transparent);
      border-radius: 16px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    summary.top {
      display: grid;
      gap: 0;
      cursor: pointer;
      list-style: none;
      position: relative;
    }
    summary.top::-webkit-details-marker { display: none; }
    .summary-left {
      display: block;
      min-width: 0;
    }
    .summary-right {
      position: absolute;
      top: 8px;
      right: 8px;
      display: flex;
      gap: 6px;
      z-index: 2;
    }
    .edit-btn,
    .delete-btn {
      border: 0;
      background: rgba(0,0,0,.38);
      color: #fff;
      border-radius: 10px;
      width: 30px;
      height: 30px;
      font-size: 15px;
      line-height: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
    }
    .delete-btn { background: rgba(255,59,48,.78); color: #fff; }
    .edit-btn:active, .delete-btn:active { transform: scale(.98); }
    .edit-btn .icon-svg,
    .delete-btn .icon-svg,
    .cal-btn .icon-svg {
      width: 16px;
      height: 16px;
    }
    .repair-media {
      position: relative;
      aspect-ratio: 1 / 1;
      background: var(--placeholder-grad);
      overflow: hidden;
    }
    .thumb {
      width: 100%;
      height: 100%;
      background: transparent;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
      font-size: 32px;
      color: rgba(255,255,255,.82);
    }
    .thumb img,
    .repair-photo {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .repair-badge {
      position: absolute;
      top: 8px;
      left: 8px;
      display: inline-flex;
      align-items: center;
      max-width: calc(100% - 88px);
      padding: 5px 8px;
      border-radius: 999px;
      background: rgba(0,0,0,.5);
      color: #fff;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .02em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      z-index: 2;
    }
    .repair-body { padding: 12px; display: grid; gap: 6px; }
    .repair-title {
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.02em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .repair-sub { color: var(--muted); font-size: 12px; line-height: 1.35; }
    .repair-metrics { display: grid; gap: 4px; }
    .repair-line {
      font-size: 13px;
      line-height: 1.35;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .stage-list {
      margin: 0 12px 12px;
      border-top: 1px dashed color-mix(in srgb, var(--line) 80%, transparent);
      padding-top: 10px;
      display: grid;
      gap: 8px;
    }
    .stage {
      border-radius: 14px;
      background: color-mix(in srgb, var(--card) 55%, var(--bg));
      border: 1px solid color-mix(in srgb, var(--line) 72%, transparent);
      padding: 10px;
    }
    .seal { font-size: 18px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .seal-extra { color: var(--muted); font-size: 12px; line-height: 1.25; margin-top: 2px; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .date { color: var(--muted); font-size: 13px; }
    .row { margin: 4px 0; white-space: pre-wrap; word-break: break-word; }
    .ledger-item {
      background: var(--card);
      border: 1px solid color-mix(in srgb, var(--line) 72%, transparent);
      border-radius: 16px;
      padding: 12px;
      box-shadow: var(--shadow-soft);
    }
    .ledger-top { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
    .plus { color: #065f46; font-weight: 700; }
    .minus { color: var(--danger); font-weight: 700; }
    .empty { color: var(--muted); text-align: center; padding: 24px; }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(15,23,42,.26);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 40;
      padding: 16px;
      pointer-events: none;
    }
    .modal-backdrop.show { display: flex; }
    .modal-backdrop.show { pointer-events: auto; }
    .modal {
      width: 100%;
      max-width: 460px;
      background: var(--card);
      border: 1px solid color-mix(in srgb, var(--line) 72%, transparent);
      border-radius: 20px;
      box-shadow: 0 20px 45px rgba(15,23,42,.22);
      padding: 16px;
    }
    .modal h3 { margin: 0 0 10px; font-size: 18px; }
    .form-grid { display: grid; gap: 10px; }
    .field { display: grid; gap: 6px; }
    .field label { color: var(--muted); font-size: 12px; }
    .field input, .field textarea {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      font-size: 14px;
      font-family: inherit;
      background: var(--card);
      color: var(--text);
      resize: vertical;
    }
    .modal-actions { margin-top: 12px; display: flex; gap: 8px; justify-content: flex-end; }
    .m-btn {
      border: 1px solid var(--line);
      background: var(--card);
      color: var(--text);
      border-radius: 14px;
      padding: 10px 14px;
      font-size: 14px;
      cursor: pointer;
    }
    .m-btn.primary { border-color: var(--accent); color: var(--accent); }
    .settings-card {
      background: var(--card);
      border-radius: 20px;
      border: 1px solid color-mix(in srgb, var(--line) 72%, transparent);
      padding: 12px 16px;
      box-shadow: var(--shadow);
    }
    .settings-title { font-size: 15px; font-weight: 700; margin: 2px 0 10px; }
    .settings-desc { color: var(--muted); font-size: 13px; margin-top: -4px; margin-bottom: 10px; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; }
    .chip-btn {
      border: 1px solid transparent;
      background: var(--chip-bg);
      color: var(--text);
      border-radius: 999px;
      padding: 9px 13px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
    }
    .chip-btn.active {
      background: var(--card);
      color: var(--accent);
      border-color: color-mix(in srgb, var(--accent) 24%, transparent);
      box-shadow: var(--shadow-soft);
    }
    .chip-btn:disabled {
      opacity: .45;
      cursor: default;
    }
    .toggle-list { display: grid; gap: 8px; }
    .toggle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 44px;
      padding: 12px 0;
      border-top: 0.5px solid color-mix(in srgb, var(--line) 85%, transparent);
    }
    .toggle-row:first-child { border-top: 0; padding-top: 0; }
    .toggle-copy { display: grid; gap: 2px; }
    .toggle-copy .t { font-size: 14px; }
    .toggle-copy .d { color: var(--muted); font-size: 12px; }
    .switch-toggle {
      position: relative;
      width: 46px;
      height: 28px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: color-mix(in srgb, var(--muted) 28%, transparent);
      cursor: pointer;
      transition: .15s ease;
      flex: 0 0 auto;
    }
    .switch-toggle::after {
      content: "";
      position: absolute;
      top: 3px;
      left: 3px;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 1px 3px rgba(0,0,0,.18);
      transition: .15s ease;
    }
    .switch-toggle.active {
      background: color-mix(in srgb, var(--accent) 24%, transparent);
      border-color: var(--accent);
    }
    .switch-toggle.active::after {
      left: 21px;
      background: var(--accent);
    }
    .export-row { display: flex; gap: 8px; flex-wrap: wrap; }
    @media (max-width: 720px) {
      .wrap { padding: 14px 14px 102px; }
      .stats { grid-template-columns: 1fr 1fr; }
      details.item,
      .ledger-item,
      .settings-card,
      .search-input,
      .head,
      .tabs-shell {
        box-shadow: none;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div id="ledgerPage" class="page-view">
      <div class="head">
        <h1>Личный кабинет</h1>
        <div id="meta" class="meta">Загрузка...</div>
        <div class="switch">
          <button class="sw-btn active" data-period="7">1 неделя</button>
          <button class="sw-btn" data-period="30">1 месяц</button>
          <button class="sw-btn" data-period="90">3 месяца</button>
          <button id="openDateRange" class="sw-btn cal-btn" type="button" title="Выбрать даты" aria-label="Выбрать даты">
            <svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="3" y="5" width="18" height="16" rx="3"></rect>
              <path d="M16 3v4M8 3v4M3 10h18"></path>
            </svg>
          </button>
        </div>
      </div>
      <div id="stats" class="stats"></div>
      <div id="ledgerList" class="grid"></div>
    </div>
    <div id="cardsPage" class="page-view hidden">
      <div class="cards-toolbar">
        <input id="cardsSearch" class="search-input" type="text" placeholder="Поиск по карточкам" />
      </div>
      <div id="cardsList" class="grid cards-grid"></div>
    </div>
    <div id="settingsView" class="grid hidden">
      <div class="settings-card">
        <div class="settings-title">Процент расходов</div>
        <div class="settings-desc">Выбери, сколько процентов вычитать из чистой выручки.</div>
        <div id="expensePercentButtons" class="chips"></div>
      </div>
      <div class="settings-card">
        <div class="settings-title">Автоочистка чата</div>
        <div class="settings-desc">Очищать чат бота автоматически через выбранное время бездействия.</div>
        <div class="toggle-row">
          <div class="toggle-copy">
            <div class="t">Автоочистка включена</div>
            <div class="d">Если выключить, сообщения бота и история останутся в чате.</div>
          </div>
          <button id="chatCleanupToggle" class="switch-toggle" type="button" aria-label="Автоочистка чата"></button>
        </div>
        <div id="chatCleanupMinuteButtons" class="chips" style="margin-top:10px;"></div>
        <div class="export-row" style="margin-top:10px;">
          <button id="clearChatNow" class="m-btn" type="button">Очистить чат</button>
        </div>
      </div>
      <div class="settings-card">
        <div class="settings-title">Что показывать в карточке</div>
        <div id="cardFieldToggles" class="toggle-list"></div>
      </div>
      <div class="settings-card">
        <div class="settings-title">Экспорт базы</div>
        <div class="settings-desc">Скачать все твои записи в удобном формате.</div>
        <div class="export-row">
          <button id="exportJson" class="m-btn primary" type="button">JSON</button>
          <button id="exportCsv" class="m-btn" type="button">CSV</button>
        </div>
      </div>
    </div>
  </div>
  <div class="tabs-panel">
    <div class="tabs-shell">
      <div class="tabs">
        <button class="tab-btn active" data-tab="ledger">
          <span class="ico">
            <svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 3v18"></path>
              <path d="M16.5 7.5c0-1.9-2-3-4.5-3s-4.5 1.1-4.5 3 2 3 4.5 3 4.5 1.1 4.5 3-2 3-4.5 3-4.5-1.1-4.5-3"></path>
            </svg>
          </span>
          <span class="lbl">Учет</span>
        </button>
        <button class="tab-btn" data-tab="cards">
          <span class="ico">
            <svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.2-3.2a6 6 0 0 1-7.6 7.6l-6.9 6.9a2 2 0 1 1-2.8-2.8l6.9-6.9a6 6 0 0 1 7.6-7.6Z"></path>
            </svg>
          </span>
          <span class="lbl">Ремонт</span>
        </button>
        <button class="tab-btn" data-tab="settings">
          <span class="ico">
            <svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12.22 2h-.44a2 2 0 0 0-2 1.75l-.14 1.1c-.5.14-.98.34-1.43.6l-.88-.55a2 2 0 0 0-2.62.29l-.31.31a2 2 0 0 0-.29 2.62l.55.88c-.26.45-.46.93-.6 1.43l-1.1.14A2 2 0 0 0 2 11.78v.44a2 2 0 0 0 1.75 2l1.1.14c.14.5.34.98.6 1.43l-.55.88a2 2 0 0 0 .29 2.62l.31.31a2 2 0 0 0 2.62.29l.88-.55c.45.26.93.46 1.43.6l.14 1.1a2 2 0 0 0 2 1.75h.44a2 2 0 0 0 2-1.75l.14-1.1c.5-.14.98-.34 1.43-.6l.88.55a2 2 0 0 0 2.62-.29l.31-.31a2 2 0 0 0 .29-2.62l-.55-.88c.26-.45.46-.93.6-1.43l1.1-.14a2 2 0 0 0 1.75-2v-.44a2 2 0 0 0-1.75-2l-1.1-.14a7.85 7.85 0 0 0-.6-1.43l.55-.88a2 2 0 0 0-.29-2.62l-.31-.31a2 2 0 0 0-2.62-.29l-.88.55c-.45-.26-.93-.46-1.43-.6l-.14-1.1A2 2 0 0 0 12.22 2Z"></path>
              <circle cx="12" cy="12" r="3"></circle>
            </svg>
          </span>
          <span class="lbl">Настройки</span>
        </button>
      </div>
    </div>
  </div>
  <div id="editModalBackdrop" class="modal-backdrop" hidden>
    <div class="modal">
      <h3>Редактирование карточки</h3>
      <div class="form-grid">
        <div class="field">
          <label for="editSeal">Пломба</label>
          <input id="editSeal" type="text" />
        </div>
        <div class="field">
          <label for="editAmount">Сумма</label>
          <input id="editAmount" type="text" />
        </div>
        <div class="field">
          <label for="editWork">Ремонт</label>
          <textarea id="editWork" rows="2"></textarea>
        </div>
        <div class="field">
          <label for="editPart">Сумма детали</label>
          <input id="editPart" type="text" />
        </div>
      </div>
      <div class="modal-actions">
        <button id="editCancel" class="m-btn" type="button">Отмена</button>
        <button id="editSave" class="m-btn primary" type="button">Сохранить</button>
      </div>
    </div>
  </div>
  <div id="rangeModalBackdrop" class="modal-backdrop" hidden>
    <div class="modal">
      <h3>Выбери период</h3>
      <div class="form-grid">
        <div class="field">
          <label for="rangeFrom">С даты</label>
          <input id="rangeFrom" type="date" />
        </div>
        <div class="field">
          <label for="rangeTo">По дату</label>
          <input id="rangeTo" type="date" />
        </div>
      </div>
      <div class="modal-actions">
        <button id="rangeReset" class="m-btn" type="button">Сбросить</button>
        <button id="rangeCancel" class="m-btn" type="button">Отмена</button>
        <button id="rangeApply" class="m-btn primary" type="button">Показать</button>
      </div>
    </div>
  </div>
  <script>
    let tg = null;
    let activeTab = "ledger";
    let ledgerFilter = "all";
    let currentData = null;
    let currentPeriodDays = 7;
    let editingCardId = null;
    let currentCardQuery = "";
    let currentDateFrom = "";
    let currentDateTo = "";
    let editSaving = false;
    let bootstrapped = false;
    let currentSettings = {
      expense_percent: 40,
      show_seal: true,
      show_amount: true,
      show_work: true,
      show_part_cost: true,
      show_history: true,
      chat_cleanup_enabled: true,
      chat_cleanup_minutes: 3
    };

    const cardFieldConfig = [
      { key: "show_seal", title: "Пломба", desc: "Показывать номер пломбы в карточке" },
      { key: "show_amount", title: "Сумма", desc: "Показывать сумму ремонта" },
      { key: "show_work", title: "Ремонт", desc: "Показывать описание ремонта" },
      { key: "show_part_cost", title: "Сумма детали", desc: "Показывать расход на детали" },
      { key: "show_history", title: "История изменений", desc: "Показывать этапы и историю карточки" }
    ];

    function getTelegramWebApp() {
      return window.Telegram && window.Telegram.WebApp
        ? window.Telegram.WebApp
        : null;
    }

    function hapticImpact(style = "medium") {
      try {
        if (tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred) {
          tg.HapticFeedback.impactOccurred(style);
        }
      } catch (_) {}
    }

    function hapticNotify(type = "success") {
      try {
        if (tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred) {
          tg.HapticFeedback.notificationOccurred(type);
        }
      } catch (_) {}
    }

    function applyTheme() {
      const root = document.documentElement;
      const body = document.body;
      if (!root || !body) return;

      const params = (tg && tg.themeParams) || {};
      const bg = params.bg_color || params.secondary_bg_color || "#F2F2F7";
      const card = params.secondary_bg_color || params.bg_color || "#FFFFFF";
      const text = params.text_color || "#1C1C1E";
      const muted = params.hint_color || "#8E8E93";
      const accent = params.button_color || params.link_color || "#007AFF";
      const line = params.section_separator_color || params.section_header_text_color || "#C6C6C8";

      root.style.setProperty("--bg", bg);
      root.style.setProperty("--card", card);
      root.style.setProperty("--text", text);
      root.style.setProperty("--muted", muted);
      root.style.setProperty("--accent", accent);
      root.style.setProperty("--line", line);

      body.classList.toggle("dark-theme", (tg && tg.colorScheme) === "dark");
    }

    function showMetaError(text) {
      const meta = document.getElementById("meta");
      if (meta) meta.textContent = text;
    }

    function money(v) {
      return (v ?? "0") + " ₽";
    }

    function normalizeSearchText(value) {
      return String(value || "").toLowerCase().trim();
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function filterCards(cards) {
      const query = normalizeSearchText(currentCardQuery);
      if (!query) return cards;
      return cards.filter(card => {
        const chunks = [];
        chunks.push(card.latest_seal_number || "");
        (card.all_seals_view || []).forEach(seal => chunks.push(seal));
        (card.stages || []).forEach(stage => {
          chunks.push(stage.created_at || "");
          chunks.push(stage.seal_number || "");
          chunks.push(stage.amount || "");
          chunks.push(stage.part_cost || "");
          chunks.push(stage.work_done || "");
        });
        return normalizeSearchText(chunks.join(" ")).includes(query);
      });
    }

    function renderSummary(summary) {
      const stats = document.getElementById("stats");
      stats.innerHTML = `
        <div class="stat hero"><div class="k">Итог после -${summary.expense_percent}%</div><div class="v">${money(summary.net_after_percent)}</div></div>
        <div class="stat"><div class="k">Валовая выручка</div><div class="v">${money(summary.gross_revenue)}</div></div>
        <div class="stat"><div class="k">Расход на детали</div><div class="v">${money(summary.parts_cost)}</div></div>
        <div class="stat"><div class="k">Чистая без деталей</div><div class="v">${money(summary.net_without_parts)}</div></div>
        <div class="stat"><div class="k">-${summary.expense_percent}%</div><div class="v">${money(summary.expense_percent_cost)}</div></div>
        <div id="opsWithAmountCard" class="stat clickable"><div class="k">Операций с суммой</div><div class="v">${summary.transactions_count}</div></div>
      `;
      const opsCard = document.getElementById("opsWithAmountCard");
      if (opsCard) {
        opsCard.addEventListener("click", () => {
          activeTab = "ledger";
          ledgerFilter = "with_amount";
          applyTab();
          if (currentData) renderLedger(currentData.ledger || []);
        });
      }
    }

    function renderExpensePercentButtons() {
      const host = document.getElementById("expensePercentButtons");
      if (!host) return;
      const current = Number(currentSettings.expense_percent || 40);
      host.innerHTML = [30, 35, 40, 45, 50].map(percent => `
        <button class="chip-btn ${current === percent ? "active" : ""}" data-expense-percent="${percent}" type="button">${percent}%</button>
      `).join("");
      host.querySelectorAll("[data-expense-percent]").forEach(btn => {
        btn.addEventListener("click", () => {
          const percent = Number(btn.dataset.expensePercent);
          saveSettings({ expense_percent: percent });
        });
      });
    }

    function renderChatCleanupControls() {
      const toggle = document.getElementById("chatCleanupToggle");
      const host = document.getElementById("chatCleanupMinuteButtons");
      if (!toggle || !host) return;

      const enabled = Boolean(currentSettings.chat_cleanup_enabled);
      const currentMinutes = Number(currentSettings.chat_cleanup_minutes || 3);

      toggle.classList.toggle("active", enabled);
      host.innerHTML = [1, 3, 5, 10].map(minutes => `
        <button class="chip-btn ${enabled && currentMinutes === minutes ? "active" : ""}" data-cleanup-minutes="${minutes}" type="button"${enabled ? "" : " disabled"}>${minutes} мин</button>
      `).join("");

      toggle.onclick = () => {
        saveSettings({ chat_cleanup_enabled: !enabled });
      };

      host.querySelectorAll("[data-cleanup-minutes]").forEach(btn => {
        btn.addEventListener("click", () => {
          const minutes = Number(btn.dataset.cleanupMinutes);
          saveSettings({ chat_cleanup_minutes: minutes, chat_cleanup_enabled: true });
        });
      });
    }

    function renderCardFieldToggles() {
      const host = document.getElementById("cardFieldToggles");
      if (!host) return;
      host.innerHTML = cardFieldConfig.map(item => `
        <div class="toggle-row">
          <div class="toggle-copy">
            <div class="t">${item.title}</div>
            <div class="d">${item.desc}</div>
          </div>
          <button class="switch-toggle ${currentSettings[item.key] ? "active" : ""}" data-setting-key="${item.key}" type="button" aria-label="${item.title}"></button>
        </div>
      `).join("");
      host.querySelectorAll("[data-setting-key]").forEach(btn => {
        btn.addEventListener("click", () => {
          const key = btn.dataset.settingKey;
          saveSettings({ [key]: !Boolean(currentSettings[key]) });
        });
      });
    }

    function renderSettings() {
      renderExpensePercentButtons();
      renderChatCleanupControls();
      renderCardFieldToggles();
    }

    async function loadThumb(photoRef, imgId) {
      try {
        const resp = await fetch("/api/cabinet/photo", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ initData: (tg && tg.initData) || "", photoRef })
        });
        if (!resp.ok) return;
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const img = document.getElementById(imgId);
        if (img) img.src = url;
      } catch (_) {}
    }

    function renderCards(cards) {
      const list = document.getElementById("cardsList");
      if (!cards.length) {
        list.innerHTML = currentCardQuery
          ? '<div class="empty">По этому запросу ничего не найдено</div>'
          : '<div class="empty">Пока нет записей</div>';
        return;
      }

      const showSeal = Boolean(currentSettings.show_seal);
      const showAmount = Boolean(currentSettings.show_amount);
      const showWork = Boolean(currentSettings.show_work);
      const showPartCost = Boolean(currentSettings.show_part_cost);
      const showHistory = Boolean(currentSettings.show_history);

      list.innerHTML = cards.map(card => {
        const latestStage = (card.stages && card.stages.length) ? card.stages[card.stages.length - 1] : {};
        const title = showSeal
          ? (card.latest_seal_number || "Без пломбы")
          : `Карточка #${card.card_id}`;
        const related = (card.all_seals_view && card.all_seals_view.length > 1)
          ? (showSeal ? `Связанные: ${card.all_seals_view.slice(1).join(", ")}` : `Связанные записи: ${card.all_seals_view.length}`)
          : "";
        const amountLine = showAmount ? `<div class="repair-line">Сумма: ${escapeHtml(latestStage.amount || "—")}</div>` : "";
        const partLine = showPartCost ? `<div class="repair-line">Деталь: ${escapeHtml(latestStage.part_cost || "—")}</div>` : "";
        const workLine = showWork ? `<div class="repair-line">${escapeHtml(latestStage.work_done || "Без описания ремонта")}</div>` : "";
        return `
        <details class="item">
          <summary class="top">
            <div class="summary-left repair-media">
              ${showSeal ? `<div class="repair-badge">${escapeHtml(card.latest_seal_number || "Без пломбы")}</div>` : ""}
              <div class="summary-right">
                <button class="edit-btn" data-card-id="${card.card_id}" title="Редактировать" aria-label="Редактировать">
                  <svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M3 21h4l11-11a2.12 2.12 0 1 0-3-3L4 18v3Z"></path>
                    <path d="m14.5 6.5 3 3"></path>
                  </svg>
                </button>
                <button class="delete-btn" data-card-id="${card.card_id}" title="Удалить" aria-label="Удалить">
                  <svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M3 6h18"></path>
                    <path d="M8 6V4h8v2"></path>
                    <path d="M19 6l-1 14H6L5 6"></path>
                    <path d="M10 11v6M14 11v6"></path>
                  </svg>
                </button>
              </div>
              <div class="thumb">
                ${card.latest_has_photo ? `<img id="img-${card.card_id}" class="repair-photo" alt="photo" />` : "?"}
              </div>
            </div>
            <div class="repair-body">
              <div class="repair-title">${escapeHtml(title)}</div>
              <div class="date">${escapeHtml(card.latest_created_at || "—")}</div>
              ${related ? `<div class="seal-extra">${escapeHtml(related)}</div>` : ""}
              <div class="repair-metrics">
                ${amountLine}
                ${partLine}
                ${workLine}
              </div>
            </div>
          </summary>
          <div class="stage-list ${showHistory ? "" : "hidden"}">
            ${card.stages.map(stage => `
              <div class="stage">
                <div class="row"><b>${stage.stage_type === "main" ? "Основная запись" : "Этап ремонта"}</b> · ${escapeHtml(stage.created_at || "—")}</div>
                ${showSeal ? `<div class="row">Пломба: ${escapeHtml(stage.seal_number || "—")}</div>` : ""}
                ${showAmount ? `<div class="row">Сумма: ${escapeHtml(stage.amount || "—")}</div>` : ""}
                ${showPartCost ? `<div class="row">Деталь: ${escapeHtml(stage.part_cost || "—")}</div>` : ""}
                ${showWork ? `<div class="row">Тип ремонта: ${escapeHtml(stage.work_done || "—")}</div>` : ""}
              </div>
            `).join("")}
          </div>
          ${!showHistory ? `
            <div class="stage-list">
              <div class="stage">
                ${showSeal ? `<div class="row">Пломба: ${escapeHtml(card.latest_seal_number || "—")}</div>` : ""}
                ${showAmount ? `<div class="row">Сумма: ${escapeHtml(latestStage.amount || "—")}</div>` : ""}
                ${showPartCost ? `<div class="row">Деталь: ${escapeHtml(latestStage.part_cost || "—")}</div>` : ""}
                ${showWork ? `<div class="row">Тип ремонта: ${escapeHtml(latestStage.work_done || "—")}</div>` : ""}
              </div>
            </div>
          ` : ""}
        </details>
      `;
      }).join("");

      cards.forEach(card => {
        if (card.latest_has_photo && card.latest_photo_ref) {
          loadThumb(card.latest_photo_ref, `img-${card.card_id}`);
        }
      });

      list.querySelectorAll(".edit-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          openEditModal(Number(btn.dataset.cardId));
        });
      });
      list.querySelectorAll(".delete-btn").forEach(btn => {
        btn.addEventListener("click", async (e) => {
          e.preventDefault();
          e.stopPropagation();
          await confirmDeleteCard(Number(btn.dataset.cardId));
        });
      });
    }

    function renderLedger(items) {
      const list = document.getElementById("ledgerList");
      const filtered = ledgerFilter === "with_amount"
        ? items.filter(i => i.has_amount)
        : items;
      if (!filtered.length) {
        list.innerHTML = '<div class="empty">За выбранный период операций нет</div>';
        return;
      }
      list.innerHTML = filtered.map(item => `
        <div class="ledger-item">
          <div class="ledger-top">
            <div class="date">${item.created_at || "—"}</div>
            <div><span class="plus">+${money(item.plus_amount)}</span> <span class="minus">-${money(item.minus_part)}</span></div>
          </div>
          <div class="row">${item.work_done || "-"}</div>
          <div class="row">Чистая: ${money(item.net)}</div>
        </div>
      `).join("");
    }

    function applyTab() {
      const ledgerPage = document.getElementById("ledgerPage");
      const cardsPage = document.getElementById("cardsPage");
      const settings = document.getElementById("settingsView");
      [
        [ledgerPage, activeTab === "ledger"],
        [cardsPage, activeTab === "cards"],
        [settings, activeTab === "settings"]
      ].forEach(([node, show]) => {
        node.classList.toggle("hidden", !show);
        if (show) {
          node.style.animation = "none";
          void node.offsetWidth;
          node.style.animation = "";
        }
      });
      document.querySelectorAll(".tab-btn").forEach(btn => btn.classList.toggle("active", btn.dataset.tab === activeTab));
      const opsCard = document.getElementById("opsWithAmountCard");
      if (opsCard) opsCard.classList.toggle("active", activeTab === "ledger" && ledgerFilter === "with_amount");
    }

    function updatePresetButtons(activePeriod = null, customActive = false) {
      document.querySelectorAll(".sw-btn[data-period]").forEach(btn => {
        btn.classList.toggle("active", !customActive && Number(btn.dataset.period) === Number(activePeriod));
      });
      document.getElementById("openDateRange").classList.toggle("active", customActive);
    }

    async function loadData(periodDays, dateFrom = "", dateTo = "") {
      currentPeriodDays = periodDays;
      currentDateFrom = dateFrom;
      currentDateTo = dateTo;
      const resp = await fetch("/api/cabinet/repairs", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ initData: (tg && tg.initData) || "", periodDays, dateFrom, dateTo })
      });

      if (!resp.ok) {
        document.getElementById("meta").textContent = "Ошибка загрузки данных";
        return;
      }

      const data = await resp.json();
      currentData = data;
      currentSettings = Object.assign({}, currentSettings, data.settings || {});
      const meta = document.getElementById("meta");
      meta.textContent = "Карточек: " + data.count + " · Период: " + data.period_label;
      renderSummary(data.summary);
      renderSettings();
      renderCards(filterCards(data.cards || []));
      renderLedger(data.ledger || []);
      applyTab();
    }

    async function saveSettings(patch) {
      try {
        const resp = await fetch("/api/cabinet/settings", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            initData: (tg && tg.initData) || "",
            settings: Object.assign({}, currentSettings, patch)
          })
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          if (tg && tg.showAlert) tg.showAlert("Не удалось сохранить настройки");
          return;
        }
        currentSettings = Object.assign({}, currentSettings, data.settings || {});
        renderSettings();
        if (currentData) renderCards(filterCards(currentData.cards || []));
        hapticNotify("success");
        await loadData(currentPeriodDays, currentDateFrom, currentDateTo);
      } catch (_) {
        if (tg && tg.showAlert) tg.showAlert("Ошибка сохранения настроек");
      }
    }

    async function exportDatabase(format) {
      try {
        const resp = await fetch("/api/cabinet/export", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            initData: (tg && tg.initData) || "",
            format
          })
        });
        if (!resp.ok) {
          if (tg && tg.showAlert) tg.showAlert("Не удалось выгрузить базу");
          return;
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `repairs-export.${format}`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (_) {
        if (tg && tg.showAlert) tg.showAlert("Ошибка выгрузки");
      }
    }

    async function clearChatNow() {
      try {
        const resp = await fetch("/api/cabinet/clear-chat", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            initData: (tg && tg.initData) || ""
          })
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          if (tg && tg.showAlert) tg.showAlert("Не удалось очистить чат");
          return;
        }
        hapticNotify("success");
        if (tg && tg.showAlert) {
          tg.showAlert("Чат очищен");
        } else {
          alert("Чат очищен");
        }
      } catch (_) {
        if (tg && tg.showAlert) tg.showAlert("Ошибка очистки чата");
      }
    }

    function askConfirm(text) {
      return new Promise(resolve => {
        if (tg && tg.showConfirm) {
          try {
            tg.showConfirm(text, resolve);
            return;
          } catch (_) {}
        }
        resolve(window.confirm(text));
      });
    }

    async function confirmDeleteCard(cardId) {
      hapticNotify("warning");
      const ok = await askConfirm("Удалить эту карточку?");
      if (!ok) return;
      try {
        const resp = await fetch("/api/cabinet/card/delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            initData: (tg && tg.initData) || "",
            cardId
          })
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          if (tg && tg.showAlert) tg.showAlert("Не удалось удалить карточку");
          return;
        }
        hapticNotify("success");
        await loadData(currentPeriodDays, currentDateFrom, currentDateTo);
      } catch (_) {
        if (tg && tg.showAlert) tg.showAlert("Ошибка удаления");
      }
    }

    document.querySelectorAll(".tab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        hapticImpact("medium");
        activeTab = btn.dataset.tab;
        if (activeTab === "ledger") ledgerFilter = "all";
        applyTab();
        if (currentData) {
          renderCards(filterCards(currentData.cards || []));
          renderLedger(currentData.ledger || []);
        }
      });
    });

    document.querySelectorAll(".sw-btn").forEach(btn => {
      if (!btn.dataset.period) return;
      btn.addEventListener("click", () => {
        updatePresetButtons(Number(btn.dataset.period), false);
        loadData(Number(btn.dataset.period), "", "").catch(() => {
          document.getElementById("meta").textContent = "Ошибка загрузки данных";
        });
      });
    });

    function closeRangeModal() {
      const backdrop = document.getElementById("rangeModalBackdrop");
      backdrop.classList.remove("show");
      backdrop.style.display = "none";
      backdrop.hidden = true;
    }

    function openRangeModal() {
      const backdrop = document.getElementById("rangeModalBackdrop");
      document.getElementById("rangeFrom").value = currentDateFrom || "";
      document.getElementById("rangeTo").value = currentDateTo || "";
      backdrop.hidden = false;
      backdrop.style.display = "";
      backdrop.classList.add("show");
    }

    function closeEditModal() {
      const backdrop = document.getElementById("editModalBackdrop");
      editingCardId = null;
      editSaving = false;
      document.getElementById("editSave").disabled = false;
      document.getElementById("editSeal").value = "";
      document.getElementById("editAmount").value = "";
      document.getElementById("editWork").value = "";
      document.getElementById("editPart").value = "";
      backdrop.classList.remove("show");
      backdrop.style.display = "none";
      backdrop.hidden = true;
    }

    function openEditModal(cardId) {
      const cards = (currentData && currentData.cards) || [];
      const card = cards.find(c => Number(c.card_id) === Number(cardId));
      if (!card || !card.stages || !card.stages.length) return;
      const latest = card.stages[card.stages.length - 1];
      const backdrop = document.getElementById("editModalBackdrop");
      editingCardId = cardId;
      document.getElementById("editSeal").value = latest.seal_number || "";
      document.getElementById("editAmount").value = latest.amount || "";
      document.getElementById("editWork").value = latest.work_done || "";
      document.getElementById("editPart").value = latest.part_cost || "";
      backdrop.hidden = false;
      backdrop.style.display = "";
      backdrop.classList.add("show");
    }

    async function saveCardEdit() {
      if (!editingCardId || editSaving) return;
      const saveBtn = document.getElementById("editSave");
      const payload = {
        initData: (tg && tg.initData) || "",
        cardId: editingCardId,
        sealNumber: document.getElementById("editSeal").value.trim(),
        amount: document.getElementById("editAmount").value.trim(),
        workDone: document.getElementById("editWork").value.trim(),
        partCost: document.getElementById("editPart").value.trim()
      };
      editSaving = true;
      saveBtn.disabled = true;
      try {
        const resp = await fetch("/api/cabinet/card/update", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          const msg = data && data.error === "duplicate_seal"
            ? "Такая пломба уже есть в базе"
            : "Не удалось сохранить изменения";
          if (tg && tg.showAlert) tg.showAlert(msg); else alert(msg);
          return;
        }
        hapticNotify("success");
        closeEditModal();
        await new Promise(resolve => requestAnimationFrame(resolve));
        await loadData(currentPeriodDays, currentDateFrom, currentDateTo);
      } catch (_) {
        if (tg && tg.showAlert) tg.showAlert("Ошибка сохранения");
      } finally {
        editSaving = false;
        saveBtn.disabled = false;
      }
    }

    document.getElementById("editCancel").addEventListener("click", closeEditModal);
    document.getElementById("editSave").addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      saveCardEdit();
    });
    document.getElementById("editModalBackdrop").addEventListener("click", (e) => {
      if (e.target.id === "editModalBackdrop") closeEditModal();
    });
    document.getElementById("openDateRange").addEventListener("click", openRangeModal);
    document.getElementById("rangeCancel").addEventListener("click", closeRangeModal);
    document.getElementById("rangeReset").addEventListener("click", () => {
      closeRangeModal();
      updatePresetButtons(7, false);
      loadData(7, "", "").catch(() => {
        document.getElementById("meta").textContent = "Ошибка загрузки данных";
      });
    });
    document.getElementById("rangeApply").addEventListener("click", () => {
      const dateFrom = document.getElementById("rangeFrom").value || "";
      const dateTo = document.getElementById("rangeTo").value || "";
      if (!dateFrom && !dateTo) {
        if (tg && tg.showAlert) tg.showAlert("Выбери хотя бы одну дату");
        return;
      }
      closeRangeModal();
      updatePresetButtons(null, true);
      loadData(currentPeriodDays, dateFrom, dateTo).catch(() => {
        document.getElementById("meta").textContent = "Ошибка загрузки данных";
      });
    });
    document.getElementById("rangeModalBackdrop").addEventListener("click", (e) => {
      if (e.target.id === "rangeModalBackdrop") closeRangeModal();
    });
    document.getElementById("cardsSearch").addEventListener("input", (e) => {
      currentCardQuery = e.target.value || "";
      if (currentData) renderCards(filterCards(currentData.cards || []));
    });
    document.getElementById("exportJson").addEventListener("click", () => exportDatabase("json"));
    document.getElementById("exportCsv").addEventListener("click", () => exportDatabase("csv"));
    document.getElementById("clearChatNow").addEventListener("click", clearChatNow);

    async function bootCabinet(attempt = 0) {
      if (bootstrapped) return;

      tg = getTelegramWebApp();
      if (!tg) {
        if (attempt < 50) {
          setTimeout(() => bootCabinet(attempt + 1), 120);
          return;
        }
        showMetaError("Не удалось инициализировать мини-приложение");
        return;
      }

      try {
        tg.ready();
        tg.expand();
        applyTheme();
        if (tg.onEvent) {
          tg.onEvent("themeChanged", applyTheme);
        }
      } catch (_) {}

      bootstrapped = true;
      loadData(7).catch(() => {
        showMetaError("Ошибка загрузки данных");
      });
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => bootCabinet());
    } else {
      bootCabinet();
    }
  </script>
</body>
</html>
"""


async def cabinet_page(request: web.Request):
    return web.Response(text=WEBAPP_HTML, content_type="text/html")


async def cabinet_photo_api(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    user_id = validate_webapp_init_data((body or {}).get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    photo_ref = ((body or {}).get("photoRef") or "").strip()
    if not photo_ref:
        return web.json_response({"ok": False, "error": "photo_not_found"}, status=404)

    local_photo_path = resolve_local_photo_path(photo_ref)
    if local_photo_path:
        try:
            with open(local_photo_path, "rb") as f:
                data = f.read()
            ext = os.path.splitext(local_photo_path)[1].lower()
            content_type = "image/jpeg"
            if ext == ".png":
                content_type = "image/png"
            elif ext == ".webp":
                content_type = "image/webp"
            return web.Response(body=data, content_type=content_type)
        except Exception:
            return web.json_response({"ok": False, "error": "photo_read_error"}, status=500)

    # Telegram file_id
    if "/" in photo_ref or "\\" in photo_ref:
        return web.json_response({"ok": False, "error": "photo_not_found"}, status=404)

    try:
        tg_file = await bot.get_file(photo_ref)
        file_path = tg_file.file_path
        if not file_path:
            return web.json_response({"ok": False, "error": "photo_not_found"}, status=404)

        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        async with ClientSession() as session:
            async with session.get(file_url, timeout=20) as resp:
                if resp.status != 200:
                    return web.json_response({"ok": False, "error": "photo_not_found"}, status=404)
                payload = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                return web.Response(body=payload, content_type=content_type)
    except Exception:
        return web.json_response({"ok": False, "error": "photo_not_found"}, status=404)


async def cabinet_repairs_api(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    user_id = validate_webapp_init_data((body or {}).get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        period_days = int((body or {}).get("periodDays") or 7)
    except Exception:
        period_days = 7
    if period_days not in (7, 30, 90):
        period_days = 7

    date_from = parse_iso_date((body or {}).get("dateFrom"))
    date_to = parse_iso_date((body or {}).get("dateTo"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    cards, summary, ledger_items, settings = await get_cabinet_dashboard(user_id, period_days, date_from, date_to)
    if date_from or date_to:
        from_label = date_from.strftime("%d.%m.%Y") if date_from else "..."
        to_label = date_to.strftime("%d.%m.%Y") if date_to else "..."
        period_label = f"{from_label} - {to_label}"
    else:
        period_label = {7: "1 неделя", 30: "1 месяц", 90: "3 месяца"}[period_days]
    return web.json_response({
        "ok": True,
        "count": len(cards),
        "period_days": period_days,
        "period_label": period_label,
        "summary": summary,
        "cards": cards,
        "ledger": ledger_items,
        "settings": settings
    })


async def cabinet_settings_api(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    user_id = validate_webapp_init_data((body or {}).get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    settings = await save_user_settings(user_id, (body or {}).get("settings") or {})
    await apply_user_chat_cleanup_settings(user_id)
    return web.json_response({"ok": True, "settings": settings})


async def cabinet_export_api(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    user_id = validate_webapp_init_data((body or {}).get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    export_format = ((body or {}).get("format") or "json").strip().lower()
    if export_format not in {"json", "csv"}:
        export_format = "json"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT
                r.id AS card_id,
                r.created_at,
                r.user_id,
                r.photo_file_id,
                r.seal_number,
                r.work_done,
                r.amount,
                r.part_cost,
                'main' AS stage_type
            FROM repairs r
            WHERE r.user_id = ?

            UNION ALL

            SELECT
                h.parent_repair_id AS card_id,
                h.created_at,
                r.user_id,
                h.photo_file_id,
                h.seal_number,
                h.work_done,
                h.amount,
                h.part_cost,
                'history' AS stage_type
            FROM repair_history h
            JOIN repairs r ON r.id = h.parent_repair_id
            WHERE r.user_id = ?

            ORDER BY card_id DESC, created_at DESC
        """, (user_id, user_id))
        rows = [dict(row) for row in await cursor.fetchall()]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if export_format == "json":
        payload = json.dumps(rows, ensure_ascii=False, indent=2)
        return web.Response(
            text=payload,
            content_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="repairs-{stamp}.json"'}
        )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "card_id", "stage_type", "created_at", "user_id",
        "seal_number", "amount", "part_cost", "work_done", "photo_file_id"
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow({
            "card_id": row.get("card_id"),
            "stage_type": row.get("stage_type"),
            "created_at": row.get("created_at"),
            "user_id": row.get("user_id"),
            "seal_number": row.get("seal_number"),
            "amount": row.get("amount"),
            "part_cost": row.get("part_cost"),
            "work_done": row.get("work_done"),
            "photo_file_id": row.get("photo_file_id"),
        })
    return web.Response(
        text=output.getvalue(),
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="repairs-{stamp}.csv"'}
    )


async def cabinet_clear_chat_api(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    user_id = validate_webapp_init_data((body or {}).get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    target_chat_ids = [chat_id for chat_id, owner_id in chat_user_ids.items() if owner_id == user_id]
    if not target_chat_ids:
        return web.json_response({"ok": True, "cleared": 0})

    for chat_id in target_chat_ids:
        await purge_chat_history(chat_id)

    return web.json_response({"ok": True, "cleared": len(target_chat_ids)})


async def cabinet_update_card_api(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    user_id = validate_webapp_init_data((body or {}).get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        card_id = int((body or {}).get("cardId"))
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_card_id"}, status=400)

    seal_number = ((body or {}).get("sealNumber") or "").strip()
    amount = ((body or {}).get("amount") or "").strip() or "-"
    work_done = ((body or {}).get("workDone") or "").strip()
    part_cost = ((body or {}).get("partCost") or "").strip() or None

    if not seal_number or not work_done:
        return web.json_response({"ok": False, "error": "invalid_fields"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, seal_number
            FROM repairs
            WHERE id = ? AND user_id = ?
            LIMIT 1
        """, (card_id, user_id))
        main_row = await cur.fetchone()
        if not main_row:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)

        cur = await db.execute("""
            SELECT id, seal_number
            FROM repair_history
            WHERE parent_repair_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (card_id,))
        latest_history = await cur.fetchone()

        target_table = "repairs"
        target_id = card_id
        previous_seal = (main_row[1] or "").strip()
        if latest_history:
            target_table = "repair_history"
            target_id = int(latest_history[0])
            previous_seal = (latest_history[1] or "").strip()

        exclude_repairs_id = card_id if target_table == "repairs" else -1
        exclude_history_id = target_id if target_table == "repair_history" else -1

        cur = await db.execute("""
            SELECT 1
            FROM repairs
            WHERE user_id = ? AND trim(seal_number) = ? AND id != ?
            LIMIT 1
        """, (user_id, seal_number, exclude_repairs_id))
        if await cur.fetchone():
            return web.json_response({"ok": False, "error": "duplicate_seal"}, status=409)

        cur = await db.execute("""
            SELECT 1
            FROM repair_history h
            JOIN repairs r ON r.id = h.parent_repair_id
            WHERE r.user_id = ? AND trim(h.seal_number) = ? AND h.id != ?
            LIMIT 1
        """, (user_id, seal_number, exclude_history_id))
        if await cur.fetchone():
            return web.json_response({"ok": False, "error": "duplicate_seal"}, status=409)

        if target_table == "repair_history":
            await db.execute("""
                UPDATE repair_history
                SET seal_number = ?, amount = ?, work_done = ?, part_cost = ?
                WHERE id = ?
            """, (seal_number, amount, work_done, part_cost, target_id))
        else:
            await db.execute("""
                UPDATE repairs
                SET seal_number = ?, amount = ?, work_done = ?, part_cost = ?
                WHERE id = ?
            """, (seal_number, amount, work_done, part_cost, target_id))

        for seal_value in {previous_seal, seal_number}:
            clean = (seal_value or "").strip()
            if not clean:
                continue
            await db.execute("""
                INSERT INTO repair_seal_aliases (parent_repair_id, user_id, seal_number, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(parent_repair_id, seal_number) DO NOTHING
            """, (card_id, user_id, clean, today_str()))

        await db.commit()

    return web.json_response({"ok": True})


async def cabinet_delete_card_api(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    user_id = validate_webapp_init_data((body or {}).get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        card_id = int((body or {}).get("cardId"))
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_card_id"}, status=400)

    deleted = await delete_repair_card(user_id, card_id)
    if not deleted:
        return web.json_response({"ok": False, "error": "not_found"}, status=404)

    return web.json_response({"ok": True})


async def start_webapp_server():
    app = web.Application()
    app.router.add_get("/cabinet", cabinet_page)
    app.router.add_post("/api/cabinet/repairs", cabinet_repairs_api)
    app.router.add_post("/api/cabinet/photo", cabinet_photo_api)
    app.router.add_post("/api/cabinet/card/update", cabinet_update_card_api)
    app.router.add_post("/api/cabinet/card/delete", cabinet_delete_card_api)
    app.router.add_post("/api/cabinet/settings", cabinet_settings_api)
    app.router.add_post("/api/cabinet/export", cabinet_export_api)
    app.router.add_post("/api/cabinet/clear-chat", cabinet_clear_chat_api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
    await site.start()
    return runner


async def save_repair(
    user_id: int,
    photo_file_id: str | None,
    seal_number: str,
    work_done: str,
    amount: str,
    part_cost: str | None = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO repairs (
                created_at, user_id, photo_file_id, seal_number, work_done, amount, part_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            today_str(),
            user_id,
            photo_file_id,
            seal_number,
            work_done,
            amount,
            (part_cost or "").strip() or None
        ))
        await db.commit()
        return cursor.lastrowid


async def save_history(
    parent_repair_id: int,
    user_id: int,
    photo_file_id: str | None,
    seal_number: str,
    work_done: str,
    amount: str,
    part_cost: str | None = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO repair_history (
                parent_repair_id, created_at, user_id, photo_file_id, seal_number, work_done, amount, part_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            parent_repair_id,
            today_str(),
            user_id,
            photo_file_id,
            seal_number,
            work_done,
            amount,
            (part_cost or "").strip() or None
        ))
        await db.commit()


async def get_today_repairs(user_id: int):
    today = today_str()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, created_at, seal_number, work_done, amount, part_cost
            FROM repairs
            WHERE created_at = ? AND user_id = ?
            ORDER BY id DESC
        """, (today, user_id))
        return await cursor.fetchall()


async def get_repair_by_any_seal(user_id: int, seal_number: str):
    seal_number = (seal_number or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, created_at, photo_file_id, seal_number, work_done, amount, part_cost
            FROM repairs
            WHERE seal_number = ? AND user_id = ?
            LIMIT 1
        """, (seal_number, user_id))
        row = await cursor.fetchone()
        if row:
            return row

        cursor = await db.execute("""
            SELECT r.id, r.created_at, r.photo_file_id, r.seal_number, r.work_done, r.amount, r.part_cost
            FROM repair_seal_aliases a
            JOIN repairs r ON r.id = a.parent_repair_id
            WHERE a.seal_number = ? AND a.user_id = ? AND r.user_id = ?
            ORDER BY a.id DESC
            LIMIT 1
        """, (seal_number, user_id, user_id))
        row = await cursor.fetchone()
        if row:
            return row

        cursor = await db.execute("""
            SELECT r.id, r.created_at, r.photo_file_id, r.seal_number, r.work_done, r.amount, r.part_cost
            FROM repair_history h
            JOIN repairs r ON r.id = h.parent_repair_id
            WHERE h.seal_number = ? AND r.user_id = ?
            ORDER BY h.id DESC
            LIMIT 1
        """, (seal_number, user_id))
        return await cursor.fetchone()


async def seal_exists_anywhere(user_id: int, seal_number: str):
    seal_number = (seal_number or "").strip()
    if not seal_number:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT 1
            FROM repairs
            WHERE seal_number = ? AND user_id = ?
            LIMIT 1
        """, (seal_number, user_id))
        if await cursor.fetchone():
            return True

        cursor = await db.execute("""
            SELECT 1
            FROM repair_history h
            JOIN repairs r ON r.id = h.parent_repair_id
            WHERE h.seal_number = ? AND r.user_id = ?
            LIMIT 1
        """, (seal_number, user_id))
        if await cursor.fetchone():
            return True

        cursor = await db.execute("""
            SELECT 1
            FROM repair_seal_aliases a
            JOIN repairs r ON r.id = a.parent_repair_id
            WHERE a.seal_number = ? AND a.user_id = ? AND r.user_id = ?
            LIMIT 1
        """, (seal_number, user_id, user_id))
        if await cursor.fetchone():
            return True

    return False


async def get_repair_history(user_id: int, parent_repair_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT created_at, photo_file_id, seal_number, work_done, amount, part_cost, 'main' as record_type, 0 as sort_id
            FROM repairs
            WHERE id = ? AND user_id = ?

            UNION ALL

            SELECT h.created_at, h.photo_file_id, h.seal_number, h.work_done, h.amount, h.part_cost, 'history' as record_type, h.id as sort_id
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
        await db.execute("DELETE FROM repair_seal_aliases WHERE parent_repair_id = ?", (parent_repair_id,))
        await db.execute("DELETE FROM repairs WHERE id = ? AND user_id = ?", (parent_repair_id, user_id))
        await db.commit()
        return True


async def get_main_repair_seal(user_id: int, parent_repair_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT seal_number
            FROM repairs
            WHERE id = ? AND user_id = ?
            LIMIT 1
        """, (parent_repair_id, user_id))
        row = await cursor.fetchone()
        if not row:
            return None
        return row[0]


async def update_main_repair_seal(
    user_id: int,
    parent_repair_id: int,
    new_seal_number: str,
    new_amount: str,
    new_work_done: str,
    new_part_cost: str | None = None
):
    new_seal_number = (new_seal_number or "").strip()
    new_amount = (new_amount or "").strip()
    new_work_done = (new_work_done or "").strip()
    if not new_seal_number:
        return "invalid", None, None, None, None
    if not new_amount or not new_work_done:
        return "invalid", None, None, None, None

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT seal_number, created_at, photo_file_id, work_done, amount, part_cost
            FROM (
                SELECT r.seal_number, r.created_at, r.photo_file_id, r.work_done, r.amount, r.part_cost, 0 AS sort_id
                FROM repairs r
                WHERE r.id = ? AND r.user_id = ?

                UNION ALL

                SELECT h.seal_number, h.created_at, h.photo_file_id, h.work_done, h.amount, h.part_cost, h.id AS sort_id
                FROM repair_history h
                JOIN repairs r ON r.id = h.parent_repair_id
                WHERE h.parent_repair_id = ? AND r.user_id = ?
            )
            ORDER BY sort_id DESC
            LIMIT 1
        """, (parent_repair_id, user_id, parent_repair_id, user_id))
        row = await cursor.fetchone()
        if not row:
            return "not_found", None, None, None, None

        old_seal_number = (row[0] or "").strip()
        old_seal_created_at = row[1]
        current_photo_file_id = row[2]
        if old_seal_number == new_seal_number:
            return "same", old_seal_number, new_seal_number, old_seal_created_at, old_seal_created_at

        if await seal_exists_anywhere(user_id, new_seal_number):
            return "duplicate", None, None, None, None

        new_seal_created_at = today_str()

        await db.execute("""
            INSERT OR IGNORE INTO repair_seal_aliases (parent_repair_id, user_id, seal_number, created_at)
            VALUES (?, ?, ?, ?)
        """, (parent_repair_id, user_id, old_seal_number, old_seal_created_at))

        await db.execute("""
            INSERT OR IGNORE INTO repair_seal_aliases (parent_repair_id, user_id, seal_number, created_at)
            VALUES (?, ?, ?, ?)
        """, (parent_repair_id, user_id, new_seal_number, new_seal_created_at))

        await db.execute("""
            INSERT INTO repair_history (
                parent_repair_id, created_at, user_id, photo_file_id, seal_number, work_done, amount, part_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            parent_repair_id,
            new_seal_created_at,
            user_id,
            current_photo_file_id,
            new_seal_number,
            new_work_done,
            new_amount,
            (new_part_cost or "").strip() or None
        ))

        await db.commit()
        return "updated", old_seal_number, new_seal_number, old_seal_created_at, new_seal_created_at


def parse_repair_input(text: str):
    text = (text or "").strip()
    if not text:
        return None

    comma_parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(comma_parts) >= 3:
        seal_number = comma_parts[0]
        amount = comma_parts[1]
        if len(comma_parts) >= 4:
            work_done = ", ".join(comma_parts[2:-1]).strip()
            part_cost = comma_parts[-1]
        else:
            work_done = ", ".join(comma_parts[2:]).strip()
            part_cost = None
    else:
        line_parts = [p.strip() for p in text.splitlines() if p.strip()]
        if len(line_parts) < 3:
            return None
        seal_number = line_parts[0]
        amount = line_parts[1]
        if len(line_parts) >= 4:
            work_done = " ".join(line_parts[2:-1]).strip()
            part_cost = line_parts[-1]
        else:
            work_done = " ".join(line_parts[2:]).strip()
            part_cost = None

    if not seal_number or not amount or not work_done:
        return None

    return {
        "seal_number": seal_number,
        "amount": amount,
        "work_done": work_done,
        "part_cost": (part_cost or "").strip() or None
    }


def parse_new_repair_line(text: str):
    return parse_repair_input(text)


def parse_history_line(text: str):
    return parse_repair_input(text)


def parse_quick_line(text: str):
    text = (text or "").strip()
    if not text:
        return None

    comma_parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(comma_parts) >= 2:
        amount = comma_parts[0]
        if len(comma_parts) >= 3:
            work_done = ", ".join(comma_parts[1:-1]).strip()
            part_cost = comma_parts[-1]
        else:
            work_done = ", ".join(comma_parts[1:]).strip()
            part_cost = None
    else:
        line_parts = [p.strip() for p in text.splitlines() if p.strip()]
        if len(line_parts) < 2:
            return None
        amount = line_parts[0]
        if len(line_parts) >= 3:
            work_done = " ".join(line_parts[1:-1]).strip()
            part_cost = line_parts[-1]
        else:
            work_done = " ".join(line_parts[1:]).strip()
            part_cost = None

    if not amount or not work_done:
        return None

    return {
        "amount": amount,
        "work_done": work_done,
        "part_cost": (part_cost or "").strip() or None
    }


async def safe_delete_message(message: Message, allow_main_message: bool = False):
    if is_main_message(message.chat.id, message.message_id):
        if allow_main_message:
            try:
                await message.delete()
            except Exception:
                pass
        return
    try:
        await message.delete()
    except Exception:
        pass


async def safe_delete_by_id(bot: Bot, chat_id: int, message_id, allow_main_message: bool = False):
    if not message_id:
        return
    if is_main_message(chat_id, message_id):
        if allow_main_message:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def purge_chat_history(chat_id: int):
    await load_main_message_id(chat_id)
    last_id = chat_last_message_id.get(chat_id)
    if not last_id:
        return

    start_id = max(1, last_id - CHAT_SWEEP_BACK_MESSAGES)
    for msg_id in range(last_id, start_id - 1, -1):
        await safe_delete_by_id(bot, chat_id, msg_id)


async def delayed_chat_cleanup(chat_id: int, anchor_message_id: int, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    if chat_last_message_id.get(chat_id) != anchor_message_id:
        return
    await purge_chat_history(chat_id)


async def reschedule_chat_cleanup(chat_id: int, user_id: int | None = None):
    task = chat_cleanup_tasks.get(chat_id)
    if task and not task.done():
        task.cancel()

    if not user_id:
        return

    settings = await get_user_settings(user_id)
    if not settings.get("chat_cleanup_enabled", True):
        return

    delay_seconds = normalize_chat_cleanup_minutes(settings.get("chat_cleanup_minutes")) * 60
    anchor = chat_last_message_id.get(chat_id, 0)
    chat_cleanup_tasks[chat_id] = asyncio.create_task(
        delayed_chat_cleanup(chat_id, anchor, delay_seconds)
    )


async def apply_user_chat_cleanup_settings(user_id: int):
    for chat_id, owner_id in list(chat_user_ids.items()):
        if owner_id != user_id:
            continue
        await reschedule_chat_cleanup(chat_id, user_id)


async def delete_message_later(chat_id: int, message_id: int, delay_seconds: int = AUTO_DELETE_SECONDS):
    await asyncio.sleep(delay_seconds)
    await safe_delete_by_id(bot, chat_id, message_id)


def schedule_auto_delete(chat_id: int, message_id: int, delay_seconds: int = AUTO_DELETE_SECONDS):
    if not message_id:
        return
    asyncio.create_task(delete_message_later(chat_id, message_id, delay_seconds))


async def remember_flow_bot_message(state: FSMContext, message_id: int):
    data = await state.get_data()
    ids = data.get("flow_bot_message_ids", [])
    if message_id not in ids:
        ids.append(message_id)
    await state.update_data(flow_bot_message_ids=ids)


async def remember_flow_user_message(state: FSMContext, message_id: int):
    data = await state.get_data()
    ids = data.get("flow_user_message_ids", [])
    if message_id not in ids:
        ids.append(message_id)
    await state.update_data(flow_user_message_ids=ids)


async def reset_flow_with_cleanup(message: Message, state: FSMContext):
    data = await state.get_data()

    message_ids = set()
    for key, value in data.items():
        if key.endswith("_message_id") and isinstance(value, int):
            message_ids.add(value)

    for value in data.get("flow_bot_message_ids", []):
        if isinstance(value, int):
            message_ids.add(value)

    for value in data.get("flow_user_message_ids", []):
        if isinstance(value, int):
            message_ids.add(value)

    if message.message_id:
        message_ids.add(message.message_id)

    for msg_id in message_ids:
        await safe_delete_by_id(bot, message.chat.id, msg_id)

    await state.clear()
    restart_msg = await message.answer("Начни сначала", reply_markup=main_kb)
    await asyncio.sleep(60)
    await safe_delete_message(restart_msg)


class ActivityCleanupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        chat_id = None
        message_id = None
        user_id = None

        if isinstance(event, Message):
            chat_id = event.chat.id
            message_id = event.message_id
            if event.from_user:
                user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.message:
            chat_id = event.message.chat.id
            message_id = event.message.message_id
            if event.from_user:
                user_id = event.from_user.id

        if chat_id and message_id:
            if user_id:
                chat_user_ids[chat_id] = user_id
            if chat_id not in webapp_menu_set_chats:
                await set_webapp_menu_button(chat_id)
            current_last = chat_last_message_id.get(chat_id, 0)
            if message_id > current_last:
                chat_last_message_id[chat_id] = message_id
            await reschedule_chat_cleanup(chat_id, user_id)

        return await handler(event, data)


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
dp.message.outer_middleware(ActivityCleanupMiddleware())
dp.callback_query.outer_middleware(ActivityCleanupMiddleware())


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    chat_last_message_id[message.chat.id] = max(chat_last_message_id.get(message.chat.id, 0), message.message_id)
    await purge_chat_history(message.chat.id)
    await set_webapp_menu_button(message.chat.id)
    await refresh_main_message(message.chat.id)


@dp.message(F.text.in_({"старт", "страт", "start"}))
async def text_start_alias(message: Message, state: FSMContext):
    await cmd_start(message, state)


@dp.callback_query(F.data == "main_new")
async def main_new_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddRepair.waiting_photo)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💰 Без фото", callback_data="new_without_photo")]]
    )
    ask_msg = await callback.message.answer("Отправьте фото или выберите вариант без фото.", reply_markup=kb)
    await state.update_data(new_repair_ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)
    await callback.answer()


@dp.callback_query(F.data == "main_find")
async def main_find_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(FindRepair.waiting_seal)
    ask_msg = await callback.message.answer("Введите номер пломбы:", reply_markup=main_kb)
    await state.update_data(find_ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)
    await callback.answer()


async def start_quick_flow(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddQuick.waiting_data)
    ask_msg = await message.answer(
        "Введите данные ремонта без фото и без пломбы.\n"
        "Формат:\n"
        "<code>сумма, что сделал, деталь</code>\n"
        "или\n"
        "<code>сумма\nчто сделал\nдеталь</code>\n\n"
        "Поле деталь можно не указывать.",
        reply_markup=main_kb
    )
    await state.update_data(quick_ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)


@dp.callback_query(F.data == "new_without_photo")
async def new_without_photo_callback(callback: CallbackQuery, state: FSMContext):
    await start_quick_flow(callback.message, state)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.message(Command("cabinet"))
@dp.message(F.text == "👤 Личный кабинет")
async def open_cabinet(message: Message):
    webapp_url = build_webapp_url()
    if not webapp_url:
        msg = await message.answer("Личный кабинет пока не настроен.", reply_markup=main_kb)
        schedule_auto_delete(message.chat.id, msg.message_id)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть личный кабинет", web_app=WebAppInfo(url=webapp_url))]
        ]
    )
    msg = await message.answer("Открой личный кабинет:", reply_markup=kb)
    schedule_auto_delete(message.chat.id, msg.message_id, delay_seconds=600)


@dp.message(Command("today"))
@dp.message(F.text == "📅 Сегодня")
async def cmd_today(message: Message):
    rows = await get_today_repairs(message.from_user.id)
    if not rows:
        msg = await message.answer("За сегодня записей нет.", reply_markup=main_kb)
        schedule_auto_delete(message.chat.id, msg.message_id)
        return

    parts = ["<b>Записи за сегодня:</b>\n"]
    for row in rows[:20]:
        _, created_at, seal_number, work_done, amount, part_cost = row
        parts.append(
            f"• <b>{created_at}</b>\n"
            f"Пломба: {display_seal(seal_number)}\n"
            f"Сумма: {amount}\n"
            f"Деталь: {part_cost or '-'}\n"
            f"Ремонт: {work_done}\n"
        )

    info_msg = await message.answer("\n".join(parts), reply_markup=main_kb)
    schedule_auto_delete(message.chat.id, info_msg.message_id)


async def show_card_by_seal(message: Message, user_id: int, seal_number: str):
    repair = await get_repair_by_any_seal(user_id, seal_number)

    if not repair:
        msg = await message.answer(f"По пломбе <b>{seal_number}</b> ничего не найдено.", reply_markup=main_kb)
        schedule_auto_delete(message.chat.id, msg.message_id)
        return

    parent_repair_id = repair[0]
    history_rows = await get_repair_history(user_id, parent_repair_id)

    if not history_rows:
        msg = await message.answer(f"По пломбе <b>{seal_number}</b> ничего не найдено.", reply_markup=main_kb)
        schedule_auto_delete(message.chat.id, msg.message_id)
        return

    latest_row = history_rows[-1]
    _, latest_photo_file_id, _, _, _, _, _, _ = latest_row

    def build_timeline_block(title: str, items):
        lines = []
        for idx, (value, created_at) in enumerate(items):
            value_text = (value or "—").strip() or "—"
            date_text = (created_at or "—").strip() or "—"
            if idx == 0:
                lines.append(f"{title}: {value_text} ({date_text})")
            else:
                lines.append(f"{value_text} ({date_text})")
        return "\n".join(lines)

    seal_items = []
    amount_items = []
    part_items = []
    work_items = []
    for row in history_rows:
        created_at, _, hist_seal, work_done, amount, part_cost, _, _ = row
        seal_items.append((display_seal(hist_seal), created_at))
        amount_items.append((amount, created_at))
        part_items.append((part_cost or "-", created_at))
        work_items.append((work_done, created_at))

    seals_block = build_timeline_block("Пломба", seal_items)
    amounts_block = build_timeline_block("Сумма", amount_items)
    parts_block = build_timeline_block("Деталь", part_items)
    works_block = build_timeline_block("Тип ремонта", work_items)
    caption = (
        f"{seals_block}\n"
        f"{amounts_block}\n"
        f"{parts_block}\n"
        f"{works_block}"
    )

    card_msg = None
    photo_to_send = resolve_message_photo(latest_photo_file_id)

    if photo_to_send is not None:
        try:
            card_msg = await message.answer_photo(
                photo=photo_to_send,
                caption=caption,
                reply_markup=card_actions_kb(parent_repair_id)
            )
        except TelegramBadRequest:
            card_msg = None

    if card_msg is None:
        card_msg = await message.answer(
            caption,
            reply_markup=card_actions_kb(parent_repair_id)
        )

    schedule_auto_delete(message.chat.id, card_msg.message_id)
    return [card_msg.message_id]


@dp.message(Command("find"))
async def cmd_find(message: Message, state: FSMContext, command: CommandObject):
    if command.args:
        seal_number = command.args.strip()
        await show_card_by_seal(message, message.from_user.id, seal_number)
        return

    await state.clear()
    await state.set_state(FindRepair.waiting_seal)
    ask_msg = await message.answer("Введите номер пломбы:", reply_markup=main_kb)
    await state.update_data(find_ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)


@dp.message(F.text == "🔎 Найти")
@dp.message(F.text.regexp(r"(?i)^\s*(?:(?:🔎|🔍)\s*)?(?:найти|поиск)\s*$"))
async def find_button(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FindRepair.waiting_seal)
    ask_msg = await message.answer("Введите номер пломбы:", reply_markup=main_kb)
    await state.update_data(find_ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)


@dp.message(FindRepair.waiting_seal)
async def process_find_seal(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    seal_number = (message.text or "").strip()

    if not seal_number:
        await reset_flow_with_cleanup(message, state)
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


@dp.callback_query(F.data.startswith("edit_seal:"))
async def edit_seal_callback(callback: CallbackQuery, state: FSMContext):
    try:
        parent_repair_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("Ошибка редактирования", show_alert=True)
        return

    current_seal = await get_main_repair_seal(callback.from_user.id, parent_repair_id)
    if not current_seal:
        await callback.answer("Карточка не найдена", show_alert=True)
        return

    await state.clear()
    await state.update_data(edit_parent_repair_id=parent_repair_id)

    ask_msg = await callback.message.answer(
        "✏️ Введите новые данные (в строку или в столбик).\n"
        "Можно 3 или 4 поля (деталь — опционально):\n"
        "<code>новая пломба, сумма, тип ремонта, деталь</code>\n"
        "или\n"
        "<code>новая пломба\nсумма\nтип ремонта\nдеталь</code>\n\n"
        f"Текущая пломба: <b>{current_seal}</b>\n"
        "Пример:\n"
        "<code>321, 4500, замена стика, 1200</code>",
        reply_markup=main_kb
    )
    await state.update_data(edit_ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)
    await state.set_state(EditSeal.waiting_new_seal)

    await callback.answer("Введите новую пломбу")


@dp.message(EditSeal.waiting_new_seal)
async def process_edit_seal(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    parsed = parse_history_line(message.text or "")
    if not parsed:
        await reset_flow_with_cleanup(message, state)
        return

    data = await state.get_data()
    parent_repair_id = data.get("edit_parent_repair_id")
    ask_message_id = data.get("edit_ask_message_id")

    if not parent_repair_id:
        await reset_flow_with_cleanup(message, state)
        return

    status, old_seal, updated_seal, old_created_at, new_created_at = await update_main_repair_seal(
        user_id=message.from_user.id,
        parent_repair_id=parent_repair_id,
        new_seal_number=parsed["seal_number"],
        new_amount=parsed["amount"],
        new_work_done=parsed["work_done"],
        new_part_cost=parsed.get("part_cost")
    )

    await safe_delete_message(message)
    await safe_delete_by_id(bot, message.chat.id, ask_message_id)

    if status == "not_found":
        await reset_flow_with_cleanup(message, state)
        return

    if status == "same":
        await reset_flow_with_cleanup(message, state)
        return

    if status == "duplicate":
        await reset_flow_with_cleanup(message, state)
        return

    await state.clear()
    await show_card_by_seal(message, message.from_user.id, updated_seal)


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
    await remember_flow_bot_message(state, ask_msg.message_id)
    await state.set_state(AddHistory.waiting_photo)


@dp.message(AddHistory.waiting_photo, F.photo)
async def handle_add_photo(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    photo_file_id = message.photo[-1].file_id
    data = await state.get_data()
    parent_repair_id = data.get("parent_repair_id")
    old_ask_message_id = data.get("add_ask_message_id")

    parsed_from_caption = parse_history_line(message.caption or "")
    if parsed_from_caption:
        if not parent_repair_id:
            await reset_flow_with_cleanup(message, state)
            return

        if await seal_exists_anywhere(message.from_user.id, parsed_from_caption["seal_number"]):
            await reset_flow_with_cleanup(message, state)
            return

        await save_history(
            parent_repair_id=parent_repair_id,
            user_id=message.from_user.id,
            photo_file_id=photo_file_id,
            seal_number=parsed_from_caption["seal_number"],
            work_done=parsed_from_caption["work_done"],
            amount=parsed_from_caption["amount"],
            part_cost=parsed_from_caption.get("part_cost")
        )

        await safe_delete_message(message)
        await safe_delete_by_id(bot, message.chat.id, old_ask_message_id)

        ok_msg = await message.answer(
            "✅ Повторный ремонт добавлен\n"
            f"Новая пломба: {parsed_from_caption['seal_number']}\n"
            f"Сумма: {parsed_from_caption['amount']}\n"
            f"Деталь: {parsed_from_caption.get('part_cost') or '-'}\n"
            f"Ремонт: {parsed_from_caption['work_done']}",
            reply_markup=main_kb
        )

        await state.clear()
        await asyncio.sleep(2)
        await safe_delete_message(ok_msg)
        await send_main_menu(message)
        return

    await state.update_data(history_photo_file_id=photo_file_id)

    ask_msg = await message.answer(
        "Фото получено.\n\n"
        "Можно сразу в подписи к фото написать:\n"
        "<code>новая пломба, сумма, что сделал, деталь</code>\n\n"
        "Или отправить данные следующим сообщением.\n"
        "Формат:\n"
        "<code>новая пломба, сумма, что сделал, деталь</code>\n\n"
        "Пример:\n"
        "<code>1881, 2500, заменил разъем питания, 900</code>",
        reply_markup=main_kb
    )

    await state.update_data(history_data_ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)
    await state.set_state(AddHistory.waiting_data)

    await safe_delete_message(message)
    await safe_delete_by_id(bot, message.chat.id, old_ask_message_id)


@dp.message(AddHistory.waiting_photo)
async def handle_add_photo_invalid(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    await reset_flow_with_cleanup(message, state)


@dp.message(AddHistory.waiting_data)
async def handle_add_data(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    parsed = parse_history_line(message.text or "")

    if not parsed:
        await reset_flow_with_cleanup(message, state)
        return

    data = await state.get_data()
    parent_repair_id = data.get("parent_repair_id")
    photo_file_id = data.get("history_photo_file_id")
    ask_message_id = data.get("history_data_ask_message_id")

    if not parent_repair_id or not photo_file_id:
        await reset_flow_with_cleanup(message, state)
        return

    if await seal_exists_anywhere(message.from_user.id, parsed["seal_number"]):
        await reset_flow_with_cleanup(message, state)
        return

    await save_history(
        parent_repair_id=parent_repair_id,
        user_id=message.from_user.id,
        photo_file_id=photo_file_id,
        seal_number=parsed["seal_number"],
        work_done=parsed["work_done"],
        amount=parsed["amount"],
        part_cost=parsed.get("part_cost")
    )

    await safe_delete_message(message)
    await safe_delete_by_id(bot, message.chat.id, ask_message_id)

    ok_msg = await message.answer(
        "✅ Повторный ремонт добавлен\n"
        f"Новая пломба: {parsed['seal_number']}\n"
        f"Сумма: {parsed['amount']}\n"
        f"Деталь: {parsed.get('part_cost') or '-'}\n"
        f"Ремонт: {parsed['work_done']}",
        reply_markup=main_kb
    )

    await state.clear()
    await asyncio.sleep(2)
    await safe_delete_message(ok_msg)
    await send_main_menu(message)


@dp.message(Command("new"))
@dp.message(F.text == "🆕 Новый ремонт")
@dp.message(F.text.regexp(r"(?i)^\s*(?:🆕\s*)?новый\s+ремонт\s*$"))
async def new_repair_button(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddRepair.waiting_photo)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💰 Без фото", callback_data="new_without_photo")]]
    )
    ask_msg = await message.answer("Отправьте фото или выберите вариант без фото.", reply_markup=kb)
    await state.update_data(new_repair_ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)


@dp.message(Command("quick"))
async def quick_repair_button(message: Message, state: FSMContext):
    await start_quick_flow(message, state)


@dp.message(AddQuick.waiting_data)
async def handle_quick_data(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    parsed = parse_quick_line(message.text or "")
    if not parsed:
        await reset_flow_with_cleanup(message, state)
        return

    data = await state.get_data()
    ask_message_id = data.get("quick_ask_message_id")

    await save_repair(
        user_id=message.from_user.id,
        photo_file_id=None,
        seal_number=make_virtual_seal(),
        work_done=parsed["work_done"],
        amount=parsed["amount"],
        part_cost=parsed.get("part_cost")
    )

    await safe_delete_message(message)
    await safe_delete_by_id(bot, message.chat.id, ask_message_id)

    ok_msg = await message.answer(
        "✅ Запись сохранена (без фото)\n"
        f"Сумма: {parsed['amount']}\n"
        f"Деталь: {parsed.get('part_cost') or '-'}\n"
        f"Ремонт: {parsed['work_done']}",
        reply_markup=main_kb
    )
    await state.clear()
    await asyncio.sleep(2)
    await safe_delete_message(ok_msg)
    await send_main_menu(message)


@dp.message(AddRepair.waiting_photo, F.photo)
async def handle_new_photo_from_state(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    photo_file_id = message.photo[-1].file_id
    data = await state.get_data()
    old_ask_message_id = data.get("new_repair_ask_message_id")

    parsed_from_caption = parse_new_repair_line(message.caption or "")
    if parsed_from_caption:
        if await seal_exists_anywhere(message.from_user.id, parsed_from_caption["seal_number"]):
            await reset_flow_with_cleanup(message, state)
            return

        await save_repair(
            user_id=message.from_user.id,
            photo_file_id=photo_file_id,
            seal_number=parsed_from_caption["seal_number"],
            work_done=parsed_from_caption["work_done"],
            amount=parsed_from_caption["amount"],
            part_cost=parsed_from_caption.get("part_cost")
        )

        await safe_delete_message(message)
        await safe_delete_by_id(bot, message.chat.id, old_ask_message_id)

        ok_msg = await message.answer(
            "✅ Запись сохранена\n"
            f"Пломба: {parsed_from_caption['seal_number']}\n"
            f"Сумма: {parsed_from_caption['amount']}\n"
            f"Деталь: {parsed_from_caption.get('part_cost') or '-'}\n"
            f"Ремонт: {parsed_from_caption['work_done']}",
            reply_markup=main_kb
        )

        await state.clear()
        await asyncio.sleep(2)
        await safe_delete_message(ok_msg)
        await send_main_menu(message)
        return

    await state.update_data(photo_file_id=photo_file_id)

    ask_msg = await message.answer(
        "Фото получено.\n\n"
        "Можно сразу в подписи к фото написать:\n"
        "<code>номер пломбы, сумма, тип ремонта, деталь</code>\n\n"
        "Или отправить данные следующим сообщением.\n"
        "Формат:\n"
        "<code>номер пломбы, сумма, тип ремонта, деталь</code>\n\n"
        "Пример:\n"
        "<code>1542, 3500, замена блока питания, 1200</code>",
        reply_markup=main_kb
    )

    await state.update_data(ask_message_id=ask_msg.message_id)
    await remember_flow_bot_message(state, ask_msg.message_id)
    await state.set_state(AddRepair.waiting_data)

    await safe_delete_message(message)


@dp.message(AddRepair.waiting_photo)
async def handle_new_photo_invalid(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    await reset_flow_with_cleanup(message, state)


@dp.message(AddRepair.waiting_data)
async def handle_new_data(message: Message, state: FSMContext):
    await remember_flow_user_message(state, message.message_id)
    parsed = parse_new_repair_line(message.text or "")

    if not parsed:
        await reset_flow_with_cleanup(message, state)
        return

    data = await state.get_data()
    photo_file_id = data.get("photo_file_id")
    ask_message_id = data.get("ask_message_id")

    if not photo_file_id:
        await reset_flow_with_cleanup(message, state)
        return

    if await seal_exists_anywhere(message.from_user.id, parsed["seal_number"]):
        await reset_flow_with_cleanup(message, state)
        return

    await save_repair(
        user_id=message.from_user.id,
        photo_file_id=photo_file_id,
        seal_number=parsed["seal_number"],
        work_done=parsed["work_done"],
        amount=parsed["amount"],
        part_cost=parsed.get("part_cost")
    )

    await safe_delete_message(message)
    await safe_delete_by_id(bot, message.chat.id, ask_message_id)

    ok_msg = await message.answer(
        "✅ Запись сохранена\n"
        f"Пломба: {parsed['seal_number']}\n"
        f"Сумма: {parsed['amount']}\n"
        f"Деталь: {parsed.get('part_cost') or '-'}\n"
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
    return
    

async def main():
    await init_db()
    await refresh_saved_main_messages()
    await set_webapp_menu_button()
    web_runner = await start_webapp_server()
    try:
        await dp.start_polling(bot)
    finally:
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
