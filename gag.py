# ============================================================
#  VELTRIX — Telegram Bot (FINAL WEBHOOK VERSION)
#  Готов для деплоя на Render.com
# ============================================================

import asyncio
import logging
import os
import shutil
import re
from datetime import datetime, timedelta
from aiohttp import web

import aiosqlite
import phonenumbers
from phonenumbers import carrier
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
#  НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "9087734099:AAF_jxv23SZBhTiItLh_pMkKVTSlhJphSgE")
ADMIN_IDS = [8318031160]
ADMIN_CHAT_ID = -5560956557
BACKUP_CHAT_ID = -5306713812
BACKUP_TIME = "03:00"
DB_PATH = "wb.db"

# Render сам подставит свой URL в эту переменную
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://твой-сайт.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

PRICE_READY = 15.00
PRICE_UNREG = 12.00
PAYOUT_READY = 12.50
PAYOUT_UNREG = 9.00
PAYOUT_OZON = 7.00
PAYOUT_BEELINE = 11.00
PRICE_BEELINE = 14.00  # Твоя маржа 3$
PRICE_OZON = 10.00
REF_BONUS = 0.50
MIN_WITHDRAW = 5.00
WITHDRAW_TIMEOUT_MIN = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("veltrix")

# ============================================================
#  БД
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY, username TEXT, role TEXT DEFAULT 'user',
    balance REAL DEFAULT 0, ref_code TEXT, referred_by INTEGER,
    is_banned INTEGER DEFAULT 0, vbi_access INTEGER DEFAULT 1, created_at TEXT
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, type TEXT, status TEXT,
    platform TEXT DEFAULT 'wb', operator TEXT, wb_phone TEXT, wb_password TEXT,
    code TEXT, pin TEXT, taker_id INTEGER, verdict_comment TEXT, verdict_file_id TEXT,
    price REAL, payout REAL, created_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, method TEXT,
    status TEXT, receipt TEXT, reason TEXT, created_at TEXT, deadline TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_id INTEGER,
    bonus_paid REAL DEFAULT 0, submission_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, type TEXT,
    ref_id INTEGER, note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT DEFAULT 'open',
    created_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, from_user INTEGER,
    text TEXT, file_id TEXT, message_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        async with db.execute("PRAGMA table_info(submissions)") as cur:
            cols = [r[1] for r in await cur.fetchall()]
        if "platform" not in cols: await db.execute("ALTER TABLE submissions ADD COLUMN platform TEXT DEFAULT 'wb'")
        if "operator" not in cols: await db.execute("ALTER TABLE submissions ADD COLUMN operator TEXT")
        
        async with db.execute("PRAGMA table_info(ticket_messages)") as cur:
            tcols = [r[1] for r in await cur.fetchall()]
        if "message_id" not in tcols: await db.execute("ALTER TABLE ticket_messages ADD COLUMN message_id INTEGER")
        
        # Инициализация цен
        for key, val in [
            ("price_ready", "15.00"), ("payout_ready", "12.50"), 
            ("price_unreg", "12.00"), ("payout_unreg", "9.00"), 
            ("price_ozon", "10.00"), ("payout_ozon", "7.00"),
            ("price_beeline", "14.00"), ("payout_beeline", "11.00")
        ]:
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))
        await db.commit()

def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# ============================================================
#  ХЕЛПЕРЫ
# ============================================================

async def get_user(tg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)) as cur:
            return await cur.fetchone()

async def create_user(tg_id, username, referred_by):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (tg_id, username, role, balance, ref_code, referred_by, created_at) VALUES (?, ?, 'user', 0, ?, ?, ?)",
            (tg_id, username, f"ref_{tg_id}", referred_by, now()))
        await db.commit()

async def set_role(tg_id, role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET role=? WHERE tg_id=?", (role, tg_id))
        await db.commit()

async def get_setting(key):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()

async def add_balance(tg_id, amount, tx_type, ref_id=None, note=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id=?", (amount, tg_id))
        await db.execute(
            "INSERT INTO transactions (user_id, amount, type, ref_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tg_id, amount, tx_type, ref_id, note, now()))
        await db.commit()

def get_operator(phone: str) -> str:
    clean = "".join(filter(str.isdigit, phone))
    if len(clean) < 4: return "Неизвестно"
    code = clean[1:4] if (clean.startswith("7") or clean.startswith("8")) else clean[:3]
    
    mts = ["910","911","912","913","914","915","916","917","918","919","980","981","982","983","984","985","986","987","988","989"]
    megafon = ["920","921","922","923","924","925","926","927","928","929","930","931","932","933","934","935","936","937","938","939"]
    beeline = ["903","905","906","909","960","961","962","963","964","965","966","967","968","969"]
    t2 = ["901","902","904","908","950","951","952","953","954","955","956","957","958","959","977","978","991","992","993","994","995","996","999"]
    
    if code in mts: return "МТС"
    if code in megafon: return "МегаФон"
    if code in beeline: return "Билайн"
    if code in t2: return "Т2"
    
    try:
        parsed = phonenumbers.parse(phone, "RU")
        op_name = carrier.name_for_number(parsed, "ru")
        if op_name:
            op_lower = op_name.lower()
            if "mts" in op_lower or "мтс" in op_lower: return "МТС"
            if "megafon" in op_lower or "мегафон" in op_lower: return "МегаФон"
            if "beeline" in op_lower or "билайн" in op_lower: return "Билайн"
            if "tele2" in op_lower or "т2" in op_lower: return "Т2"
    except: pass
    return "Неизвестно"
    
async def get_prices():
    return {
        "price_ready": float(await get_setting("price_ready") or "15.00"),
        "payout_ready": float(await get_setting("payout_ready") or "12.50"),
        "price_unreg": float(await get_setting("price_unreg") or "12.00"),
        "payout_unreg": float(await get_setting("payout_unreg") or "9.00"),
        "price_ozon": float(await get_setting("price_ozon") or "10.00"),
        "payout_ozon": float(await get_setting("payout_ozon") or "7.00"),
        "price_beeline": float(await get_setting("price_beeline") or "14.00"),
        "payout_beeline": float(await get_setting("payout_beeline") or "11.00"),
    }

# ============================================================
#  БОТ
# ============================================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

async def get_admin_chat():
    if ADMIN_CHAT_ID: return ADMIN_CHAT_ID
    val = await get_setting("admin_chat")
    return int(val) if val else None

async def notify_admin(text, kb=None):
    chat_id = await get_admin_chat()
    if not chat_id: return
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        log.warning(f"admin notify: {e}")

async def notify_user(tg_id, text, kb=None):
    try:
        await bot.send_message(tg_id, text, reply_markup=kb)
    except Exception as e:
        log.warning(f"user notify ({tg_id}): {e}")

# ============================================================
#  FSM
# ============================================================

class SubmitSG(StatesGroup):
    choose_type = State()
    phone = State()
    password = State()

class WithdrawSG(StatesGroup):
    amount = State()

class VerdictSG(StatesGroup):
    comment = State()
    file = State()

class TicketSG(StatesGroup):
    text = State()

class OwnerSG(StatesGroup):
    receipt = State()
    reason = State()
    balance_amount = State()
    ban_reason = State()
    ticket_reply = State()

class BroadcastSG(StatesGroup):
    waiting_text = State()

class PriceSG(StatesGroup):
    waiting_value = State()

# ============================================================
#  КЛАВИАТУРЫ
# ============================================================

def kb_main_user():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сдать номер", callback_data="user:submit", icon_custom_emoji_id="5255819258994125698")],
        [InlineKeyboardButton(text="Профиль", callback_data="user:profile", icon_custom_emoji_id="5319289070218674433"),
         InlineKeyboardButton(text="Рефералка", callback_data="user:ref", icon_custom_emoji_id="5233316306127709823")],
        [InlineKeyboardButton(text="Вывести", callback_data="user:withdraw", icon_custom_emoji_id="5231211574584178182"),
         InlineKeyboardButton(text="Поддержка", callback_data="user:support", icon_custom_emoji_id="5318762236645246095")],
    ])

def kb_main_owner():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Доход и Статистика", callback_data="owner:income", icon_custom_emoji_id="5235637061641332940")],
        [InlineKeyboardButton(text="Заявки на вывод", callback_data="owner:withdrawals", icon_custom_emoji_id="5231211574584178182")],
        [InlineKeyboardButton(text="Пользователи", callback_data="owner:users", icon_custom_emoji_id="5213126326294179859"),
         InlineKeyboardButton(text="Вбивы", callback_data="owner:vbi", icon_custom_emoji_id="5233709798146471128")],
        [InlineKeyboardButton(text="Управление приёмом", callback_data="owner:accept", icon_custom_emoji_id="5289640945334120513")],
        [InlineKeyboardButton(text="Редактор цен", callback_data="owner:prices", icon_custom_emoji_id="5231211574584178182"),
         InlineKeyboardButton(text="Рассылка", callback_data="owner:broadcast", icon_custom_emoji_id="5451620684805481209")],
        [InlineKeyboardButton(text="Тикеты", callback_data="owner:tickets", icon_custom_emoji_id="5406921525636916650")],
    ])

def kb_main_vbi():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Взять из очереди", callback_data="vbi:queue_menu", icon_custom_emoji_id="5406921525636916650")],
        [InlineKeyboardButton(text="Очередь (статистика)", callback_data="vbi:queue_stats", icon_custom_emoji_id="5256200407276874123")],
        [InlineKeyboardButton(text="Мои заявки", callback_data="vbi:mine", icon_custom_emoji_id="5359797333443770179")],
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="back:main", icon_custom_emoji_id="5359361785105249383")]
    ])

def kb_submit_type(wb_on, wb_unreg_on, oz_on, bl_on, prices):
    kb = []
    if wb_on:
        kb.append([InlineKeyboardButton(text=f"WB Готовый — {prices['payout_ready']:.2f}$", callback_data="submit:ready", icon_custom_emoji_id="5456184504169309058")])
    else:
        kb.append([InlineKeyboardButton(text="WB Готовый (ВЫКЛ)", callback_data="noop", icon_custom_emoji_id="5359361785105249383")])
        
    if wb_unreg_on:
        kb.append([InlineKeyboardButton(text=f"WB Нерег — {prices['payout_unreg']:.2f}$", callback_data="submit:unreg", icon_custom_emoji_id="5456184504169309058")])
    else:
        kb.append([InlineKeyboardButton(text="WB Нерег (ВЫКЛ)", callback_data="noop", icon_custom_emoji_id="5359361785105249383")])
        
    if oz_on:
        kb.append([InlineKeyboardButton(text=f"Ozon — {prices['payout_ozon']:.2f}$", callback_data="submit:ozon", icon_custom_emoji_id="5458594960074907382")])
    else:
        kb.append([InlineKeyboardButton(text="Ozon (ВЫКЛ)", callback_data="noop", icon_custom_emoji_id="5359361785105249383")])
        
    if bl_on:
        kb.append([InlineKeyboardButton(text=f"Билайн — {prices['payout_beeline']:.2f}$", callback_data="submit:beeline", icon_custom_emoji_id="5280919528908267119")])
    else:
        kb.append([InlineKeyboardButton(text="Билайн (ВЫКЛ)", callback_data="noop", icon_custom_emoji_id="5359361785105249383")])
        
    kb.append([InlineKeyboardButton(text="Назад", callback_data="back:main", icon_custom_emoji_id="5359361785105249383")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_withdraw_method():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="xRocket", callback_data="wd:xrocket", icon_custom_emoji_id="5415897719522744378"),
         InlineKeyboardButton(text="CryptoBot", callback_data="wd:cryptobot", icon_custom_emoji_id="5361914370068613491")],
        [InlineKeyboardButton(text="Назад", callback_data="back:main", icon_custom_emoji_id="5359361785105249383")]
    ])

def kb_vbi_work(sid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Запросить код", callback_data=f"vbi:code:{sid}", icon_custom_emoji_id="5231349962725419070")],
        [InlineKeyboardButton(text="Успех", callback_data=f"vbi:success:{sid}", icon_custom_emoji_id="5359828295863005340"),
         InlineKeyboardButton(text="Неудача", callback_data=f"vbi:fail:{sid}", icon_custom_emoji_id="5359361785105249383")],
        [InlineKeyboardButton(text="Вернуть в очередь", callback_data=f"vbi:return:{sid}", icon_custom_emoji_id="5359361785105249383")],
    ])

def kb_vbi_fail_reasons(sid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Неверный пароль", callback_data=f"vbi:fr:{sid}:Неверный пароль", icon_custom_emoji_id="5359361785105249383")],
        [InlineKeyboardButton(text="Аккаунт заблокирован", callback_data=f"vbi:fr:{sid}:Аккаунт заблокирован", icon_custom_emoji_id="5359361785105249383")],
        [InlineKeyboardButton(text="Нет кода", callback_data=f"vbi:fr:{sid}:Нет кода", icon_custom_emoji_id="5359361785105249383")],
        [InlineKeyboardButton(text="Свой текст", callback_data=f"vbi:fc:{sid}", icon_custom_emoji_id="5359797333443770179")],
        [InlineKeyboardButton(text="Прикрепить скрин", callback_data=f"vbi:ff:{sid}", icon_custom_emoji_id="5271936420125110212")],
    ])

def kb_admin_withdraw(wid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выплатить", callback_data=f"ow:pay:{wid}", icon_custom_emoji_id="5359828295863005340"),
         InlineKeyboardButton(text="Отклонить", callback_data=f"ow:rej:{wid}", icon_custom_emoji_id="5359361785105249383")],
        [InlineKeyboardButton(text="+30 мин", callback_data=f"ow:ext:{wid}", icon_custom_emoji_id="5233316306127709823")],
    ])

def kb_admin_vbi(tg_id, active):
    if active:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отключить доступ", callback_data=f"ov:off:{tg_id}", icon_custom_emoji_id="5359361785105249383")]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Включить доступ", callback_data=f"ov:on:{tg_id}", icon_custom_emoji_id="5359828295863005340")]])

def kb_admin_user(tg_id, banned):
    if banned:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Разбанить", callback_data=f"ou:unban:{tg_id}", icon_custom_emoji_id="5359828295863005340")]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить баланс", callback_data=f"ou:bal:{tg_id}", icon_custom_emoji_id="5231211574584178182"),
         InlineKeyboardButton(text="Забанить", callback_data=f"ou:ban:{tg_id}", icon_custom_emoji_id="5359361785105249383")]])

def kb_vbi_queue_types():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="WB Готовый", callback_data="vbi:take_type:wb_ready", icon_custom_emoji_id="5456184504169309058")],
        [InlineKeyboardButton(text="WB Нерег", callback_data="vbi:take_type:wb_unreg", icon_custom_emoji_id="5456184504169309058")],
        [InlineKeyboardButton(text="Ozon Нерег", callback_data="vbi:take_type:ozon", icon_custom_emoji_id="5458594960074907382")],
        [InlineKeyboardButton(text="Билайн", callback_data="vbi:take_type:beeline", icon_custom_emoji_id="5280919528908267119")],
        [InlineKeyboardButton(text="Назад", callback_data="back:main", icon_custom_emoji_id="5359361785105249383")]
    ])

# ============================================================
#  МЕНЮ И СТАРТ
# ============================================================

async def show_main_menu(event, edit=False):
    tg_id = event.from_user.id
    user = await get_user(tg_id)
    if not user: return
    
    role = user["role"]
    if role == "owner":
        text = "👑 <b>VELTRIX · Админ-панель</b>\n\nВыбери действие:"
        kb = kb_main_owner()
    elif role == "vbi":
        if not user["vbi_access"]:
            text = "⛔ Твой доступ отключён. Обратись к ТС."
            kb = None
        else:
            text = "🛠 <b>VELTRIX · Панель вбивера</b>\n\nВыбери действие:"
            kb = kb_main_vbi()
    elif user["is_banned"]:
        text = "🚫 Ты забанен. Если не согласен — напиши в поддержку."
        kb = None
    else:
        prices = await get_prices()
        text = (
            f'<tg-emoji emoji-id="5255819258994125698">👋</tg-emoji> <b>Добро пожаловать в VELTRIX!</b>\n\n'
            f'<tg-emoji emoji-id="5456184504169309058">🟣</tg-emoji> WB: {prices["payout_ready"]:.2f}$ / {prices["payout_unreg"]:.2f}$\n'
            f'<tg-emoji emoji-id="5458594960074907382">🔵</tg-emoji> Ozon: {prices["payout_ozon"]:.2f}$\n'
            f'<tg-emoji emoji-id="5280919528908267119">📱</tg-emoji> Билайн: {prices["payout_beeline"]:.2f}$\n\n'
            f'<tg-emoji emoji-id="5361948905900635660">👇</tg-emoji> Выбери действие:'
        )
        kb = kb_main_user()

    if edit and isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            try: await event.message.delete()
            except: pass
            
    target = event.message if isinstance(event, CallbackQuery) else event
    await target.answer(text, reply_markup=kb)

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    parts = msg.text.split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else None
    referred_by = None
    if payload and payload.startswith("ref_"):
        try:
            ref_id = int(payload[4:])
            if ref_id != msg.from_user.id: referred_by = ref_id
        except: pass

    user = await get_user(msg.from_user.id)
    if not user:
        await create_user(msg.from_user.id, msg.from_user.username, referred_by)
        if referred_by:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)", (referred_by, msg.from_user.id, now()))
                await db.commit()
            await notify_admin(f"🎁 Новый реферал: @{msg.from_user.username or msg.from_user.id}")
    await show_main_menu(msg)

@router.callback_query(F.data == "back:main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main_menu(cb, edit=True)
    await cb.answer()

@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer("Эта площадка сейчас выключена ТС.", show_alert=True)

# ============================================================
#  КОМАНДЫ ТС
# ============================================================

@router.message(Command("setadminchat"))
async def cmd_setadminchat(msg: Message):
    if msg.from_user.id in ADMIN_IDS and msg.chat.type != "private":
        await set_setting("admin_chat", str(msg.chat.id))
        await msg.answer(f"✅ Админ-чат подключён: {msg.chat.id}")

@router.message(Command("setbackupchat"))
async def cmd_setbackupchat(msg: Message):
    if msg.from_user.id in ADMIN_IDS and msg.chat.type != "private":
        await set_setting("backup_chat", str(msg.chat.id))
        await msg.answer(f"✅ Чат бэкапов подключён: {msg.chat.id}")

@router.message(Command("setrole"))
async def cmd_setrole(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    parts = msg.text.split()
    if len(parts) != 3 or parts[2] not in ("user", "vbi", "owner"):
        await msg.answer("Использование: /setrole <tg_id> <user|vbi|owner>"); return
    await set_role(int(parts[1]), parts[2])
    await msg.answer(f"✅ Роль {parts[1]} → {parts[2]}")

@router.message(Command("backup"))
async def cmd_backup(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        await msg.answer("💾 Делаю бэкап...")
        await make_backup(manual=True)

# ============================================================
#  ЮЗЕР — СДАЧА
# ============================================================

@router.callback_query(F.data == "user:submit")
async def cb_submit(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    if user["is_banned"]:
        await cb.answer("🚫 Ты забанен", show_alert=True); return
        
    wb_ready = (await get_setting("accept_wb") or "1") == "1"
    wb_unreg = (await get_setting("accept_wb_unreg") or "1") == "1"
    oz_on = (await get_setting("accept_ozon") or "1") == "1"
    bl_on = (await get_setting("accept_beeline") or "1") == "1"
    
    if not wb_ready and not wb_unreg and not oz_on and not bl_on:
        await cb.answer("Приём временно полностью приостановлен.", show_alert=True); return

    prices = await get_prices()
    
    await state.set_state(SubmitSG.choose_type)
    text = (
        f'<tg-emoji emoji-id="5255819258994125698">📦</tg-emoji> <b>Сдача номера</b>\n\n'
        f'<tg-emoji emoji-id="5359797333443770179">⚠️</tg-emoji> <i>Принимаются только: МТС, МегаФон, Т2, Билайн.</i>\n\n'
        f'Выбери площадку:'
    )
    await cb.message.edit_text(text, reply_markup=kb_submit_type(wb_ready, wb_unreg, oz_on, bl_on, prices))
    await cb.answer()

@router.callback_query(SubmitSG.choose_type, F.data.startswith("submit:"))
async def cb_submit_type(cb: CallbackQuery, state: FSMContext):
    t = cb.data.split(":")[1]
    if t == "ozon" and (await get_setting("accept_ozon") or "1") != "1":
        await cb.answer("Ozon выключен", show_alert=True); return
    if t == "ready" and (await get_setting("accept_wb") or "1") != "1":
        await cb.answer("WB Готовый выключен", show_alert=True); return
    if t == "unreg" and (await get_setting("accept_wb_unreg") or "1") != "1":
        await cb.answer("WB Нерег выключен", show_alert=True); return
    if t == "beeline" and (await get_setting("accept_beeline") or "1") != "1":
        await cb.answer("Билайн выключен", show_alert=True); return

    await state.update_data(type=t)
    await state.set_state(SubmitSG.phone)
    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5271936420125110212">📞</tg-emoji> Отправь номер в формате +7XXXXXXXXXX:'
    )
    await cb.answer()

@router.message(SubmitSG.phone)
async def submit_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    operator = get_operator(phone)
    if operator not in ("МТС", "МегаФон", "Билайн", "Т2"):
        await msg.answer(f"⚠️ Оператор <b>{operator}</b> не поддерживается.\nПринимаем: МТС, МегаФон, Т2, Билайн.\n\nПопробуй другой или /start")
        return
        
    data = await state.get_data()
    t = data["type"]
    
    await state.update_data(phone=phone, operator=operator)
    
    if t in ("unreg", "ozon", "beeline"):
        await create_submission(msg, state, phone, operator, t, password="—")
    else:
        await state.set_state(SubmitSG.password)
        await msg.answer(
            f'<tg-emoji emoji-id="5361948905900635660">🔑</tg-emoji> Отправь пароль от аккаунта:'
        )

@router.message(SubmitSG.password)
async def submit_password(msg: Message, state: FSMContext):
    data = await state.get_data()
    await create_submission(msg, state, data["phone"], data["operator"], data["type"], msg.text.strip())

async def create_submission(msg: Message, state: FSMContext, phone, operator, t, password):
    prices = await get_prices()
    
    if t == "ready": 
        platform = "wb"
        price = prices["price_ready"]
        payout = prices["payout_ready"]
    elif t == "unreg": 
        platform = "wb"
        price = prices["price_unreg"]
        payout = prices["payout_unreg"]
    elif t == "beeline":
        platform = "beeline"
        price = prices["price_beeline"]
        payout = prices["payout_beeline"]
    else: 
        platform = "ozon"
        price = prices["price_ozon"]
        payout = prices["payout_ozon"]
        
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO submissions (user_id, type, status, platform, operator, wb_phone, wb_password, price, payout, created_at) "
            "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (msg.from_user.id, t, platform, operator, phone, password, price, payout, now()))
        sid = cur.lastrowid
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM submissions WHERE status='queued'") as c:
            pos = (await c.fetchone())[0]
            
    await state.clear()
    p_name = "WB" if platform == "wb" else ("Ozon" if platform == "ozon" else "Билайн")
    t_name = "Готовый" if t == "ready" else ("Нерег" if t == "unreg" else ("Аккаунт" if t == "ozon" else "Билайн"))
    
    await msg.answer(
        f"✅ <b>Заявка #{sid} создана!</b>\n\n"
        f"<tg-emoji emoji-id='5456184504169309058'>🟣</tg-emoji> Площадка: {p_name}\n"
        f"<tg-emoji emoji-id='5271936420125110212'>📞</tg-emoji> Оператор: {operator}\n"
        f"<tg-emoji emoji-id='5303541117361358684'>💰</tg-emoji> К оплате: {payout}$\n"
        f"<tg-emoji emoji-id='5256200407276874123'>⏳</tg-emoji> В очереди (позиция: {pos})\n\nЖди оператора.",
        reply_markup=kb_back())
    await notify_admin(f"📥 Новая заявка #{sid}\n👤 @{msg.from_user.username or msg.from_user.id}\n📱 {p_name} · {t_name} · {operator}\n💰 {payout}$")

# ============================================================
#  ЮЗЕР — ПРОФИЛЬ / РЕФ / ВЫВОД / ТИКЕТ / ИСТОРИЯ
# ============================================================

@router.callback_query(F.data == "user:profile")
async def cb_profile(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    me = await bot.get_me()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM submissions WHERE user_id=?", (cb.from_user.id,)) as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='success'", (cb.from_user.id,)) as c:
            succ = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='fail'", (cb.from_user.id,)) as c:
            fails = (await c.fetchone())[0]
        async with db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=? AND status='pending'", (cb.from_user.id,)) as c:
            hold = (await c.fetchone())[0]

    text = (
        f'<tg-emoji emoji-id="5319289070218674433">👤</tg-emoji> <b>Мой профиль</b>\n\n'
        f'<tg-emoji emoji-id="5231211574584178182">👛</tg-emoji> <b>Баланс:</b> {user["balance"]:.2f} $\n'
        f'<tg-emoji emoji-id="5397633774823427300">⏳</tg-emoji> <b>На выводе:</b> {hold:.2f} $\n\n'
        f'<tg-emoji emoji-id="5213137175381566462">🗂</tg-emoji> <b>Заказов всего:</b> {total}\n'
        f'<tg-emoji emoji-id="5359828295863005340">✅</tg-emoji> <b>Успешно:</b> {succ}\n'
        f'<tg-emoji emoji-id="5359361785105249383">❌</tg-emoji> <b>Неуспешно:</b> {fails}\n\n'
        f'<tg-emoji emoji-id="5278305362703835500">🔗</tg-emoji> <b>Реф-ссылка:</b>\n'
        f'<code>https://t.me/{me.username}?start={user["ref_code"]}</code>'
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="История выводов", callback_data="user:withdraw_history", icon_custom_emoji_id="5449574412061810740")],
        [InlineKeyboardButton(text="Назад", callback_data="back:main", icon_custom_emoji_id="5359361785105249383")]
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "user:withdraw_history")
async def cb_withdraw_history(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 10", (cb.from_user.id,)) as cur:
            rows = await cur.fetchall()
            
    if not rows:
        await cb.message.edit_text("📜 У тебя пока нет выводов.", reply_markup=kb_back())
        await cb.answer(); return
        
    text = "📜 <b>Последние 10 выводов</b>\n\n"
    for r in rows:
        status_icon = "⏳" if r["status"] == "pending" else ("✅" if r["status"] == "paid" else "❌")
        text += f"{status_icon} <b>{r['amount']:.2f}$</b> · {r['method']} · {r['created_at'][:10]}\n"
        
    await cb.message.edit_text(text, reply_markup=kb_back())
    await cb.answer()

@router.callback_query(F.data == "user:ref")
async def cb_ref(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    me = await bot.get_me()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (cb.from_user.id,)) as c:
            cnt = (await c.fetchone())[0]
        async with db.execute("SELECT COALESCE(SUM(bonus_paid),0) FROM referrals WHERE referrer_id=?", (cb.from_user.id,)) as c:
            earned = (await c.fetchone())[0]

    text = (
        f'<tg-emoji emoji-id="5233316306127709823">🎁</tg-emoji> <b>Реферальная программа</b>\n\n'
        f"👥 <b>Приглашено друзей:</b> {cnt}\n"
        f"<tg-emoji emoji-id='5231211574584178182'>💰</tg-emoji> <b>Заработано на рефералах:</b> {earned:.2f} $\n"
        f"💵 <b>Твой бонус:</b> {REF_BONUS}$ за каждую успешную сдачу друга\n\n"
        f'<tg-emoji emoji-id="5278305362703835500">🔗</tg-emoji> <b>Твоя ссылка для приглашения:</b>\n'
        f"<code>https://t.me/{me.username}?start={user['ref_code']}</code>\n\n"
        f"<i>Отправь ссылку другу. Как только он успешно сдаст номер — ты получишь бонус на баланс!</i>"
    )
    await cb.message.edit_text(text, reply_markup=kb_back())
    await cb.answer()

@router.callback_query(F.data == "user:withdraw")
async def cb_withdraw(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    if user["balance"] < MIN_WITHDRAW:
        await cb.answer(f"Минимум {MIN_WITHDRAW}$", show_alert=True); return
    await state.set_state(WithdrawSG.amount)
    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5231211574584178182">💸</tg-emoji> <b>Вывод</b>\n\n'
        f"💰 Доступно: {user['balance']:.2f}$\n\nКуда вывести?", 
        reply_markup=kb_withdraw_method()
    )
    await cb.answer()

@router.callback_query(F.data.startswith("wd:"))
async def cb_withdraw_method(cb: CallbackQuery, state: FSMContext):
    method = cb.data.split(":")[1]
    await state.update_data(method=method)
    await cb.message.edit_text("Введи сумму для вывода:")
    await cb.answer()

@router.message(WithdrawSG.amount)
async def withdraw_amount(msg: Message, state: FSMContext):
    try: amt = float(msg.text.replace(",", "."))
    except: await msg.answer("Введи число."); return
    user = await get_user(msg.from_user.id)
    if amt < MIN_WITHDRAW: await msg.answer(f"Минимум {MIN_WITHDRAW}$"); return
    if amt > user["balance"]: await msg.answer("Недостаточно средств."); return
    
    data = await state.get_data()
    deadline = (datetime.utcnow() + timedelta(minutes=WITHDRAW_TIMEOUT_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO withdrawals (user_id, amount, method, status, created_at, deadline) VALUES (?, ?, ?, 'pending', ?, ?)", (msg.from_user.id, amt, data["method"], now(), deadline))
        wid = cur.lastrowid
        await db.execute("UPDATE users SET balance = balance - ? WHERE tg_id=?", (amt, msg.from_user.id))
        await db.execute("INSERT INTO transactions (user_id, amount, type, ref_id, note, created_at) VALUES (?, ?, 'withdrawal', ?, ?, ?)", (msg.from_user.id, -amt, wid, f"Вывод {data['method']}", now()))
        await db.commit()
    await state.clear()
    await msg.answer(f"✅ Заявка на вывод #{wid} создана\n💰 {amt:.2f}$ · {data['method']}\nЖди чек.", reply_markup=kb_back())
    await notify_admin(f"💸 Вывод #{wid}\n👤 <code>{msg.from_user.id}</code>\n💰 {amt:.2f}$ · {data['method']}", kb_admin_withdraw(wid))

@router.callback_query(F.data == "user:support")
async def cb_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TicketSG.text)
    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5318762236645246095">🆘</tg-emoji> <b>Поддержка</b>\n\nОпиши проблему (можно скрин):'
    )
    await cb.answer()

@router.message(TicketSG.text)
async def ticket_create(msg: Message, state: FSMContext):
    if msg.reply_to_message: return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO tickets (user_id, status, created_at) VALUES (?, 'open', ?)", (msg.from_user.id, now()))
        tid = cur.lastrowid
        file_id = msg.photo[-1].file_id if msg.photo else None
        await db.execute("INSERT INTO ticket_messages (ticket_id, from_user, text, file_id, message_id, created_at) VALUES (?, 1, ?, ?, ?, ?)", (tid, msg.text or msg.caption or "", file_id, msg.message_id, now()))
        await db.commit()
    await state.clear()
    await msg.answer("✅ Тикет создан.", reply_markup=kb_back())
    await notify_admin(f"🆘 Тикет #{tid}\n👤 @{msg.from_user.username or msg.from_user.id}\n💬 {msg.text or msg.caption or ''}", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Ответить", callback_data=f"ot:reply:{tid}", icon_custom_emoji_id="5451620684805481209")]]))

# ============================================================
#  ВБИВ
# ============================================================

@router.callback_query(F.data == "vbi:queue_menu")
async def cb_vbi_queue_menu(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    if not user["vbi_access"]:
        await cb.answer("Доступ отключён", show_alert=True); return
        
    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5406921525636916650">📥</tg-emoji> <b>Выбор очереди</b>\n\nКакую заявку хочешь взять?', 
        reply_markup=kb_vbi_queue_types()
    )
    await cb.answer()

@router.callback_query(F.data == "vbi:queue_stats")
async def cb_vbi_queue_stats(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT platform, type, COUNT(*) FROM submissions WHERE status='queued' GROUP BY platform, type") as cur:
            rows = await cur.fetchall()
            
    if not rows:
        await cb.answer("Очередь пуста!", show_alert=True); return
        
    text = "📊 <b>Статистика очереди</b>\n\n"
    for r in rows:
        plat, t_type, count = r[0], r[1], r[2]
        p_name = "WB" if plat == "wb" else "Ozon"
        t_name = "Готовый" if t_type == "ready" else ("Нерег" if t_type == "unreg" else "Аккаунт")
        text += f"• {p_name} ({t_name}): <b>{count}</b> шт.\n"
        
    await cb.message.edit_text(text, reply_markup=kb_back())
    await cb.answer()

@router.callback_query(F.data.startswith("vbi:take_type:"))
async def cb_vbi_take_type(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    if not user["vbi_access"]:
        await cb.answer("Доступ отключён", show_alert=True); return
        
    t_type = cb.data.split(":")[2]
    
    if t_type == "wb_ready":
        query = "SELECT * FROM submissions WHERE status='queued' AND platform='wb' AND type='ready' ORDER BY id ASC LIMIT 1"
    elif t_type == "wb_unreg":
        query = "SELECT * FROM submissions WHERE status='queued' AND platform='wb' AND type='unreg' ORDER BY id ASC LIMIT 1"
    elif t_type == "ozon":
        query = "SELECT * FROM submissions WHERE status='queued' AND platform='ozon' ORDER BY id ASC LIMIT 1"
    else: # beeline
        query = "SELECT * FROM submissions WHERE status='queued' AND platform='beeline' ORDER BY id ASC LIMIT 1"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query) as cur:
            sub = await cur.fetchone()
            
        if not sub:
            await cb.answer("В этой очереди пока пусто!", show_alert=True); return
            
        await db.execute("UPDATE submissions SET status='in_work', taker_id=? WHERE id=?", (cb.from_user.id, sub["id"]))
        await db.commit()

    p_name = "WB" if sub["platform"] == "wb" else "Ozon"
    t_name = "Готовый" if sub["type"] == "ready" else ("Нерег" if sub["type"] == "unreg" else "Аккаунт")
    
    await cb.message.edit_text(
        f"📥 <b>Заявка #{sub['id']}</b>\n\n"
        f"<tg-emoji emoji-id='5456184504169309058'>🟣</tg-emoji> {p_name} ({t_name})\n"
        f"<tg-emoji emoji-id='5271936420125110212'>📞</tg-emoji> Номер: <code>{sub['wb_phone']}</code> ({sub['operator']})\n"
        f"<tg-emoji emoji-id='5361948905900635660'>🔑</tg-emoji> Пароль: <code>{sub['wb_password']}</code>\n"
        f"<tg-emoji emoji-id='5303541117361358684'>💰</tg-emoji> К оплате: <b>{sub['price']}$</b>",
        reply_markup=kb_vbi_work(sub["id"]))
    await cb.answer()
    
    await notify_user(sub["user_id"], 
        f'<tg-emoji emoji-id="5271936420125110212">📞</tg-emoji> <b>Заявка #{sub["id"]}: номер в обработке.</b>\n\nВзял оператор.')
    await notify_admin(f"📥 @{cb.from_user.username or cb.from_user.id} взял #{sub['id']} ({sub['platform']} · {sub['type']})")

@router.callback_query(F.data == "vbi:mine")
async def cb_vbi_mine(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM submissions WHERE taker_id=? AND status IN ('in_work','awaiting_code') ORDER BY id", (cb.from_user.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await cb.answer("Нет активных заявок", show_alert=True); return
    text = "📂 <b>Мои заявки</b>\n\n"
    for r in rows:
        text += f"#{r['id']} · {r['wb_phone']} · {'WB' if r['platform']=='wb' else 'Ozon'} ({r['operator']})\n"
    await cb.message.edit_text(text, reply_markup=kb_back())
    await cb.answer()

@router.callback_query(F.data.startswith("vbi:code:"))
async def cb_vbi_code(cb: CallbackQuery):
    sid = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, wb_phone, platform FROM submissions WHERE id=?", (sid,)) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute("UPDATE submissions SET status='awaiting_code' WHERE id=?", (sid,))
            await db.commit()
    if row:
        p_name = "WB" if row["platform"] == "wb" else "Ozon"
        await notify_user(row["user_id"], f"<tg-emoji emoji-id='5231349962725419070'>📲</tg-emoji> <b>Оператор запрашивает код.</b>\n\nОтправьте код по номеру <code>{row['wb_phone']}</code> ({p_name}) <b>в ответ на это сообщение</b>.")
        await cb.answer("Запрос отправлен")
    else:
        await cb.answer("Заявка не найдена", show_alert=True)

@router.callback_query(F.data.startswith("vbi:pin:"))
async def cb_vbi_pin(cb: CallbackQuery):
    sid = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, wb_phone, platform FROM submissions WHERE id=?", (sid,)) as cur:
            row = await cur.fetchone()
    if row:
        await notify_user(row["user_id"], f"<tg-emoji emoji-id='5276314275994954605'>🔐</tg-emoji> <b>Оператор запрашивает ПИН.</b>\n\nОтправьте ПИН по номеру <code>{row['wb_phone']}</code> <b>в ответ на это сообщение</b>.")
        await cb.answer("Запрос ПИН отправлен")

@router.callback_query(F.data.startswith("vbi:success:"))
async def cb_vbi_success(cb: CallbackQuery):
    sid = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM submissions WHERE id=?", (sid,)) as cur:
            sub = await cur.fetchone()
        if not sub or sub["status"] in ("success", "fail"):
            await cb.answer("Заявка закрыта", show_alert=True); return
            
        await db.execute("UPDATE submissions SET status='success', closed_at=? WHERE id=?", (now(), sid))
        await db.commit()
        
        async with db.execute("SELECT referred_by FROM users WHERE tg_id=?", (sub["user_id"],)) as cur:
            ref_row = await cur.fetchone()
        referred_by = ref_row[0] if ref_row else None
        
        if referred_by:
            async with db.execute("SELECT id FROM referrals WHERE referred_id=? AND referrer_id=?", (sub["user_id"], referred_by)) as cur:
                ref_rec = await cur.fetchone()
                
            if ref_rec:
                await add_balance(referred_by, REF_BONUS, "ref_bonus", ref_rec["id"], f"Бонус за сдачу #{sid}")
                await db.execute("UPDATE referrals SET bonus_paid = bonus_paid + ?, submission_id=? WHERE id=?", (REF_BONUS, sid, ref_rec["id"]))
                await db.commit()
                await notify_user(referred_by, f"🎁 <b>Реферальный бонус!</b>\n\nТвой реферал успешно сдал номер. Тебе начислено <b>+{REF_BONUS:.2f}$</b>")
        
    margin = sub["price"] - sub["payout"]
    await add_balance(sub["user_id"], sub["payout"], "submission_payout", sid, f"Сдача #{sid}")
    await notify_user(sub["user_id"], f"<tg-emoji emoji-id='5359828295863005340'>✅</tg-emoji> <b>Заявка #{sid} — УСПЕХ!</b>\n\n💰 +{sub['payout']:.2f}$ на баланс.")
    await cb.message.edit_text(f"✅ Заявка #{sid} — УСПЕХ")
    await cb.answer("Успех")
    await notify_admin(f"✅ Успех #{sid}\n👤 <code>{sub['user_id']}</code>\n🛠 @{cb.from_user.username or cb.from_user.id}\n📱 {sub['platform']} · {sub['operator']}\n💰 Маржа: +{margin:.2f}$")

@router.callback_query(F.data.startswith("vbi:fail:"))
async def cb_vbi_fail(cb: CallbackQuery):
    sid = int(cb.data.split(":")[2])
    await cb.message.edit_text(f"❌ <b>Неудача #{sid}</b>\n\nПричина:", reply_markup=kb_vbi_fail_reasons(sid))
    await cb.answer()

@router.callback_query(F.data.startswith("vbi:fr:"))
async def cb_vbi_fail_reason(cb: CallbackQuery):
    parts = cb.data.split(":", 3)
    await _close_fail(cb, int(parts[2]), parts[3], None)

@router.callback_query(F.data.startswith("vbi:fc:"))
async def cb_vbi_fail_custom(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    await state.update_data(fail_sid=sid)
    await state.set_state(VerdictSG.comment)
    await cb.message.edit_text(f"✍️ Причина неудачи по заявке #{sid}:")
    await cb.answer()

@router.message(VerdictSG.comment)
async def vbi_fail_comment(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await _close_fail(msg, data["fail_sid"], msg.text.strip(), None)

@router.callback_query(F.data.startswith("vbi:ff:"))
async def cb_vbi_fail_file(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    await state.update_data(fail_sid=sid)
    await state.set_state(VerdictSG.file)
    await cb.message.edit_text(f"📎 Прикрепи скрин неудачи #{sid}:")
    await cb.answer()

@router.message(VerdictSG.file, F.photo | F.document)
async def vbi_fail_file(msg: Message, state: FSMContext):
    data = await state.get_data()
    file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
    await state.clear()
    await _close_fail(msg, data["fail_sid"], "Скрин приложен", file_id)

async def _close_fail(event, sid, reason, file_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM submissions WHERE id=?", (sid,)) as cur:
            sub = await cur.fetchone()
        if not sub or sub["status"] in ("success", "fail"): return
        await db.execute("UPDATE submissions SET status='fail', verdict_comment=?, verdict_file_id=?, closed_at=? WHERE id=?", (reason, file_id, now(), sid))
        await db.commit()
        
    text = f"❌ <b>Заявка #{sid} — НЕУДАЧА</b>\n\n📝 Причина: {reason}\n\nСредства не начислены."
    await notify_user(sub["user_id"], text)
    if file_id: await bot.send_photo(sub["user_id"], file_id, caption=f"📎 Скрин по заявке #{sid}")
    
    try:
        if hasattr(event, "message") and event.message:
            await event.message.edit_text(f"❌ Заявка #{sid} закрыта как неудача\n📝 {reason}")
    except: pass
    if hasattr(event, "answer"):
        try: await event.answer("Готово")
        except: pass
    await notify_admin(f"❌ Неудача #{sid}\n📝 {reason}\n📎 Скрин: {'да' if file_id else 'нет'}")

@router.callback_query(F.data.startswith("vbi:return:"))
async def cb_vbi_return(cb: CallbackQuery):
    sid = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE submissions SET status='queued', taker_id=NULL WHERE id=?", (sid,))
        await db.commit()
    await cb.message.edit_text(f"↩️ Заявка #{sid} возвращена в очередь", reply_markup=kb_back())
    await cb.answer()
    await notify_admin(f"↩️ @{cb.from_user.username or cb.from_user.id} вернул заявку #{sid}")

# ============================================================
#  ЮЗЕР — ОТВЕТ НА КОД (РЕПЛАЙ)
# ============================================================

@router.message(F.reply_to_message)
async def user_reply_handler(msg: Message, state: FSMContext):
    if not msg.reply_to_message or not msg.reply_to_message.text: return
    reply_text = msg.reply_to_message.text
    if "Оператор запрашивает" not in reply_text: return
        
    match = re.search(r'\d{10,11}', reply_text)
    if not match: return
    phone_str = match.group()
    if not phone_str.startswith("+"): phone_str = "+" + phone_str
        
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, taker_id FROM submissions WHERE user_id=? AND wb_phone LIKE ? AND status='awaiting_code' ORDER BY id DESC LIMIT 1",
            (msg.from_user.id, f"%{phone_str[-10:]}")) as cur:
            sub = await cur.fetchone()
        
        if sub:
            await db.execute("UPDATE submissions SET code=? WHERE id=?", (msg.text.strip(), sub["id"]))
            await db.commit()
            await state.clear()
            await msg.answer(f'<tg-emoji emoji-id="5359828295863005340">✅</tg-emoji> Код по заявке #{sub["id"]} передан оператору.')
            if sub["taker_id"]: await notify_user(sub["taker_id"], f"📲 <b>Код по заявке #{sub['id']}:</b> <code>{msg.text.strip()}</code>")
            await notify_admin(f"📲 Код #{sub['id']}: <code>{msg.text.strip()}</code>")
        else:
            await msg.answer("⚠️ Нет активной заявки с таким номером или код уже передан.")

# ============================================================
#  АДМИНКА ТС
# ============================================================

@router.callback_query(F.data == "owner:income")
async def cb_owner_income(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT platform, COUNT(*) as cnt, SUM(price-payout) as m FROM submissions WHERE status='success' GROUP BY platform") as c:
            by_plat = await c.fetchall()
        async with db.execute("SELECT COALESCE(SUM(balance),0) FROM users") as c:
            obligations = (await c.fetchone())[0]
        async with db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'") as c:
            paid_out = (await c.fetchone())[0]
        async with db.execute("SELECT COALESCE(SUM(bonus_paid),0) FROM referrals") as c:
            ref_bonuses = (await c.fetchone())[0]

    text = "📊 <b>Финансовая статистика</b>\n\n"
    total_margin = 0
    for r in by_plat:
        m = r["m"] or 0
        total_margin += m
        icon = "🟣" if r['platform'] == 'wb' else "🔵"
        name = "WB" if r['platform'] == 'wb' else "Ozon"
        text += f"{icon} <b>{name}:</b> {r['cnt']} сдач → <b>+{m:.2f}$</b>\n"
    text += f"\n🎁 <b>Реф-бонусы:</b> -{ref_bonuses:.2f}$\n"
    text += f"💎 <b>Чистая маржа:</b> <b>{total_margin - ref_bonuses:.2f}$</b>\n\n"
    text += f"💸 <b>Выплачено юзерам:</b> {paid_out:.2f}$\n"
    text += f"⏳ <b>На балансах (долг):</b> {obligations:.2f}$\n"
    await cb.message.edit_text(text, reply_markup=kb_back())
    await cb.answer()

@router.callback_query(F.data == "owner:accept")
async def cb_owner_accept(cb: CallbackQuery):
    wb_ready = (await get_setting("accept_wb") or "1") == "1"
    wb_unreg = (await get_setting("accept_wb_unreg") or "1") == "1"
    oz_on = (await get_setting("accept_ozon") or "1") == "1"
    bl_on = (await get_setting("accept_beeline") or "1") == "1"
    
    text = "⚙️ <b>Управление приёмом</b>\n\nНажми на площадку, чтобы переключить:"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="WB Готовый (ВКЛ)" if wb_ready else "WB Готовый (ВЫКЛ)", callback_data="os:toggle:wb", style="success" if wb_ready else "danger", icon_custom_emoji_id="5456184504169309058")],
        [InlineKeyboardButton(text="WB Нерег (ВКЛ)" if wb_unreg else "WB Нерег (ВЫКЛ)", callback_data="os:toggle:wb_unreg", style="success" if wb_unreg else "danger", icon_custom_emoji_id="5456184504169309058")],
        [InlineKeyboardButton(text="Ozon (ВКЛ)" if oz_on else "Ozon (ВЫКЛ)", callback_data="os:toggle:ozon", style="success" if oz_on else "danger", icon_custom_emoji_id="5458594960074907382")],
        [InlineKeyboardButton(text="Билайн (ВКЛ)" if bl_on else "Билайн (ВЫКЛ)", callback_data="os:toggle:beeline", style="success" if bl_on else "danger", icon_custom_emoji_id="5280919528908267119")],
        [InlineKeyboardButton(text="Назад", callback_data="back:main", icon_custom_emoji_id="5359361785105249383")]
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("os:toggle:"))
async def cb_os_toggle(cb: CallbackQuery):
    plat = cb.data.split(":")[2]
    key = f"accept_{plat}"
    cur = (await get_setting(key) or "1") == "1"
    await set_setting(key, "0" if cur else "1")
    await cb.answer(f"{plat.upper()} переключен")
    await cb_owner_accept(cb)

@router.callback_query(F.data == "owner:withdrawals")
async def cb_owner_withdrawals(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id") as cur:
            rows = await cur.fetchall()
    if not rows:
        await cb.message.edit_text("💸 Нет заявок на вывод.", reply_markup=kb_back()); await cb.answer(); return
    await cb.message.edit_text(f"💸 <b>Заявок: {len(rows)}</b>", reply_markup=kb_back())
    for r in rows:
        await cb.message.answer(f"💸 #{r['id']}\n👤 <code>{r['user_id']}</code>\n💰 {r['amount']:.2f}$ · {r['method']}", reply_markup=kb_admin_withdraw(r["id"]))
    await cb.answer()

@router.callback_query(F.data.startswith("ow:pay:"))
async def cb_ow_pay(cb: CallbackQuery, state: FSMContext):
    wid = int(cb.data.split(":")[2])
    await state.update_data(pay_wid=wid)
    await state.set_state(OwnerSG.receipt)
    await cb.message.edit_text(f"🧾 Отправь ссылку на чек для #{wid}:")
    await cb.answer()

@router.message(OwnerSG.receipt)
async def owner_receipt(msg: Message, state: FSMContext):
    data = await state.get_data()
    wid = data["pay_wid"]
    receipt = msg.text.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)) as cur:
            w = await cur.fetchone()
        await db.execute("UPDATE withdrawals SET status='paid', receipt=?, closed_at=? WHERE id=?", (receipt, now(), wid))
        await db.commit()
    await state.clear()
    await notify_user(w["user_id"], f"💸 <b>Выплата выполнена!</b>\n\n💰 {w['amount']:.2f}$ · {w['method']}\n🧾 Чек: {receipt}")
    await msg.answer(f"✅ Чек отправлен юзеру {w['user_id']}")

@router.callback_query(F.data.startswith("ow:rej:"))
async def cb_ow_rej(cb: CallbackQuery, state: FSMContext):
    wid = int(cb.data.split(":")[2])
    await state.update_data(rej_wid=wid)
    await state.set_state(OwnerSG.reason)
    await cb.message.edit_text(f"✍️ Причина отклонения #{wid}:")
    await cb.answer()

@router.message(OwnerSG.reason)
async def owner_reject(msg: Message, state: FSMContext):
    data = await state.get_data()
    wid = data["rej_wid"]
    reason = msg.text.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)) as cur:
            w = await cur.fetchone()
        await db.execute("UPDATE withdrawals SET status='rejected', reason=?, closed_at=? WHERE id=?", (reason, now(), wid))
        await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id=?", (w["amount"], w["user_id"]))
        await db.commit()
    await state.clear()
    await notify_user(w["user_id"], f"❌ Вывод #{wid} отклонён\n📝 {reason}\n💰 {w['amount']:.2f}$ возвращены на баланс.")
    await msg.answer("✅ Отклонено, средства возвращены.")

@router.callback_query(F.data.startswith("ow:ext:"))
async def cb_ow_ext(cb: CallbackQuery):
    wid = int(cb.data.split(":")[2])
    new_dl = (datetime.utcnow() + timedelta(minutes=WITHDRAW_TIMEOUT_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE withdrawals SET deadline=? WHERE id=?", (new_dl, wid))
        await db.commit()
    await cb.answer("Продлено на 30 мин")
    await cb.message.edit_text(f"⏱ Вывод #{wid} продлён.")

@router.callback_query(F.data.startswith("owner:users"))
async def cb_owner_users(cb: CallbackQuery):
    page = 0
    if ":" in cb.data and cb.data.split(":")[1].isdigit():
        page = int(cb.data.split(":")[1])
        
    offset = page * 10
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 10 OFFSET ?", (offset,)) as cur:
            rows = await cur.fetchall()
            
    if not rows:
        await cb.answer("Больше пользователей нет", show_alert=True); return

    text = f"👥 <b>Пользователи (стр. {page+1})</b>\n\n"
    for u in rows:
        ban = "🚫" if u["is_banned"] else "✅"
        text += f"{ban} <code>{u['tg_id']}</code> @{u['username'] or '—'} | {u['balance']:.1f}$\n"
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"owner:users:{page-1}"), 
         InlineKeyboardButton(text="Вперед ▶️", callback_data=f"owner:users:{page+1}")] if page > 0 else 
        [InlineKeyboardButton(text="Вперед ▶️", callback_data=f"owner:users:{page+1}")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="back:main")]
    ])
    
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("ou:ban:"))
async def cb_ou_ban(cb: CallbackQuery, state: FSMContext):
    tg_id = int(cb.data.split(":")[2])
    await state.update_data(ban_uid=tg_id)
    await state.set_state(OwnerSG.ban_reason)
    await cb.message.edit_text(f"✍️ Причина бана <code>{tg_id}</code>:")
    await cb.answer()

@router.message(OwnerSG.ban_reason)
async def owner_ban(msg: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data["ban_uid"]
    reason = msg.text.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=1 WHERE tg_id=?", (tg_id,))
        await db.commit()
    await state.clear()
    await notify_user(tg_id, f"🚫 Ты забанен.\n📝 {reason}")
    await msg.answer(f"✅ <code>{tg_id}</code> забанен")

@router.callback_query(F.data.startswith("ou:unban:"))
async def cb_ou_unban(cb: CallbackQuery):
    tg_id = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=0 WHERE tg_id=?", (tg_id,))
        await db.commit()
    await cb.message.edit_text(f"✅ <code>{tg_id}</code> разбанен")
    await cb.answer()

@router.callback_query(F.data.startswith("ou:bal:"))
async def cb_ou_bal(cb: CallbackQuery, state: FSMContext):
    tg_id = int(cb.data.split(":")[2])
    await state.update_data(bal_uid=tg_id)
    await state.set_state(OwnerSG.balance_amount)
    await cb.message.edit_text(f"💰 Введи сумму (например, 10 или -5):")
    await cb.answer()

@router.message(OwnerSG.balance_amount)
async def owner_balance(msg: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data["bal_uid"]
    try: amt = float(msg.text.replace(",", "."))
    except: await msg.answer("Введи число."); return
    await add_balance(tg_id, amt, "manual", None, "Правка ТС")
    await state.clear()
    await msg.answer(f"✅ Баланс <code>{tg_id}</code> изменён на {amt:+.2f}$")

@router.callback_query(F.data == "owner:vbi")
async def cb_owner_vbi(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE role='vbi'") as cur:
            rows = await cur.fetchall()
    if not rows:
        await cb.message.edit_text("🛠 Вбивов нет.", reply_markup=kb_back()); await cb.answer(); return
    await cb.message.edit_text(f"🛠 <b>Вбивы: {len(rows)}</b>", reply_markup=kb_back())
    for u in rows:
        active = u["vbi_access"]
        await cb.message.answer(f"{'✅' if active else '⛔'} <code>{u['tg_id']}</code> @{u['username'] or '—'}", reply_markup=kb_admin_vbi(u["tg_id"], active))
    await cb.answer()

@router.callback_query(F.data.startswith("ov:off:"))
async def cb_ov_off(cb: CallbackQuery):
    tg_id = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET vbi_access=0 WHERE tg_id=?", (tg_id,))
        await db.commit()
    await cb.message.edit_text(f"🚫 Доступ <code>{tg_id}</code> отключён")
    await cb.answer()

@router.callback_query(F.data.startswith("ov:on:"))
async def cb_ov_on(cb: CallbackQuery):
    tg_id = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET vbi_access=1 WHERE tg_id=?", (tg_id,))
        await db.commit()
    await cb.message.edit_text(f"✅ Доступ <code>{tg_id}</code> включён")
    await cb.answer()

@router.callback_query(F.data == "owner:tickets")
async def cb_owner_tickets(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tickets WHERE status!='closed' ORDER BY id") as cur:
            rows = await cur.fetchall()
    if not rows:
        await cb.message.edit_text("🆘 Тикетов нет.", reply_markup=kb_back()); await cb.answer(); return
    await cb.message.edit_text(f"🆘 <b>Открытые тикеты:</b>", reply_markup=kb_back())
    for t in rows:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT text FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (t["id"],)) as cur:
                last = await cur.fetchone()
        text = f"🆘 #{t['id']}\n👤 <code>{t['user_id']}</code>\n💬 {last['text'][:100] if last else ''}"
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Ответить", callback_data=f"ot:reply:{t['id']}", icon_custom_emoji_id="5451620684805481209"), InlineKeyboardButton(text="✅ Закрыть", callback_data=f"ot:close:{t['id']}", icon_custom_emoji_id="5359361785105249383")]]))
    await cb.answer()

@router.callback_query(F.data.startswith("ot:reply:"))
async def cb_ot_reply(cb: CallbackQuery, state: FSMContext):
    tid = int(cb.data.split(":")[2])
    await state.update_data(tid=tid)
    await state.set_state(OwnerSG.ticket_reply)
    await cb.message.edit_text(f"✍️ Ответ на тикет #{tid}:")
    await cb.answer()

@router.message(OwnerSG.ticket_reply)
async def owner_ticket_reply(msg: Message, state: FSMContext):
    data = await state.get_data()
    tid = data["tid"]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM tickets WHERE id=?", (tid,)) as cur:
            t = await cur.fetchone()
        async with db.execute("SELECT message_id FROM ticket_messages WHERE ticket_id=? AND from_user=1 ORDER BY id LIMIT 1", (tid,)) as cur:
            first = await cur.fetchone()
        await db.execute("INSERT INTO ticket_messages (ticket_id, from_user, text, created_at) VALUES (?, 0, ?, ?)", (tid, msg.text.strip(), now()))
        await db.execute("UPDATE tickets SET status='answered' WHERE id=?", (tid,))
        await db.commit()
    await state.clear()
    try:
        if first and first["message_id"]:
            await bot.send_message(t["user_id"], f"🆘 <b>Ответ поддержки:</b>\n\n{msg.text.strip()}", reply_to_message_id=first["message_id"])
        else:
            await notify_user(t["user_id"], f"🆘 <b>Ответ поддержки:</b>\n\n{msg.text.strip()}")
    except:
        await notify_user(t["user_id"], f"🆘 <b>Ответ поддержки:</b>\n\n{msg.text.strip()}")
    await msg.answer("✅ Ответ отправлен.")

@router.callback_query(F.data.startswith("ot:close:"))
async def cb_ot_close(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM tickets WHERE id=?", (tid,)) as cur:
            t = await cur.fetchone()
        await db.execute("UPDATE tickets SET status='closed', closed_at=? WHERE id=?", (now(), tid))
        await db.commit()
    await cb.message.edit_text(f"✅ Тикет #{tid} закрыт")
    await cb.answer()
    if t: await notify_user(t["user_id"], f"✅ Тикет #{tid} закрыт.")

@router.callback_query(F.data == "owner:broadcast")
async def cb_owner_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastSG.waiting_text)
    await cb.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Отправь сообщение, которое нужно разослать <b>всем пользователям</b>.\n"
        "Можно прикрепить фото или текст.\n\n"
        "<i>Внимание: бот разошлет сообщение по одному, чтобы избежать блокировки.</i>"
    )
    await cb.answer()

@router.message(BroadcastSG.waiting_text)
async def broadcast_send(msg: Message, state: FSMContext):
    await state.clear()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT tg_id FROM users") as cur:
            users = await cur.fetchall()
            
    if not users:
        await msg.answer("В базе нет пользователей.")
        return
        
    await msg.answer(f"🚀 Начинаю рассылку на {len(users)} юзеров...")
    
    success = 0
    for (tg_id,) in users:
        try:
            if msg.photo:
                await bot.send_photo(tg_id, msg.photo[-1].file_id, caption=msg.caption or "", parse_mode="HTML")
            else:
                await bot.send_message(tg_id, msg.text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
            
    await msg.answer(f"✅ Рассылка завершена!\nДоставлено: {success} из {len(users)}") 

@router.callback_query(F.data == "owner:prices")
async def cb_owner_prices(cb: CallbackQuery):
    p = await get_prices()
    text = (
        "💰 <b>Редактор цен</b>\n\n"
        f"🟣 <b>WB Готовый:</b> платим {p['payout_ready']}$ (маржа {p['price_ready'] - p['payout_ready']}$)\n"
        f"🟣 <b>WB Нерег:</b> платим {p['payout_unreg']}$ (маржа {p['price_unreg'] - p['payout_unreg']}$)\n"
        f"🔵 <b>Ozon:</b> платим {p['payout_ozon']}$ (маржа {p['price_ozon'] - p['payout_ozon']}$)\n"
        f"📱 <b>Билайн:</b> платим {p['payout_beeline']}$ (маржа {p['price_beeline'] - p['payout_beeline']}$)\n\n"
        "Нажми на кнопку, чтобы изменить цену:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Изм. WB Готовый", callback_data="edit_price:ready", icon_custom_emoji_id="5456184504169309058")],
        [InlineKeyboardButton(text=f"Изм. WB Нерег", callback_data="edit_price:unreg", icon_custom_emoji_id="5456184504169309058")],
        [InlineKeyboardButton(text=f"Изм. Ozon", callback_data="edit_price:ozon", icon_custom_emoji_id="5458594960074907382")],
        [InlineKeyboardButton(text=f"Изм. Билайн", callback_data="edit_price:beeline", icon_custom_emoji_id="5280919528908267119")],
        [InlineKeyboardButton(text="Назад", callback_data="back:main", icon_custom_emoji_id="5359361785105249383")]
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("edit_price:"))
async def cb_edit_price(cb: CallbackQuery, state: FSMContext):
    t = cb.data.split(":")[1]
    await state.update_data(price_type=t)
    await state.set_state(PriceSG.waiting_value)
    await cb.message.edit_text(
        f"💰 Введи новую цену выплаты (например, 13.5) для {t.upper()}:\n\n"
        f"<i>Цена, которую платит вбив, и маржа пересчитаются автоматически.</i>"
    )
    await cb.answer()

@router.message(PriceSG.waiting_value)
async def price_value_input(msg: Message, state: FSMContext):
    try:
        val = float(msg.text.replace(",", "."))
    except ValueError:
        await msg.answer("⚠️ Введи число (например, 12.5).")
        return

    data = await state.get_data()
    t = data["price_type"]
    
    await set_setting(f"payout_{t}", f"{val:.2f}")
    await state.clear()
    
    await msg.answer(f"✅ Цена для <b>{t.upper()}</b> изменена на <b>{val:.2f}$</b>")
    await cb_owner_prices(msg)

# ============================================================
#  БЭКАП И ВЕБ-СЕРВЕР (RENDER)
# ============================================================

async def make_backup(manual=False):
    chat_id_val = await get_setting("backup_chat")
    if not chat_id_val and not BACKUP_CHAT_ID: return
    chat_id = int(chat_id_val) if chat_id_val else BACKUP_CHAT_ID
    if not os.path.exists(DB_PATH): return
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    filename = f"veltrix_backup_{ts}.db"
    tmp = f"/tmp/{filename}"
    shutil.copy(DB_PATH, tmp)
    try:
        await bot.send_document(chat_id, FSInputFile(tmp, filename=filename), caption=f"💾 {'Ручной' if manual else 'Ежедневный'} бэкап\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    except Exception as e:
        log.error(f"backup: {e}")
    finally:
        if os.path.exists(tmp): os.remove(tmp)

scheduler.add_job(make_backup, "cron", hour=int(BACKUP_TIME.split(":")[0]), minute=int(BACKUP_TIME.split(":")[1]))

async def handle_webhook(request):
    try:
        # Получаем сырые данные
        data = await request.json()
        # Превращаем их в объект Update, который понимает aiogram
        from aiogram.types import Update
        update = Update.model_validate(data, context={"bot": bot})
        
        # Отправляем в диспетчер
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception as e:
        log.error(f"Webhook error: {e}")
        return web.Response(text="Error", status=500)

async def health_check(request):
    return web.Response(text="Bot is alive!")

async def on_startup(app):
    await init_db()
    scheduler.start()
    await bot.set_webhook(WEBHOOK_URL)
    log.info(f"Webhook set to {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.delete_webhook()
    scheduler.shutdown()

def main():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/", health_check)
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
