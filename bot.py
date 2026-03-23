import os
import asyncio
import json
import hmac
import hashlib
import re
from datetime import datetime, timedelta
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
DB_PATH = "repairs.db"
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


def parse_card_date(value: str | None):
    try:
        return datetime.strptime((value or "").strip(), "%d.%m.%Y")
    except Exception:
        return None


def is_in_period(created_at: str | None, period_days: int) -> bool:
    dt = parse_card_date(created_at)
    if not dt:
        return False
    start_date = (datetime.now() - timedelta(days=period_days - 1)).date()
    return dt.date() >= start_date


def make_virtual_seal() -> str:
    return f"{NO_SEAL_PREFIX}{int(datetime.now().timestamp() * 1000)}"


def is_virtual_seal(seal_number: str | None) -> bool:
    return (seal_number or "").startswith(NO_SEAL_PREFIX)


def display_seal(seal_number: str | None) -> str:
    if is_virtual_seal(seal_number):
        return "без пломбы"
    return (seal_number or "-").strip() or "-"


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
    if isinstance(message, CallbackQuery):
        return await ensure_main_message(message.message.chat.id)
    return await ensure_main_message(message.chat.id)


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

        cursor = await db.execute("PRAGMA table_info(repairs)")
        repair_cols = {row[1] for row in await cursor.fetchall()}
        if "part_cost" not in repair_cols:
            await db.execute("ALTER TABLE repairs ADD COLUMN part_cost TEXT")

        cursor = await db.execute("PRAGMA table_info(repair_history)")
        history_cols = {row[1] for row in await cursor.fetchall()}
        if "part_cost" not in history_cols:
            await db.execute("ALTER TABLE repair_history ADD COLUMN part_cost TEXT")

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


async def save_main_message_id(chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO chat_main_messages (chat_id, message_id)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET message_id = excluded.message_id
        """, (chat_id, message_id))
        await db.commit()

    chat_main_message_id[chat_id] = message_id


async def ensure_main_message(chat_id: int):
    message_id = chat_main_message_id.get(chat_id)
    if not message_id:
        message_id = await get_saved_main_message_id(chat_id)
        if message_id:
            chat_main_message_id[chat_id] = message_id

    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=MAIN_MESSAGE_TEXT,
                reply_markup=main_kb
            )
            return message_id
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=main_kb
                    )
                except Exception:
                    pass
                return message_id
        except Exception:
            pass

    msg = await bot.send_message(chat_id=chat_id, text=MAIN_MESSAGE_TEXT, reply_markup=main_kb)
    await save_main_message_id(chat_id, msg.message_id)
    return msg.message_id


def is_main_message(chat_id: int, message_id: int):
    if not chat_id or not message_id:
        return False
    return chat_main_message_id.get(chat_id) == message_id


def build_webapp_url():
    if not WEBAPP_URL:
        return None
    return f"{WEBAPP_URL}/cabinet"


async def set_webapp_menu_button(chat_id: int):
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


async def get_cabinet_dashboard(user_id: int, period_days: int):
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

        if is_in_period(created_at, period_days):
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
    cards.sort(key=lambda x: x["card_id"], reverse=True)
    ledger_items.sort(key=lambda x: (parse_card_date(x["created_at"]) or datetime.min), reverse=True)

    net_without_parts = gross - details_total
    percent_40_cost = net_without_parts * 0.4
    net_after_percent = net_without_parts - percent_40_cost

    summary = {
        "gross_revenue": format_money(gross),
        "parts_cost": format_money(details_total),
        "net_without_parts": format_money(net_without_parts),
        "percent_40_cost": format_money(percent_40_cost),
        "net_after_percent": format_money(net_after_percent),
        "transactions_count": transactions_count
    }
    return cards, summary, ledger_items


WEBAPP_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Личный кабинет</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: #f5f6f8;
      --card: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --line: #e5e7eb;
      --accent: #0f766e;
    }
    body { margin: 0; background: linear-gradient(180deg, #e8f4f1 0%, var(--bg) 65%); color: var(--text); font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
    .wrap { max-width: 980px; margin: 0 auto; padding: 16px 16px 92px; }
    .head { background: var(--card); border-radius: 14px; padding: 14px; box-shadow: 0 6px 24px rgba(0,0,0,.06); margin-bottom: 12px; }
    h1 { font-size: 20px; margin: 0 0 6px; }
    .meta { color: var(--muted); font-size: 13px; }
    .tabs { display: flex; gap: 8px; }
    .tab-btn { border: 1px solid var(--line); background: #fff; border-radius: 999px; padding: 7px 12px; font-size: 13px; }
    .tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .tabs-panel {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(245,246,248,.96);
      backdrop-filter: blur(6px);
      border-top: 1px solid var(--line);
      padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
      z-index: 20;
    }
    .tabs-shell { max-width: 980px; margin: 0 auto; }
    .switch { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }
    .sw-btn { border: 1px solid var(--line); background: #fff; color: var(--text); border-radius: 999px; padding: 8px 12px; font-size: 13px; }
    .sw-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .stats { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; margin-bottom: 12px; }
    .stat { background: var(--card); border-radius: 12px; border: 1px solid var(--line); padding: 10px; }
    .stat .k { color: var(--muted); font-size: 12px; }
    .stat .v { margin-top: 4px; font-size: 18px; font-weight: 700; }
    .stat.clickable { cursor: pointer; transition: .15s transform ease, .15s box-shadow ease; }
    .stat.clickable:active { transform: scale(0.99); }
    .stat.clickable.active { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(15,118,110,.15) inset; }
    .grid { display: grid; gap: 10px; }
    .cards-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px; }
    .hidden { display: none; }
    details.item { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px; }
    summary.top { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 4px; cursor: pointer; list-style: none; }
    summary.top::-webkit-details-marker { display: none; }
    .summary-left { display: flex; gap: 10px; align-items: center; min-width: 0; }
    .thumb { width: 42px; height: 42px; border-radius: 10px; border: 1px solid var(--line); background: #fff; display: flex; align-items: center; justify-content: center; overflow: hidden; font-size: 20px; color: #9ca3af; }
    .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .stage-list { margin-top: 10px; border-top: 1px dashed var(--line); padding-top: 8px; display: grid; gap: 8px; }
    .stage { border-radius: 10px; background: #f9fafb; border: 1px solid var(--line); padding: 8px; }
    .seal { font-size: 18px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .date { color: var(--muted); font-size: 13px; }
    .row { margin: 4px 0; white-space: pre-wrap; word-break: break-word; }
    .ledger-item { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 10px; }
    .ledger-top { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
    .plus { color: #065f46; font-weight: 700; }
    .minus { color: #b91c1c; font-weight: 700; }
    .empty { color: var(--muted); text-align: center; padding: 24px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h1>Личный кабинет</h1>
      <div id="meta" class="meta">Загрузка...</div>
      <div class="switch">
        <button class="sw-btn active" data-period="7">1 неделя</button>
        <button class="sw-btn" data-period="30">1 месяц</button>
        <button class="sw-btn" data-period="90">3 месяца</button>
      </div>
    </div>
    <div id="stats" class="stats"></div>
    <div id="cardsList" class="grid cards-grid hidden"></div>
    <div id="ledgerList" class="grid"></div>
  </div>
  <div class="tabs-panel">
    <div class="tabs-shell">
      <div class="tabs">
        <button class="tab-btn" data-tab="cards">Карточки</button>
        <button class="tab-btn active" data-tab="ledger">Учет</button>
      </div>
    </div>
  </div>
  <script>
    const tg = window.Telegram.WebApp;
    tg.ready();
    tg.expand();
    let activeTab = "ledger";
    let ledgerFilter = "all";
    let currentData = null;

    function money(v) {
      return (v ?? "0") + " ₽";
    }

    function renderSummary(summary) {
      const stats = document.getElementById("stats");
      stats.innerHTML = `
        <div class="stat"><div class="k">Валовая выручка</div><div class="v">${money(summary.gross_revenue)}</div></div>
        <div class="stat"><div class="k">Расход на детали</div><div class="v">${money(summary.parts_cost)}</div></div>
        <div class="stat"><div class="k">Чистая без деталей</div><div class="v">${money(summary.net_without_parts)}</div></div>
        <div class="stat"><div class="k">Расход 40%</div><div class="v">${money(summary.percent_40_cost)}</div></div>
        <div class="stat"><div class="k">Итог после -40%</div><div class="v">${money(summary.net_after_percent)}</div></div>
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

    async function loadThumb(photoRef, imgId) {
      try {
        const resp = await fetch("/api/cabinet/photo", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ initData: tg.initData, photoRef })
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
        list.innerHTML = '<div class="empty">Пока нет записей</div>';
        return;
      }

      list.innerHTML = cards.map(card => `
        <details class="item">
          <summary class="top">
            <div class="summary-left">
              <div class="thumb">
                ${card.latest_has_photo ? `<img id="img-${card.card_id}" alt="photo" />` : "?"}
              </div>
              <div>
                <div class="seal">${card.latest_seal_number || "—"}</div>
                <div class="date">${card.latest_created_at || "—"}</div>
              </div>
            </div>
          </summary>
          <div class="stage-list">
            ${card.stages.map(stage => `
              <div class="stage">
                <div class="row"><b>${stage.stage_type === "main" ? "Основная запись" : "Этап ремонта"}</b> · ${stage.created_at || "—"}</div>
                <div class="row">Пломба: ${stage.seal_number || "—"}</div>
                <div class="row">Сумма: ${stage.amount || "—"}</div>
                <div class="row">Деталь: ${stage.part_cost || "—"}</div>
                <div class="row">Тип ремонта: ${stage.work_done || "—"}</div>
              </div>
            `).join("")}
          </div>
        </details>
      `).join("");

      cards.forEach(card => {
        if (card.latest_has_photo && card.latest_photo_ref) {
          loadThumb(card.latest_photo_ref, `img-${card.card_id}`);
        }
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
      const cards = document.getElementById("cardsList");
      const ledger = document.getElementById("ledgerList");
      cards.classList.toggle("hidden", activeTab !== "cards");
      ledger.classList.toggle("hidden", activeTab !== "ledger");
      document.querySelectorAll(".tab-btn").forEach(btn => btn.classList.toggle("active", btn.dataset.tab === activeTab));
      const opsCard = document.getElementById("opsWithAmountCard");
      if (opsCard) opsCard.classList.toggle("active", activeTab === "ledger" && ledgerFilter === "with_amount");
    }

    async function loadData(periodDays) {
      const resp = await fetch("/api/cabinet/repairs", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ initData: tg.initData, periodDays })
      });

      if (!resp.ok) {
        document.getElementById("meta").textContent = "Ошибка загрузки данных";
        return;
      }

      const data = await resp.json();
      currentData = data;
      const meta = document.getElementById("meta");
      meta.textContent = "Карточек: " + data.count + " · Период: " + data.period_label;
      renderSummary(data.summary);
      renderCards(data.cards || []);
      renderLedger(data.ledger || []);
      applyTab();
    }

    document.querySelectorAll(".tab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        activeTab = btn.dataset.tab;
        if (activeTab === "ledger") ledgerFilter = "all";
        applyTab();
        if (currentData) renderLedger(currentData.ledger || []);
      });
    });

    document.querySelectorAll(".sw-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".sw-btn").forEach(x => x.classList.remove("active"));
        btn.classList.add("active");
        loadData(Number(btn.dataset.period)).catch(() => {
          document.getElementById("meta").textContent = "Ошибка загрузки данных";
        });
      });
    });

    loadData(7).catch(() => {
      document.getElementById("meta").textContent = "Ошибка загрузки данных";
    });
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

    # Local imported photo path
    if photo_ref.startswith("/") and os.path.exists(photo_ref):
        try:
            with open(photo_ref, "rb") as f:
                data = f.read()
            ext = os.path.splitext(photo_ref)[1].lower()
            content_type = "image/jpeg"
            if ext == ".png":
                content_type = "image/png"
            elif ext == ".webp":
                content_type = "image/webp"
            return web.Response(body=data, content_type=content_type)
        except Exception:
            return web.json_response({"ok": False, "error": "photo_read_error"}, status=500)

    # Telegram file_id
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

    cards, summary, ledger_items = await get_cabinet_dashboard(user_id, period_days)
    period_label = {7: "1 неделя", 30: "1 месяц", 90: "3 месяца"}[period_days]
    return web.json_response({
        "ok": True,
        "count": len(cards),
        "period_days": period_days,
        "period_label": period_label,
        "summary": summary,
        "cards": cards,
        "ledger": ledger_items
    })


async def start_webapp_server():
    app = web.Application()
    app.router.add_get("/cabinet", cabinet_page)
    app.router.add_post("/api/cabinet/repairs", cabinet_repairs_api)
    app.router.add_post("/api/cabinet/photo", cabinet_photo_api)
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


async def safe_delete_message(message: Message):
    if is_main_message(message.chat.id, message.message_id):
        return
    try:
        await message.delete()
    except Exception:
        pass


async def safe_delete_by_id(bot: Bot, chat_id: int, message_id):
    if not message_id:
        return
    if is_main_message(chat_id, message_id):
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def purge_chat_history(chat_id: int):
    last_id = chat_last_message_id.get(chat_id)
    if not last_id:
        return

    start_id = max(1, last_id - CHAT_SWEEP_BACK_MESSAGES)
    for msg_id in range(last_id, start_id - 1, -1):
        await safe_delete_by_id(bot, chat_id, msg_id)


async def delayed_chat_cleanup(chat_id: int, anchor_message_id: int):
    await asyncio.sleep(CHAT_CLEANUP_SECONDS)
    if chat_last_message_id.get(chat_id) != anchor_message_id:
        return
    await purge_chat_history(chat_id)


def reschedule_chat_cleanup(chat_id: int):
    task = chat_cleanup_tasks.get(chat_id)
    if task and not task.done():
        task.cancel()

    anchor = chat_last_message_id.get(chat_id, 0)
    chat_cleanup_tasks[chat_id] = asyncio.create_task(
        delayed_chat_cleanup(chat_id, anchor)
    )


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

        if isinstance(event, Message):
            chat_id = event.chat.id
            message_id = event.message_id
        elif isinstance(event, CallbackQuery) and event.message:
            chat_id = event.message.chat.id
            message_id = event.message.message_id

        if chat_id and message_id:
            if chat_id not in webapp_menu_set_chats:
                await set_webapp_menu_button(chat_id)
            await ensure_main_message(chat_id)
            current_last = chat_last_message_id.get(chat_id, 0)
            if message_id > current_last:
                chat_last_message_id[chat_id] = message_id
            reschedule_chat_cleanup(chat_id)

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
    await ensure_main_message(message.chat.id)
    await message.answer("Меню обновлено", reply_markup=main_kb)


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

    if latest_photo_file_id:
        photo_to_send = latest_photo_file_id
        if isinstance(latest_photo_file_id, str) and latest_photo_file_id.startswith("/") and os.path.exists(latest_photo_file_id):
            photo_to_send = FSInputFile(latest_photo_file_id)

        card_msg = await message.answer_photo(
            photo=photo_to_send,
            caption=caption,
            reply_markup=card_actions_kb(parent_repair_id)
        )
    else:
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
    await send_main_menu(message)
    

async def main():
    await init_db()
    web_runner = await start_webapp_server()
    try:
        await dp.start_polling(bot)
    finally:
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
