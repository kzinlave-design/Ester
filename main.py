"""
Telegram-бот комплекса «Эстет» на Flask (для деплоя на Render.com через Webhook).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask, request, abort

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("estet_bot")

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

DT_FMT = "%Y-%m-%d %H:%M"
TS_FMT = "%Y-%m-%d %H:%M:%S"
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

WA_1_TEXT = "💬 WhatsApp"
WA_1_URL = "https://wa.me/77471777711"
WA_2_TEXT = "💬 WhatsApp"
WA_2_URL = "https://wa.me/77752191479"

STATUS_LABELS = {
    "booked": "🕓 Ожидает",
    "confirmed": "✅ Подтверждена",
    "cancelled": "❌ Отменена",
    "completed": "✔️ Завершена",
}


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_chat_ids: tuple[int, ...]
    firebase_url: str
    tz: ZoneInfo
    work_start: int
    work_end: int
    slot_days: int
    address: str
    reminder_hours: int = 2
    webhook_url: str = ""
    port: int = 10000


def load_config() -> Config:
    token = "9028301266:AAFBhryrHI_wRBegiIp0Cf3KizpawRaq1K8"
    firebase_url = "https://riba-26a05-default-rtdb.europe-west1.firebasedatabase.app"

    raw_admins = os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "")
    admin_ids = tuple(int(x) for x in raw_admins.split(",") if x)
    
    # Render автоматически подставляет переменную RENDER_EXTERNAL_URL (например, https://your-app.onrender.com)
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    webhook_path = os.getenv("WEBHOOK_PATH", f"/webhook/{token}")
    webhook_url = f"{render_url}{webhook_path}" if render_url else ""

    return Config(
        bot_token=token,
        admin_chat_ids=admin_ids,
        firebase_url=firebase_url.rstrip("/"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Asia/Aqtobe")),
        work_start=int(os.getenv("WORK_START_HOUR", "10")),
        work_end=int(os.getenv("WORK_END_HOUR", "20")),
        slot_days=int(os.getenv("SLOT_DAYS_AHEAD", "14")),
        address=os.getenv("SHOP_ADDRESS", "уточняйте по телефону"),
        webhook_url=webhook_url,
        port=int(os.getenv("PORT", "10000")),
    )


def now_local(cfg: Config) -> datetime:
    return datetime.now(cfg.tz).replace(tzinfo=None, microsecond=0)


# --------------------------------------------------------------------------- #
# Прайс-лист и мастера
# --------------------------------------------------------------------------- #

DEFAULT_MASTERS = ("Артём", "Дамир", "Руслан")

DEFAULT_SERVICES = (
    ("💈 Барбершоп", "Стрижка", "4 000–5 000", 60),
    ("💈 Барбершоп", "Подростковая стрижка (12–16 лет)", "3 500–4 500", 60),
    ("💈 Барбершоп", "Детская стрижка (до 12 лет)", "3 000–4 000", 60),
    ("💈 Барбершоп", "Удлиненная стрижка", "5 000–6 000", 60),
    ("💈 Барбершоп", "БИОзавивка волос", "15 000–20 000", 120),
    ("💈 Барбершоп", "Моделирование бороды", "3 000–4 000", 60),
    ("💈 Барбершоп", "Комплекс старший Барбер (Борода + Стрижка)", "9 000", 60),
    ("💈 Барбершоп", "LITE чистка лица", "4 000", 60),
    ("💈 Барбершоп", "Тонирование бороды или волос", "4 000", 60),
    ("💈 Барбершоп", "Удаление волос воском (1 зона)", "1 000", 30),
    ("💈 Барбершоп", "Окантовка", "2 000", 30),
    ("💈 Барбершоп", "Мытье головы + укладка", "2 000", 30),
    ("💈 Барбершоп", "Стрижка усов", "1 000", 30),
    ("💈 Барбершоп", "Трихоскоп — проверка луковиц волос", "2 000–3 000", 30),
    ("💈 Барбершоп", "HEAD SPA японский массаж головы", "9 900", 60),
    ("💈 Барбершоп", "Полный комплекс услуг (одежда, спа, барбершоп, косметолог, подолог)", "25 000", 120),
    ("🧖 Спа программы", "HEAD SPA японский массаж головы (50 мин)", "9 900", 60),
    ("🧖 Спа программы", "Программы на 1 персону", "18 000–35 000", 120),
    ("🧖 Спа программы", "Программы на 2 персоны", "35 000–45 000", 120),
    ("🧖 Спа программы", "Программы на 3 персоны", "50 000–65 000", 120),
    ("🦶 Подология", "Сеанс у подолога (медицинский педикюр)", "10 000–13 000", 60),
    ("🦶 Подология", "Классический маникюр", "5 000", 60),
    ("🦶 Подология", "Классический педикюр обработка", "8 000–10 000", 60),
    ("🦶 Подология", "Установка Титановой нити (1 ноготь)", "15 000", 60),
    ("🦶 Подология", "Удаление бородавок (сеанс)", "5 000", 30),
    ("🦶 Подология", "Лечение грибка на ногтях", "5 000–10 000", 60),
    ("💆 Массажный кабинет", "Классический массаж (60 мин)", "14 000", 60),
    ("💆 Массажный кабинет", "Массаж в 4 руки (60 мин)", "25 000", 60),
    ("💆 Массажный кабинет", "Шейно-воротниковый массаж (20 мин)", "5 000", 30),
    ("💆 Массажный кабинет", "Стоун массаж (60 мин)", "12 000", 60),
    ("💆 Массажный кабинет", "Массаж лица (20 мин)", "5 000", 30),
    ("💆 Массажный кабинет", "Массаж спины (30 мин)", "8 000", 30),
    ("💆 Массажный кабинет", "Детский массаж (30 мин)", "9 000", 30),
    ("💆 Массажный кабинет", "Массаж головы (15 мин)", "5 000", 30),
    ("💆 Массажный кабинет", "Лимфодренажный массаж (60 мин)", "15 000", 60),
    ("💆 Массажный кабинет", "Пояснично-крестцовый массаж (20 мин)", "5 000", 30),
    ("💆 Массажный кабинет", "Массаж стоп (15 мин)", "3 000", 30),
    ("✨ Косметология", "Услуга 3в1 — чистка, пилинг, альгинатная маска", "15 000", 60),
    ("✨ Косметология", "Чистка лица + карбоновый пилинг + альгинатная маска", "30 000", 60),
    ("✨ Косметология", "Чистка лица + пилинг", "25 000", 60),
    ("✨ Косметология", "Мезотерапия волос", "30 000", 60),
    ("✨ Косметология", "Удаление тату (сеанс)", "6 000–20 000", 60),
    ("✨ Косметология", "Консультация косметолога", "5 000", 30),
)


def _iter_items(data) -> list[tuple[str, dict]]:
    if not data:
        return []
    if isinstance(data, dict):
        return [(str(k), v) for k, v in data.items() if isinstance(v, dict)]
    if isinstance(data, list):
        return [(str(i), v) for i, v in enumerate(data) if isinstance(v, dict)]
    return []


# --------------------------------------------------------------------------- #
# Firebase DB
# --------------------------------------------------------------------------- #

class FirebaseDatabase:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(timeout=15.0)
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str):
        resp = await self._client.get(f"{self.base_url}/{path}.json")
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, data) -> None:
        resp = await self._client.put(f"{self.base_url}/{path}.json", json=data)
        resp.raise_for_status()

    async def _post(self, path: str, data: dict) -> str:
        resp = await self._client.post(f"{self.base_url}/{path}.json", json=data)
        resp.raise_for_status()
        return (resp.json() or {}).get("name", "")

    async def _patch(self, path: str, data: dict) -> None:
        resp = await self._client.patch(f"{self.base_url}/{path}.json", json=data)
        resp.raise_for_status()

    async def init_schema(self) -> None:
        async with self._lock:
            masters = await self._get("masters")
            if not _iter_items(masters):
                m_data = {
                    f"m_{i}": {"id": i, "name": name, "is_active": 1}
                    for i, name in enumerate(DEFAULT_MASTERS)
                }
                await self._put("masters", m_data)

            services = await self._get("services")
            if not _iter_items(services):
                s_data = {
                    f"s_{i}": {
                        "id": i,
                        "group_name": grp,
                        "category": cat,
                        "price": price,
                        "duration_min": dur,
                        "is_active": 1,
                    }
                    for i, (grp, cat, price, dur) in enumerate(DEFAULT_SERVICES)
                }
                await self._put("services", s_data)

    async def ensure_slots(self, cfg: Config) -> None:
        today = now_local(cfg).date()
        async with self._lock:
            masters_raw = await self._get("masters")
            master_ids = [
                m["id"] for _, m in _iter_items(masters_raw) if m.get("is_active") == 1
            ]
            if not master_ids:
                return

            slots_raw = await self._get("slots")
            existing = {
                (s.get("master_id"), s.get("start_at"))
                for _, s in _iter_items(slots_raw)
            }

            updates: dict[str, dict] = {}
            for offset in range(cfg.slot_days + 1):
                day = today + timedelta(days=offset)
                for hour in range(cfg.work_start, cfg.work_end):
                    stamp = datetime(day.year, day.month, day.day, hour).strftime(DT_FMT)
                    for mid in master_ids:
                        if (mid, stamp) not in existing:
                            key = f"{mid}_{day:%Y%m%d}_{hour:02d}"
                            updates[key] = {
                                "master_id": mid,
                                "start_at": stamp,
                                "is_booked": 0,
                            }

            if updates:
                await self._patch("slots", updates)
            logger.info("Слоты в Firebase обновлены (добавлено новых: %d)", len(updates))

    async def get_masters_with_free_slots(self, now: datetime) -> list[dict]:
        slots_raw = await self._get("slots")
        masters_raw = await self._get("masters")
        now_str = now.strftime(DT_FMT)

        active_masters = {
            m["id"]: m["name"]
            for _, m in _iter_items(masters_raw)
            if m.get("is_active") == 1
        }
        available_mids = {
            s.get("master_id")
            for _, s in _iter_items(slots_raw)
            if s.get("is_booked") == 0 and s.get("start_at", "") > now_str
        }
        result = [
            {"id": mid, "name": active_masters[mid]}
            for mid in available_mids
            if mid in active_masters
        ]
        return sorted(result, key=lambda x: x["name"])

    async def get_free_dates(self, master_id: int, now: datetime) -> list[str]:
        slots_raw = await self._get("slots")
        now_str = now.strftime(DT_FMT)
        dates = {
            s["start_at"][:10]
            for _, s in _iter_items(slots_raw)
            if s.get("master_id") == master_id
            and s.get("is_booked") == 0
            and s.get("start_at", "") > now_str
        }
        return sorted(dates)

    async def get_free_times(self, master_id: int, date: str, now: datetime) -> list[dict]:
        slots_raw = await self._get("slots")
        now_str = now.strftime(DT_FMT)
        times = []
        for key, s in _iter_items(slots_raw):
            if s.get("master_id") == master_id and s.get("is_booked") == 0:
                start_at = s.get("start_at", "")
                if start_at.startswith(date) and start_at > now_str:
                    times.append({"id": key, "t": start_at[11:16], "start_at": start_at})
        return sorted(times, key=lambda x: x["start_at"])

    async def get_slot(self, slot_key: str) -> Optional[dict]:
        res = await self._get(f"slots/{slot_key}")
        return res if isinstance(res, dict) else None

    async def get_service_groups(self) -> list[str]:
        services_raw = await self._get("services")
        items = sorted(
            _iter_items(services_raw),
            key=lambda kv: kv[1].get("id", 0),
        )
        groups = []
        seen = set()
        for _, s in items:
            if s.get("is_active") == 1:
                g = s.get("group_name")
                if g and g not in seen:
                    seen.add(g)
                    groups.append(g)
        return groups

    async def get_services_by_group(self, group_name: str) -> list[dict]:
        services_raw = await self._get("services")
        res = []
        for key, s in _iter_items(services_raw):
            if s.get("is_active") == 1 and s.get("group_name") == group_name:
                res.append({"key": key, **s})
        return sorted(res, key=lambda x: x.get("id", 0))

    async def get_service(self, service_key: str) -> Optional[dict]:
        res = await self._get(f"services/{service_key}")
        return res if isinstance(res, dict) else None

    async def get_master(self, master_id: int) -> Optional[dict]:
        masters_raw = await self._get("masters")
        for _, m in _iter_items(masters_raw):
            if m.get("id") == master_id:
                return m
        return None

    async def book(
        self,
        *,
        tg_id: int,
        name: str,
        phone: str,
        master_id: int,
        service_key: str,
        slot_key: str,
        now: datetime,
    ) -> Optional[str]:
        async with self._lock:
            slot = await self.get_slot(slot_key)
            if not slot or slot.get("is_booked") == 1:
                return None

            await self._patch(f"slots/{slot_key}", {"is_booked": 1})
            start_at = slot["start_at"]
            stamp = now.strftime(TS_FMT)

            client_key = f"tg_{tg_id}"
            await self._put(
                f"clients/{client_key}",
                {"tg_id": tg_id, "name": name, "phone": phone, "created_at": stamp},
            )

            appt_data = {
                "client_id": client_key,
                "tg_id": tg_id,
                "master_id": master_id,
                "service_id": service_key,
                "slot_id": slot_key,
                "start_at": start_at,
                "status": "booked",
                "reminder_sent": 0,
                "created_at": stamp,
            }
            return await self._post("appointments", appt_data)

    async def get_appointment(self, appt_key: str) -> Optional[dict]:
        appt = await self._get(f"appointments/{appt_key}")
        if not isinstance(appt, dict):
            return None

        master = await self.get_master(appt.get("master_id"))
        service = await self.get_service(str(appt.get("service_id", "")))
        client = await self._get(f"clients/{appt.get('client_id')}")
        if not isinstance(client, dict):
            client = {}

        return {
            "id": appt_key,
            "tg_id": appt.get("tg_id"),
            "start_at": appt.get("start_at"),
            "status": appt.get("status"),
            "created_at": appt.get("created_at"),
            "slot_id": appt.get("slot_id"),
            "master_name": master.get("name") if master else "Специалист",
            "service_name": service.get("category") if service else "Услуга",
            "client_name": client.get("name", "Клиент"),
            "client_phone": client.get("phone", ""),
        }

    async def get_active_appointments(self, tg_id: int, now: datetime) -> list[dict]:
        appts_raw = await self._get("appointments")
        now_str = now.strftime(DT_FMT)
        res = []
        for key, a in _iter_items(appts_raw):
            if (
                a.get("tg_id") == tg_id
                and a.get("status") in ("booked", "confirmed")
                and a.get("start_at", "") > now_str
            ):
                full_appt = await self.get_appointment(key)
                if full_appt:
                    res.append(full_appt)
        return sorted(res, key=lambda x: x["start_at"])

    async def cancel(self, appt_key: str, tg_id: int) -> Optional[dict]:
        async with self._lock:
            appt = await self._get(f"appointments/{appt_key}")
            if (
                not isinstance(appt, dict)
                or appt.get("tg_id") != tg_id
                or appt.get("status") not in ("booked", "confirmed")
            ):
                return None

            await self._patch(f"appointments/{appt_key}", {"status": "cancelled"})
            slot_key = appt.get("slot_id")
            if slot_key:
                await self._patch(f"slots/{slot_key}", {"is_booked": 0})

        return await self.get_appointment(appt_key)

    async def confirm(self, appt_key: str, tg_id: int) -> bool:
        appt = await self._get(f"appointments/{appt_key}")
        if (
            not isinstance(appt, dict)
            or appt.get("tg_id") != tg_id
            or appt.get("status") not in ("booked", "confirmed")
        ):
            return False
        await self._patch(f"appointments/{appt_key}", {"status": "confirmed"})
        return True

    async def get_due_reminders(self, now: datetime, horizon: datetime) -> list[dict]:
        appts_raw = await self._get("appointments")
        now_str = now.strftime(DT_FMT)
        horizon_str = horizon.strftime(DT_FMT)
        res = []
        for key, a in _iter_items(appts_raw):
            if a.get("status") == "booked" and a.get("reminder_sent", 0) == 0:
                start_at = a.get("start_at", "")
                if now_str < start_at <= horizon_str:
                    master = await self.get_master(a.get("master_id"))
                    res.append(
                        {
                            "id": key,
                            "tg_id": a.get("tg_id"),
                            "start_at": start_at,
                            "created_at": a.get("created_at"),
                            "master_name": master.get("name") if master else "Специалист",
                        }
                    )
        return res

    async def mark_reminded(self, appt_key: str) -> None:
        await self._patch(f"appointments/{appt_key}", {"reminder_sent": 1})

    async def complete_past(self, now: datetime) -> None:
        appts_raw = await self._get("appointments")
        past_limit = (now - timedelta(hours=1)).strftime(DT_FMT)
        updates = {}
        for key, a in _iter_items(appts_raw):
            if a.get("status") in ("booked", "confirmed") and a.get("start_at", "") < past_limit:
                updates[f"{key}/status"] = "completed"
        if updates:
            await self._patch("appointments", updates)


# --------------------------------------------------------------------------- #
# Telegram Роутер и Хендлеры
# --------------------------------------------------------------------------- #

router = Router()


class MenuCb(CallbackData, prefix="menu"):
    action: str


class GrpCb(CallbackData, prefix="grp"):
    idx: int


class CatCb(CallbackData, prefix="cat"):
    service_key: str


class MasterCb(CallbackData, prefix="mst"):
    master_id: int


class DateCb(CallbackData, prefix="dt"):
    date: str


class TimeCb(CallbackData, prefix="tm"):
    slot_id: str


class ConfirmCb(CallbackData, prefix="cf"):
    ok: bool


class MyCb(CallbackData, prefix="my"):
    action: str
    appt_id: str = "0"


class RemCb(CallbackData, prefix="rem"):
    action: str
    appt_id: str


class BookingStates(StatesGroup):
    group = State()
    service = State()
    master = State()
    date = State()
    time = State()
    name = State()
    phone = State()
    confirm = State()


def fmt_dt(start_at: str) -> str:
    dt = datetime.strptime(start_at, DT_FMT)
    return f"{dt:%d.%m} в {dt:%H:%M}"


def fmt_date_btn(date: str) -> str:
    dt = datetime.strptime(date, "%Y-%m-%d")
    return f"{WEEKDAYS[dt.weekday()]} {dt:%d.%m}"


def normalize_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not 11 <= len(digits) <= 15:
        return None
    return "+" + digits


def cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="❌ Отмена", callback_data=MenuCb(action="home").pack())


def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📅 Записаться на услуги", callback_data=MenuCb(action="book"))
    b.button(text="👤 Мои записи", callback_data=MenuCb(action="my"))
    b.button(text="📞 Контакты", callback_data=MenuCb(action="contacts"))
    b.adjust(1)
    return b.as_markup()


def back_to_menu_kb(extra_whatsapp: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if extra_whatsapp:
        b.button(text=WA_1_TEXT, url=WA_1_URL)
        b.button(text=WA_2_TEXT, url=WA_2_URL)
    b.button(text="◀️ В главное меню", callback_data=MenuCb(action="home"))
    b.adjust(1)
    return b.as_markup()


WELCOME_TEXT = (
    "✂️ <b>Комплекс «Эстет»</b>\n\n"
    "Барбершоп • Спа • Подология • Массаж • Косметология\n\n"
    "Добро пожаловать! Выберите нужное действие:"
)


def contacts_text(cfg: Config) -> str:
    return (
        "📞 <b>Контакты комплекса «Эстет»</b>\n\n"
        "WhatsApp / телефоны:\n"
        "• +7 747 177-77-11\n"
        "• +7 775 219-14-79\n\n"
        f"📍 Адрес: {html.escape(cfg.address)}\n"
        f"🕙 Режим работы: ежедневно {cfg.work_start:02d}:00–{cfg.work_end:02d}:00"
    )


async def edit(cb: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    if not isinstance(cb.message, Message):
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await cb.message.answer(text, reply_markup=kb)


async def notify_admins(bot: Bot, cfg: Config, text: str) -> None:
    for chat_id in cfg.admin_chat_ids:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            logger.exception("Не удалось отправить уведомление в чат %s", chat_id)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.callback_query(MenuCb.filter(F.action == "home"))
async def cb_home(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit(cb, WELCOME_TEXT, main_menu_kb())
    await cb.answer()


@router.callback_query(MenuCb.filter(F.action == "contacts"))
async def cb_contacts(cb: CallbackQuery, cfg: Config) -> None:
    await edit(cb, contacts_text(cfg), back_to_menu_kb(extra_whatsapp=True))
    await cb.answer()


@router.callback_query(MenuCb.filter(F.action == "book"))
async def cb_book(cb: CallbackQuery, state: FSMContext, db: FirebaseDatabase) -> None:
    await state.clear()
    groups = await db.get_service_groups()
    b = InlineKeyboardBuilder()
    for i, g in enumerate(groups):
        b.button(text=g, callback_data=GrpCb(idx=i))
    b.adjust(1)
    b.row(cancel_button())
    await state.update_data(groups=groups)
    await state.set_state(BookingStates.group)
    await edit(cb, "📂 <b>Выберите направление:</b>", b.as_markup())
    await cb.answer()


@router.callback_query(GrpCb.filter(), BookingStates.group)
async def cb_group(cb: CallbackQuery, callback_data: GrpCb, state: FSMContext, db: FirebaseDatabase) -> None:
    data = await state.get_data()
    groups: list[str] = data.get("groups", [])
    if not 0 <= callback_data.idx < len(groups):
        await cb.answer("Раздел не найден", show_alert=True)
        return

    group_name = groups[callback_data.idx]
    services = await db.get_services_by_group(group_name)
    b = InlineKeyboardBuilder()
    for s in services:
        b.button(text=f"{s['category']} — {s['price']} ₸", callback_data=CatCb(service_key=s["key"]))
    b.adjust(1)
    b.row(InlineKeyboardButton(text="◀️ Назад к разделам", callback_data=MenuCb(action="book").pack()))
    b.row(cancel_button())
    await state.set_state(BookingStates.service)
    await edit(cb, f"<b>{html.escape(group_name)}</b>\n\nВыберите услугу:", b.as_markup())
    await cb.answer()


@router.callback_query(CatCb.filter(), BookingStates.service)
async def cb_service(
    cb: CallbackQuery, callback_data: CatCb, state: FSMContext, db: FirebaseDatabase, cfg: Config
) -> None:
    service = await db.get_service(callback_data.service_key)
    if service is None:
        await cb.answer("Услуга не найдена", show_alert=True)
        return
    masters = await db.get_masters_with_free_slots(now_local(cfg))
    if not masters:
        await state.clear()
        await edit(
            cb,
            "😔 Свободных окон пока нет. Попробуйте позже или напишите нам в WhatsApp.",
            back_to_menu_kb(extra_whatsapp=True),
        )
        await cb.answer()
        return

    await state.update_data(service_key=callback_data.service_key)
    b = InlineKeyboardBuilder()
    for m in masters:
        b.button(text=f"👤 {m['name']}", callback_data=MasterCb(master_id=m["id"]))
    b.adjust(1)
    b.row(cancel_button())
    await state.set_state(BookingStates.master)
    await edit(
        cb,
        f"Услуга: <b>{html.escape(service['category'])}</b> ({service['price']} ₸)\n\n<b>Выберите специалиста:</b>",
        b.as_markup(),
    )
    await cb.answer()


@router.callback_query(MasterCb.filter(), BookingStates.master)
async def cb_master(
    cb: CallbackQuery, callback_data: MasterCb, state: FSMContext, db: FirebaseDatabase, cfg: Config
) -> None:
    master = await db.get_master(callback_data.master_id)
    dates = await db.get_free_dates(callback_data.master_id, now_local(cfg))
    if master is None or not dates:
        await cb.answer("У этого специалиста нет свободных дат", show_alert=True)
        return

    await state.update_data(master_id=master["id"])
    b = InlineKeyboardBuilder()
    for d in dates:
        b.button(text=fmt_date_btn(d), callback_data=DateCb(date=d))
    b.adjust(3)
    b.row(cancel_button())
    await state.set_state(BookingStates.date)
    await edit(cb, f"Специалист: <b>{html.escape(master['name'])}</b>\n\n<b>Выберите дату:</b>", b.as_markup())
    await cb.answer()


@router.callback_query(DateCb.filter(), BookingStates.date)
async def cb_date(
    cb: CallbackQuery, callback_data: DateCb, state: FSMContext, db: FirebaseDatabase, cfg: Config
) -> None:
    data = await state.get_data()
    times = await db.get_free_times(data["master_id"], callback_data.date, now_local(cfg))
    if not times:
        await cb.answer("На эту дату свободного времени нет", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for t in times:
        b.button(text=t["t"], callback_data=TimeCb(slot_id=t["id"]))
    b.adjust(4)
    b.row(cancel_button())
    await state.set_state(BookingStates.time)
    await edit(cb, f"Дата: <b>{fmt_date_btn(callback_data.date)}</b>\n\n<b>Выберите время:</b>", b.as_markup())
    await cb.answer()


@router.callback_query(TimeCb.filter(), BookingStates.time)
async def cb_time(cb: CallbackQuery, callback_data: TimeCb, state: FSMContext, db: FirebaseDatabase) -> None:
    slot = await db.get_slot(callback_data.slot_id)
    if slot is None:
        await cb.answer("Время недоступно", show_alert=True)
        return
    await state.update_data(slot_id=callback_data.slot_id)
    await state.set_state(BookingStates.name)
    kb = InlineKeyboardMarkup(inline_keyboard=[[cancel_button()]])
    await edit(cb, f"Время: <b>{fmt_dt(slot['start_at'])}</b>\n\n✍️ Введите ваше <b>имя</b>:", kb)
    await cb.answer()


@router.message(BookingStates.name, F.text)
async def msg_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 50:
        await message.answer("Пожалуйста, введите имя (от 2 до 50 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(BookingStates.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "📞 Отправьте номер телефона кнопкой ниже или напишите его текстом "
        "(например, +7 777 123-45-67).\n\nОтмена — /cancel",
        reply_markup=kb,
    )


async def _finish_phone(message: Message, state: FSMContext, db: FirebaseDatabase, phone: str) -> None:
    await state.update_data(phone=phone)
    data = await state.get_data()
    service = await db.get_service(data["service_key"])
    master = await db.get_master(data["master_id"])
    slot = await db.get_slot(data["slot_id"])
    if not (service and master and slot):
        await state.clear()
        await message.answer("Не удалось собрать данные записи. Начните заново.", reply_markup=ReplyKeyboardRemove())
        await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())
        return

    await message.answer(f"📱 Номер принят: {phone}", reply_markup=ReplyKeyboardRemove())
    text = (
        "📝 <b>Проверьте данные записи:</b>\n\n"
        f"Услуга: {html.escape(service['category'])} ({service['price']} ₸)\n"
        f"Специалист: {html.escape(master['name'])}\n"
        f"Дата и время: {fmt_dt(slot['start_at'])}\n"
        f"Имя: {html.escape(data['name'])}\n"
        f"Телефон: {html.escape(phone)}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить запись", callback_data=ConfirmCb(ok=True))
    b.button(text="❌ Отмена", callback_data=ConfirmCb(ok=False))
    b.adjust(1)
    await state.set_state(BookingStates.confirm)
    await message.answer(text, reply_markup=b.as_markup())


@router.message(BookingStates.phone, F.contact)
async def msg_phone_contact(message: Message, state: FSMContext, db: FirebaseDatabase) -> None:
    assert message.contact is not None
    phone = normalize_phone(message.contact.phone_number)
    if phone is None:
        await message.answer("Не удалось распознать номер. Введите его текстом.")
        return
    await _finish_phone(message, state, db, phone)


@router.message(BookingStates.phone, F.text)
async def msg_phone_text(message: Message, state: FSMContext, db: FirebaseDatabase) -> None:
    phone = normalize_phone(message.text or "")
    if phone is None:
        await message.answer("Номер выглядит некорректно. Пример: +7 777 123-45-67")
        return
    await _finish_phone(message, state, db, phone)


@router.callback_query(ConfirmCb.filter(F.ok == False), BookingStates.confirm)
async def cb_confirm_no(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit(cb, "Запись отменена.\n\n" + WELCOME_TEXT, main_menu_kb())
    await cb.answer()


@router.callback_query(ConfirmCb.filter(F.ok == True), BookingStates.confirm)
async def cb_confirm_yes(cb: CallbackQuery, state: FSMContext, db: FirebaseDatabase, bot: Bot, cfg: Config) -> None:
    data = await state.get_data()
    await state.clear()
    appt_key = await db.book(
        tg_id=cb.from_user.id,
        name=data["name"],
        phone=data["phone"],
        master_id=data["master_id"],
        service_key=data["service_key"],
        slot_key=data["slot_id"],
        now=now_local(cfg),
    )
    if not appt_key:
        await edit(
            cb,
            "😔 К сожалению, это время только что заняли. Выберите другое.",
            main_menu_kb(),
        )
        await cb.answer()
        return

    appt = await db.get_appointment(appt_key)
    assert appt is not None
    await edit(
        cb,
        "🎉 <b>Вы записаны!</b>\n\n"
        f"Услуга: {html.escape(appt['service_name'])}\n"
        f"Специалист: {html.escape(appt['master_name'])}\n"
        f"Когда: {fmt_dt(appt['start_at'])}\n\n"
        "За 2 часа до визита мы пришлём напоминание.",
        main_menu_kb(),
    )
    await cb.answer("Запись создана")
    await notify_admins(
        bot,
        cfg,
        f"🆕 Новая запись: {fmt_dt(appt['start_at'])}, мастер {appt['master_name']}\n"
        f"Услуга: {appt['service_name']}\n"
        f"Клиент: {appt['client_name']}, {appt['client_phone']}",
    )


async def render_my_appointments(cb: CallbackQuery, db: FirebaseDatabase, cfg: Config) -> None:
    appts = await db.get_active_appointments(cb.from_user.id, now_local(cfg))
    if not appts:
        await edit(cb, "У вас нет предстоящих записей.", back_to_menu_kb())
        return
    lines = ["👤 <b>Ваши записи:</b>\n"]
    b = InlineKeyboardBuilder()
    for a in appts:
        lines.append(
            f"• {fmt_dt(a['start_at'])} — {html.escape(a['master_name'])}, "
            f"{html.escape(a['service_name'])} ({STATUS_LABELS[a['status']]})"
        )
        b.button(text=f"❌ Отменить {fmt_dt(a['start_at'])}", callback_data=MyCb(action="ask", appt_id=a["id"]))
    b.button(text="◀️ В главное меню", callback_data=MenuCb(action="home"))
    b.adjust(1)
    await edit(cb, "\n".join(lines), b.as_markup())


@router.callback_query(MenuCb.filter(F.action == "my"))
async def cb_my(cb: CallbackQuery, state: FSMContext, db: FirebaseDatabase, cfg: Config) -> None:
    await state.clear()
    await render_my_appointments(cb, db, cfg)
    await cb.answer()


@router.callback_query(MyCb.filter(F.action == "back"))
async def cb_my_back(cb: CallbackQuery, db: FirebaseDatabase, cfg: Config) -> None:
    await render_my_appointments(cb, db, cfg)
    await cb.answer()


@router.callback_query(MyCb.filter(F.action == "ask"))
async def cb_my_ask(cb: CallbackQuery, callback_data: MyCb, db: FirebaseDatabase) -> None:
    appt = await db.get_appointment(callback_data.appt_id)
    if appt is None or appt["tg_id"] != cb.from_user.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="Да, отменить", callback_data=MyCb(action="yes", appt_id=appt["id"]))
    b.button(text="Нет, оставить", callback_data=MyCb(action="back"))
    b.adjust(1)
    await edit(
        cb,
        f"Отменить запись на <b>{fmt_dt(appt['start_at'])}</b> к специалисту {html.escape(appt['master_name'])}?",
        b.as_markup(),
    )
    await cb.answer()


@router.callback_query(MyCb.filter(F.action == "yes"))
async def cb_my_cancel(cb: CallbackQuery, callback_data: MyCb, db: FirebaseDatabase, bot: Bot, cfg: Config) -> None:
    row = await db.cancel(callback_data.appt_id, cb.from_user.id)
    if row is None:
        await cb.answer("Запись уже отменена или не найдена", show_alert=True)
        await render_my_appointments(cb, db, cfg)
        return
    await cb.answer("Запись отменена")
    await render_my_appointments(cb, db, cfg)
    await notify_admins(bot, cfg, f"Освободилось окошко на {fmt_dt(row['start_at'])} у мастера {row['master_name']}!")


@router.callback_query(RemCb.filter(F.action == "yes"))
async def cb_reminder_yes(cb: CallbackQuery, callback_data: RemCb, db: FirebaseDatabase) -> None:
    appt = await db.get_appointment(callback_data.appt_id)
    if appt is None or appt["tg_id"] != cb.from_user.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if appt["status"] == "cancelled":
        await edit(cb, "Эта запись уже отменена.", main_menu_kb())
        await cb.answer()
        return
    await db.confirm(appt["id"], cb.from_user.id)
    await edit(
        cb,
        f"✅ Спасибо! Ждём вас в {appt['start_at'][11:16]} у специалиста {html.escape(appt['master_name'])}.",
    )
    await cb.answer("Визит подтверждён")


@router.callback_query(RemCb.filter(F.action == "no"))
async def cb_reminder_no(cb: CallbackQuery, callback_data: RemCb, db: FirebaseDatabase, bot: Bot, cfg: Config) -> None:
    row = await db.cancel(callback_data.appt_id, cb.from_user.id)
    if row is None:
        await edit(cb, "Эта запись уже отменена или не найдена.", main_menu_kb())
        await cb.answer()
        return
    await edit(cb, "Запись отменена. Будем рады видеть вас снова!", main_menu_kb())
    await cb.answer("Запись отменена")
    await notify_admins(bot, cfg, f"Освободилось окошко на {row['start_at'][11:16]} у мастера {row['master_name']}!")


@router.callback_query(F.data.regexp(r"^(grp|cat|mst|dt|tm|cf):"))
async def cb_stale(cb: CallbackQuery) -> None:
    await cb.answer("Эта кнопка устарела. Нажмите /start и начните заново.", show_alert=True)


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    logger.error("Необработанная ошибка в хендлере: %s", event.exception, exc_info=event.exception)
    return True


async def send_reminders(bot: Bot, db: FirebaseDatabase, cfg: Config) -> None:
    now = now_local(cfg)
    horizon = now + timedelta(hours=cfg.reminder_hours)
    try:
        await db.complete_past(now)
        due = await db.get_due_reminders(now, horizon)
    except Exception:
        logger.exception("Ошибка при выборке записей для напоминаний")
        return

    for row in due:
        start = datetime.strptime(row["start_at"], DT_FMT)
        created = datetime.strptime(row["created_at"], TS_FMT)
        if created > start - timedelta(hours=cfg.reminder_hours):
            await db.mark_reminded(row["id"])
            continue

        b = InlineKeyboardBuilder()
        b.button(text="✅ Буду", callback_data=RemCb(action="yes", appt_id=row["id"]))
        b.button(text="❌ Отменяю", callback_data=RemCb(action="no", appt_id=row["id"]))
        b.adjust(2)
        text = (
            f"⏰ Напоминаем, вы записаны сегодня в {start:%H:%M} к специалисту "
            f"{html.escape(row['master_name'])}.\nПодтвердите визит:"
        )
        try:
            await bot.send_message(row["tg_id"], text, reply_markup=b.as_markup())
        except TelegramForbiddenError:
            logger.warning("Клиент %s заблокировал бота, напоминание пропущено", row["tg_id"])
        except Exception:
            logger.exception("Не удалось отправить напоминание по записи %s", row["id"])
            continue
        await db.mark_reminded(row["id"])


async def refresh_slots(db: FirebaseDatabase, cfg: Config) -> None:
    try:
        await db.ensure_slots(cfg)
    except Exception:
        logger.exception("Ошибка при обновлении слотов")


# --------------------------------------------------------------------------- #
# Flask приложение и запуск веб-сервера
# --------------------------------------------------------------------------- #

app = Flask(__name__)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

cfg = load_config()
db = FirebaseDatabase(cfg.firebase_url)
bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)
dp["db"] = db
dp["cfg"] = cfg


@app.route("/")
def index():
    return "Bot is running via Webhook!", 200


@app.route(f"/webhook/{cfg.bot_token}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_data = request.get_json()
        update = Update.model_validate(json_data, context={"bot": bot})
        loop.run_until_complete(dp.feed_update(bot, update))
        return "", 200
    else:
        abort(403)


async def startup():
    await db.init_schema()
    await db.ensure_slots(cfg)
    
    if cfg.webhook_url:
        await bot.set_webhook(cfg.webhook_url, drop_pending_updates=True)
        logger.info(f"Webhook установлен: {cfg.webhook_url}")
    else:
        logger.warning("RENDER_EXTERNAL_URL не найдена, webhook не установлен автоматически!")

    scheduler = AsyncIOScheduler(timezone=cfg.tz)
    scheduler.add_job(
        send_reminders, "interval", minutes=1, args=[bot, db, cfg],
        id="reminders", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        refresh_slots, "cron", hour=0, minute=5, args=[db, cfg],
        id="slots", max_instances=1, coalesce=True,
    )
    scheduler.start()


if __name__ == "__main__":
    loop.run_until_complete(startup())
    app.run(host="0.0.0.0", port=cfg.port)
