import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop")

# =====================================================================
#  SOZLAMALAR (Render -> Environment dan olinadi)
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi! Render -> Environment ga BOT_TOKEN qo'shing")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000 0000 0000 0000")
CARD_OWNER = os.getenv("CARD_OWNER", "Ism Familiya")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "azatrdv").lstrip("@")
NEWS_URL = os.getenv("NEWS_CHANNEL_URL", "")        # Yangiliklar kanali (ixtiyoriy)
ORDERS_URL = os.getenv("ORDERS_CHANNEL_URL", "https://t.me/newstgsale")  # Buyurtmalar kanali linki
ORDERS_CHANNEL_ID = os.getenv("ORDERS_CHANNEL_ID", "@newstgsale")  # Kanal: @username yoki -100... ID
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBAPP_URL = os.getenv("WEBAPP_URL") or (BASE_URL + "/app" if BASE_URL else "")
COLORS = os.getenv("BUTTON_COLORS", "1") == "1"      # rangli tugmalar (0 qilsangiz o'chadi)
DB_PATH = os.getenv("DB_PATH", "bot.db")
TZ = timezone(timedelta(hours=5))                    # Toshkent vaqti

# =====================================================================
#  NARXLAR - SHU YERDAN O'ZGARTIRASIZ
# =====================================================================
STAR_PRICE = 188                  # 1 ta Stars narxi (so'm)
STARS_PRESETS = [50, 100, 150]    # tez tanlash tugmalari
STARS_MIN, STARS_MAX = 50, 10000

PREMIUM = {                       # kalit: (nomi, narxi)
    "3": ("3 oy", 145_000),
    "6": ("6 oy", 195_000),
    "12": ("1 yil", 360_000),
}
GIFTS = {                         # kalit: (nomi, narxi)
    "bear": ("🧸 Ayiqcha", 3_000),
    "heart": ("❤️ Yurak", 3_000),
    "rose": ("🌹 Gul", 10_000),
    "cake": ("🎂 Tort", 10_000),
    "rocket": ("🚀 Raketa", 10_000),
    "cup": ("🏆 Kubok", 20_000),
}
TOPUP_MIN, TOPUP_MAX = 1_000, 2_500_000
PAY_MINUTES = 5

# =====================================================================
#  BAZA
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
    balance INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0,
    private INTEGER DEFAULT 1, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, user_id INTEGER,
    amount INTEGER, status TEXT DEFAULT 'pending',
    created_at INTEGER, expires_at INTEGER, msg_chat INTEGER, msg_id INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT,
    item TEXT, label TEXT, recipient TEXT, price INTEGER,
    status TEXT DEFAULT 'pending', created_at INTEGER
);
"""
db: sqlite3.Connection = None  # main() da ochiladi
last_changes = 0
last_backup_msg = None
BOT_USERNAME = ""


def open_db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.executescript(SCHEMA)
    c.commit()
    return c


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def ensure_user(u):
    """Foydalanuvchini bazaga qo'shadi. Yangi bo'lsa True qaytaradi."""
    cur = db.execute(
        "INSERT OR IGNORE INTO users (user_id, username, full_name, created_at) VALUES (?,?,?,?)",
        (u.id, u.username or "", u.full_name or "", int(time.time())),
    )
    is_new = cur.rowcount == 1
    db.execute(
        "UPDATE users SET username=?, full_name=? WHERE user_id=? AND (username IS NOT ? OR full_name IS NOT ?)",
        (u.username or "", u.full_name or "", u.id, u.username or "", u.full_name or ""),
    )
    db.commit()
    return is_new


def get_user(uid):
    r = db.execute(
        "SELECT balance, total_spent, private, username, full_name FROM users WHERE user_id=?", (uid,)
    ).fetchone()
    return r or (0, 0, 1, "", "")


def get_balance(uid):
    return get_user(uid)[0]


def now_ts():
    return int(time.time())


# =====================================================================
#  BOT, ROUTERLAR
# =====================================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
admin_router = Router()
router = Router()
admin_router.message.filter(F.from_user.id == ADMIN_ID)
admin_router.callback_query.filter(F.from_user.id == ADMIN_ID)
dp.include_router(admin_router)
dp.include_router(router)


async def notify_new_user(u):
    """Yangi foydalanuvchi kelganda adminga xabar yuboradi."""
    if not ADMIN_ID or u.id == ADMIN_ID:
        return
    name = f"@{u.username}" if u.username else html.escape(u.full_name or "username yo'q")
    try:
        await bot.send_message(
            ADMIN_ID,
            "🆕 <b>Yangi foydalanuvchi:</b> " + name + "\n"
            f"🆔 id: <code>{u.id}</code>\n"
            f"🕒 vaqt: {datetime.now(TZ):%d.%m.%Y %H:%M:%S}",
        )
    except Exception:
        log.exception("yangi foydalanuvchi xabari")


class EnsureUser(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and db is not None:
            try:
                if ensure_user(u):
                    await notify_new_user(u)
            except Exception:
                log.exception("ensure_user")
        return await handler(event, data)


dp.update.outer_middleware(EnsureUser())


class Buy(StatesGroup):
    username = State()
    stars_amount = State()


class TopUp(StatesGroup):
    amount = State()


# =====================================================================
#  TUGMALAR
# =====================================================================
def btn(text, cb=None, style=None, url=None, web=None):
    kw = {"text": text}
    if cb:
        kw["callback_data"] = cb
    if url:
        kw["url"] = url
    if web:
        kw["web_app"] = WebAppInfo(url=web)
    if style and COLORS:
        kw["style"] = style  # success=yashil, danger=qizil, primary=ko'k
    return InlineKeyboardButton(**kw)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def back_btn(cb="menu", text="⬅️ Orqaga"):
    return btn(text, cb)


def main_menu():
    rows = [
        [btn("⭐ Stars olish", "stars", "success")],
        [btn("✈️ Premium olish", "premium"), btn("🎁 Gift olish", "gift")],
        [btn("🌐 Telegram akkaunt sotib olish", "account")],
        [btn("💳 Balans to'ldirish", "topup"), btn("👤 Profil", "profile")],
        [btn("ℹ️ Yordam", "help")],
    ]
    if WEBAPP_URL.startswith("https://"):
        rows.append([btn("🔳 Web Ilova", web=WEBAPP_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_text(u):
    name = f"@{u.username}" if u.username else html.escape(u.full_name or "do'st")
    return (
        f"🐥 Assalomu alaykum, <b>{name}</b>\n"
        f"🪪 User ID: <code>{u.id}</code>\n"
        f"└ 💰 Balans: <b>{fmt(get_balance(u.id))} so'm</b>"
    )


async def safe_edit(call: CallbackQuery, text, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            await call.message.answer(text, reply_markup=markup)


async def show(event, text, markup=None):
    if isinstance(event, CallbackQuery):
        await safe_edit(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


# =====================================================================
#  /start, MENYU
# =====================================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(menu_text(message.from_user), reply_markup=main_menu())


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, menu_text(call.from_user), main_menu())
    await call.answer()


# =====================================================================
#  SOTIB OLISH: Stars / Premium / Gift
# =====================================================================
PROMPTS = {
    "stars": (
        "⭐ <b>Stars xarid qilish</b>\n\n"
        "🔎 Stars yuborilishi kerak bo'lgan foydalanuvchi username'ini kiriting:\n"
        "⬇️ Misol: <code>@azatrdv</code>"
    ),
    "premium": (
        "✈️ <b>Telegram Premium olish</b>\n\n"
        "O'zingiz yoki boshqa foydalanuvchi uchun\n"
        "⬇️ Username kiriting: <code>@username</code>"
    ),
    "gift": (
        "🎁 <b>Gift yuborish</b>\n\n"
        "🔎 Gift yubormoqchi bo'lgan foydalanuvchi username'ini kiriting:\n"
        "⬇️ Misol: <code>@azatrdv</code>\n\n"
        "<i>O'zingizga yubormoqchi bo'lsangiz — pastdagi tugmani bosing.</i>"
    ),
}
USERNAME_RE = re.compile(r"@?[A-Za-z][A-Za-z0-9_]{4,31}")


def resolve(kind, item):
    """-> (nomi, narxi)"""
    if kind == "stars":
        n = int(item)
        return f"{n} ⭐ Stars", n * STAR_PRICE
    if kind == "premium":
        label, price = PREMIUM[item]
        return f"Telegram Premium — {label}", price
    label, price = GIFTS[item]
    return f"Gift: {label}", price


@router.callback_query(F.data.in_({"stars", "premium", "gift"}))
async def cb_kind(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(kind=call.data)
    await state.set_state(Buy.username)
    await safe_edit(call, PROMPTS[call.data], kb([btn("🙋 O'zim uchun", "self", "primary")], [back_btn()]))
    await call.answer()


@router.callback_query(F.data == "self", Buy.username)
async def cb_self(call: CallbackQuery, state: FSMContext):
    if not call.from_user.username:
        await call.answer("Sizda username yo'q. Telegram sozlamalarida username qo'ying yoki qo'lda kiriting.", show_alert=True)
        return
    await state.update_data(recipient="@" + call.from_user.username)
    await go_plans(call, state)
    await call.answer()


@router.message(Buy.username, F.text, ~F.text.startswith("/"))
async def msg_username(message: Message, state: FSMContext):
    t = message.text.strip()
    if not USERNAME_RE.fullmatch(t):
        await message.answer("❌ Username noto'g'ri. Masalan: <code>@azatrdv</code>")
        return
    await state.update_data(recipient="@" + t.lstrip("@"))
    await go_plans(message, state)


def stars_kb():
    rows = [[btn(f"⭐ {n} — {fmt(n * STAR_PRICE)} so'm", f"st_{n}")] for n in STARS_PRESETS]
    rows.append([btn("✏️ Boshqa qiymat kiriting", "st_other")])
    rows.append([back_btn()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gifts_kb():
    items = list(GIFTS.items())
    rows = []
    for i in range(0, len(items), 2):
        rows.append([btn(f"{v[0]} — {fmt(v[1])}", f"gf_{k}") for k, v in items[i:i + 2]])
    rows.append([back_btn()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def premium_kb():
    rows = [[btn(f"✈️ {v[0]} — {fmt(v[1])} so'm", f"pr_{k}")] for k, v in PREMIUM.items()]
    rows.append([back_btn()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def go_plans(event, state: FSMContext):
    d = await state.get_data()
    kind, rcp = d.get("kind"), d.get("recipient")
    if not kind or not rcp:
        await show(event, "Sessiya tugadi. /start bosing.")
        return
    if kind == "stars":
        await state.set_state(Buy.stars_amount)
        text = (
            f"⭐ <b>Telegram Stars buyurtma</b>\n\n"
            f"👤 Qabul qiluvchi: <b>{rcp}</b>\n\n"
            f"⬇️ Minimal: <b>{STARS_MIN}</b>\n⬆️ Maksimal: <b>{STARS_MAX}</b>\n\n"
            f"Kerakli Stars miqdorini tanlang yoki raqam bilan yuboring:"
        )
        await show(event, text, stars_kb())
    elif kind == "premium":
        await state.set_state(None)
        await show(event, f"✈️ <b>Telegram Premium</b>\n\n👤 Qabul qiluvchi: <b>{rcp}</b>\n\nMuddatni tanlang:", premium_kb())
    else:
        await state.set_state(None)
        await show(event, f"🎁 <b>Gift tanlang</b>\n\n👤 Qabul qiluvchi: <b>{rcp}</b>", gifts_kb())


@router.callback_query(F.data == "st_other")
async def cb_st_other(call: CallbackQuery):
    await call.answer(f"Miqdorni raqam bilan yuboring ({STARS_MIN}–{STARS_MAX}). Masalan: 250", show_alert=True)


@router.message(Buy.stars_amount, F.text, ~F.text.startswith("/"))
async def msg_stars_amount(message: Message, state: FSMContext):
    digits = re.sub(r"\D", "", message.text)
    if not digits or not (STARS_MIN <= int(digits) <= STARS_MAX):
        await message.answer(f"❌ {STARS_MIN} dan {STARS_MAX} gacha raqam yuboring. Masalan: 250")
        return
    await state.update_data(item=digits)
    await show_confirm(message, state)


@router.callback_query(F.data.startswith("st_"), F.data != "st_other")
async def cb_st(call: CallbackQuery, state: FSMContext):
    await state.update_data(kind="stars", item=call.data[3:])
    await show_confirm(call, state)
    await call.answer()


@router.callback_query(F.data.startswith("pr_"))
async def cb_pr(call: CallbackQuery, state: FSMContext):
    if call.data[3:] not in PREMIUM:
        await call.answer("Xatolik", show_alert=True)
        return
    await state.update_data(kind="premium", item=call.data[3:])
    await show_confirm(call, state)
    await call.answer()


@router.callback_query(F.data.startswith("gf_"))
async def cb_gf(call: CallbackQuery, state: FSMContext):
    if call.data[3:] not in GIFTS:
        await call.answer("Xatolik", show_alert=True)
        return
    await state.update_data(kind="gift", item=call.data[3:])
    await show_confirm(call, state)
    await call.answer()


@router.callback_query(F.data == "gift_again")
async def cb_gift_again(call: CallbackQuery, state: FSMContext):
    await go_plans(call, state)
    await call.answer()


async def show_confirm(event, state: FSMContext):
    d = await state.get_data()
    uid = event.from_user.id
    if not all(k in d for k in ("kind", "item", "recipient")):
        await show(event, "Sessiya tugadi. /start bosing.")
        return
    kind, rcp = d["kind"], d["recipient"]
    label, price = resolve(kind, d["item"])
    bal = get_balance(uid)
    enough = bal >= price
    need = price - bal
    topup_row = [btn("💳 Balans to'ldirish", "topup")]

    if kind == "stars":
        text = (
            "⭐ <b>Buyurtmani tasdiqlang</b>\n\n"
            f"👤 Qabul qiluvchi: <b>{rcp}</b>\n"
            f"⭐ Stars: <b>{d['item']}</b>\n"
            f"💲 Narxi: <b>{fmt(price)} so'm</b>\n"
            f"👛 Balansingiz: <b>{fmt(bal)} so'm</b>"
        )
        if not enough:
            text += f"\n\n⚠️ Balans yetarli emas. <b>{fmt(need)} so'm</b> qo'shishingiz kerak."
        rows = [[btn("✅ Tasdiqlash", "buy_ok", "success"), btn("❌ Bekor qilish", "menu", "danger")]]
        if not enough:
            rows.append(topup_row)
    elif kind == "premium":
        text = (
            f"✈️ <b>{label}</b>\n\n"
            f"👤 Qabul qiluvchi: <b>{rcp}</b>\n"
            f"👛 Balansingiz: <b>{fmt(bal)} so'm</b>\n"
            f"💲 Narx: <b>{fmt(price)} so'm</b>"
        )
        if enough:
            rows = [[btn("✅ Tasdiqlash", "buy_ok", "success"), back_btn("menu", "❌ Bekor qilish")]]
        else:
            text += (
                f"\n\n❗ Balansingiz yetarli emas!\n💳 Yetishmaydi: <b>{fmt(need)} so'm</b>\n\n"
                "Avval balansni to'ldiring:"
            )
            rows = [topup_row, [back_btn()]]
    else:
        text = (
            "🎁 <b>Gift tasdiqlash</b>\n\n"
            f"👤 Qabul qiluvchi: <b>{rcp}</b>\n"
            f"🎀 {label}\n"
            f"💲 Narxi: <b>{fmt(price)} so'm</b>\n"
            f"👛 Balansingiz: <b>{fmt(bal)} so'm</b>"
        )
        if not enough:
            text += f"\n\n⚠️ <b>Balans yetarli emas!</b>\nKerak: <b>{fmt(need)} so'm</b> qo'shimcha."
        rows = [
            [btn("Ha, yuborish", "buy_ok", "success")],
            [btn("🎁 Boshqa gift", "gift_again")],
        ]
        if not enough:
            rows.append(topup_row)
        rows.append([back_btn()])
    await state.set_state(None)
    await show(event, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "buy_ok")
async def cb_buy_ok(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not all(k in d for k in ("kind", "item", "recipient")):
        await call.answer("Sessiya tugadi. /start bosing.", show_alert=True)
        return
    uid = call.from_user.id
    kind, item, rcp = d["kind"], d["item"], d["recipient"]
    res = do_purchase(uid, kind, item, rcp)
    if res is None:
        await call.answer("❌ Balans yetarli emas!", show_alert=True)
        return
    order_id, label, price = res
    await state.clear()
    await safe_edit(
        call,
        f"✅ <b>Buyurtma qabul qilindi! №{order_id}</b>\n\n"
        f"🛍 {label}\n👤 {rcp}\n💲 {fmt(price)} so'm\n\n"
        "Tez orada bajariladi, tayyor bo'lgach xabar beramiz.",
        kb([back_btn("menu", "🏠 Asosiy menyu")]),
    )
    await call.answer()
    who = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name
    await notify_admin_order(order_id, label, rcp, price, uid, who)


# =====================================================================
#  BALANS TO'LDIRISH
# =====================================================================
@router.callback_query(F.data == "topup")
async def cb_topup(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TopUp.amount)
    await safe_edit(
        call,
        "💳 <b>Balansni to'ldirish</b>\n\nQuyidagi miqdorni kiriting:\n\n"
        f"⬇️ Minimal: <b>{fmt(TOPUP_MIN)} so'm</b>\n⬆️ Maksimal: <b>{fmt(TOPUP_MAX)} so'm</b>",
        kb([back_btn()]),
    )
    await call.answer()


def rand_code():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


@router.message(TopUp.amount, F.text, ~F.text.startswith("/"))
async def msg_topup_amount(message: Message, state: FSMContext):
    digits = re.sub(r"\D", "", message.text)
    if not digits or not (TOPUP_MIN <= int(digits) <= TOPUP_MAX):
        await message.answer(f"❌ Summa {fmt(TOPUP_MIN)} dan {fmt(TOPUP_MAX)} so'mgacha bo'lishi kerak. Masalan: 50000")
        return
    amount = int(digits)
    pid, code, t0, t1 = new_payment(message.from_user.id, amount)
    await state.clear()
    sent = await message.answer(
        "✅ <b>To'lov so'rovi yaratildi!</b>\n\n"
        f"🆔 Buyurtma: <code>{code}</code>\n"
        f"💰 Miqdori: <b>{fmt(amount)} so'm</b>\n\n"
        f"💳 To'lov uchun karta:\n<code>{CARD_NUMBER}</code>\n"
        f"👤 Karta egasi: {html.escape(CARD_OWNER)}\n\n"
        "⚙️ To'lov amalga oshirilgach, quyidagi tugmani bosing.\n\n"
        f"⚠️ Muddat: <b>{t0:%H:%M:%S} — {t1:%H:%M:%S}</b> (Toshkent)\n"
        f"Aniq <b>{PAY_MINUTES} daqiqa</b>. Undan keyin avtomatik bekor qilinadi!",
        reply_markup=kb([btn("✅ To'lovni tekshirish", f"chk_{pid}", "success")],
                        [btn("❌ Bekor qilish", f"cxl_{pid}", "danger")]),
    )
    db.execute("UPDATE payments SET msg_chat=?, msg_id=? WHERE id=?", (sent.chat.id, sent.message_id, pid))
    db.commit()


@router.callback_query(F.data.startswith("chk_"))
async def cb_check(call: CallbackQuery):
    pid = int(call.data[4:])
    who = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name
    res = await check_payment(pid, call.from_user.id, who)
    if res == "notfound":
        await call.answer("To'lov topilmadi", show_alert=True)
    elif res == "checking":
        await call.answer("⏳ So'rov adminga yuborilgan. Tasdiqlanishini kuting.", show_alert=True)
    elif res == "paid":
        await call.answer("✅ Bu to'lov allaqachon tasdiqlangan.", show_alert=True)
    elif res == "expired":
        await safe_edit(call, "⏰ Vaqt tugadi. To'lov bekor qilindi.", kb([back_btn("menu", "🏠 Asosiy menyu")]))
        await call.answer()
    else:
        await safe_edit(
            call,
            "⏳ <b>To'lov tekshirilmoqda...</b>\n\nAdmin tasdiqlashi bilan pul balansingizga tushadi.",
            kb([back_btn("menu", "🏠 Asosiy menyu")]),
        )
        await call.answer("So'rov adminga yuborildi ✅")


@router.callback_query(F.data.startswith("cxl_"))
async def cb_cancel_pay(call: CallbackQuery):
    pid = int(call.data[4:])
    db.execute("UPDATE payments SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'", (pid, call.from_user.id))
    db.commit()
    await safe_edit(call, "❌ To'lov bekor qilindi.", kb([back_btn("menu", "🏠 Asosiy menyu")]))
    await call.answer()


# =====================================================================
#  PROFIL, TOP, YORDAM, AKKAUNT
# =====================================================================
def profile_view(uid):
    bal, spent, private, _, _ = get_user(uid)
    cnt = db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status!='cancelled'", (uid,)).fetchone()[0]
    text = (
        "👤 <b>Profil</b>\n\n"
        f"🪪 User ID: <code>{uid}</code>\n"
        f"├ 💰 Balans: <b>{fmt(bal)} so'm</b>\n"
        f"├ 🧾 Buyurtmalar: <b>{cnt} ta</b>\n"
        f"├ 💸 Sarflangan: <b>{fmt(spent)} so'm</b>\n"
        f"└ 🕶 Maxfiy rejim: {'🔒 Yoqilgan' if private else '🔓 O‘chirilgan'}"
    )
    rows = [
        [btn("🧾 Buyurtmalarim", "myorders")],
        [btn("🏆 TOP foydalanuvchilar", "top")],
        [btn(f"🕶 Maxfiy rejim: {'Yoqilgan' if private else 'O‘chirilgan'}", "priv", "success" if private else None)],
    ]
    if ORDERS_URL:
        rows.append([btn("💬 Buyurtmalar kanali", url=ORDERS_URL)])
    rows.append([back_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    text, markup = profile_view(call.from_user.id)
    await safe_edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "priv")
async def cb_priv(call: CallbackQuery):
    db.execute("UPDATE users SET private=1-COALESCE(private,1) WHERE user_id=?", (call.from_user.id,))
    db.commit()
    text, markup = profile_view(call.from_user.id)
    await safe_edit(call, text, markup)
    await call.answer("Maxfiy rejim o'zgartirildi")


STATUS_ICON = {"pending": "⏳", "done": "✅", "cancelled": "❌"}


@router.callback_query(F.data == "myorders")
async def cb_myorders(call: CallbackQuery):
    rows = db.execute(
        "SELECT id, label, recipient, price, status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (call.from_user.id,),
    ).fetchall()
    if rows:
        lines = [f"{STATUS_ICON.get(r[4], '•')} №{r[0]} — {r[1]} → {r[2]} ({fmt(r[3])} so'm)" for r in rows]
        text = "🧾 <b>Oxirgi buyurtmalaringiz</b>\n\n" + "\n".join(lines)
    else:
        text = "🧾 Sizda hali buyurtmalar yo'q."
    await safe_edit(call, text, kb([back_btn("profile")]))
    await call.answer()


@router.callback_query(F.data == "top")
async def cb_top(call: CallbackQuery):
    rows = db.execute(
        "SELECT user_id, username, full_name, total_spent, private FROM users WHERE total_spent>0 ORDER BY total_spent DESC LIMIT 10"
    ).fetchall()
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, un, fn, spent, priv) in enumerate(rows):
        if priv:
            name = f"Foydalanuvchi •••{str(uid)[-3:]}"
        else:
            name = f"@{un}" if un else html.escape(fn or str(uid))
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {name} — <b>{fmt(spent)} so'm</b>")
    text = "🏆 <b>TOP foydalanuvchilar</b>\n\n" + ("\n".join(lines) if lines else "Hali hech kim xarid qilmagan.")
    await safe_edit(call, text, kb([back_btn("profile")]))
    await call.answer()


@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    prem = "\n".join(f"  • {v[0]} — {fmt(v[1])} so'm" for v in PREMIUM.values())
    text = (
        f"💱 Stars narxi: <b>1 Stars = {STAR_PRICE} so'm</b>\n\n"
        f"✈️ <b>Premium narxlar:</b>\n{prem}\n\n"
        "⭐ <b>Stars olish:</b>\n1. Stars olish tugmasini bosing\n2. Username kiriting\n3. Paketni tanlang va tasdiqlang\n\n"
        "✈️ <b>Premium olish:</b>\n1. Premium olish tugmasini bosing\n2. Username kiriting\n3. Muddatni tanlang va tasdiqlang\n\n"
        "🎁 <b>Gift yuborish:</b>\n1. Gift yuborish tugmasini bosing\n2. Username kiriting\n3. Gift tanlang va tasdiqlang\n\n"
        "❓ Muammo bo'lsa admin bilan bog'laning."
    )
    rows = [[btn("🛟 Qo'llab-quvvatlash", url=f"https://t.me/{SUPPORT_USERNAME}")]]
    if NEWS_URL:
        rows.append([btn("📰 Yangiliklar kanali", url=NEWS_URL)])
    rows.append([back_btn()])
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "account")
async def cb_account(call: CallbackQuery):
    await safe_edit(
        call,
        "🌐 <b>Telegram akkaunt sotib olish</b>\n\n"
        "Akkaunt sotib olish uchun admin bilan bog'laning. Narx va mavjud akkauntlarni admin aytadi.",
        kb([btn("✍️ Adminga yozish", url=f"https://t.me/{SUPPORT_USERNAME}")], [back_btn()]),
    )
    await call.answer()


# =====================================================================
#  UMUMIY MANTIQ (bot ham, mini-sayt ham shuni ishlatadi)
# =====================================================================
def do_purchase(uid, kind, item, rcp):
    """Balansdan yechib, buyurtma yaratadi. -> (order_id, label, price) yoki None (balans yetmasa)"""
    label, price = resolve(kind, item)
    cur = db.execute(
        "UPDATE users SET balance=balance-?, total_spent=total_spent+? WHERE user_id=? AND balance>=?",
        (price, price, uid, price),
    )
    if cur.rowcount == 0:
        db.rollback()
        return None
    cur = db.execute(
        "INSERT INTO orders (user_id, kind, item, label, recipient, price, created_at) VALUES (?,?,?,?,?,?,?)",
        (uid, kind, item, label, rcp, price, now_ts()),
    )
    db.commit()
    return cur.lastrowid, label, price


async def notify_admin_order(order_id, label, rcp, price, uid, who):
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆕 <b>Yangi buyurtma №{order_id}</b>\n\n"
            f"🛍 {label}\n👤 Kimga: <b>{rcp}</b>\n🙋 Kimdan: {html.escape(who)} (<code>{uid}</code>)\n"
            f"💲 {fmt(price)} so'm\n\nBajarib bo'lgach «Bajarildi» ni bosing.",
            reply_markup=kb([btn("✅ Bajarildi", f"od_{order_id}", "success"),
                             btn("❌ Bekor (pul qaytadi)", f"oc_{order_id}", "danger")]),
        )
    except Exception:
        log.exception("admin xabar")


def new_payment(uid, amount):
    """Yangi to'lov so'rovi. -> (payment_id, code, boshlanish, tugash)"""
    db.execute("UPDATE payments SET status='cancelled' WHERE user_id=? AND status='pending'", (uid,))
    t0 = datetime.now(TZ)
    t1 = t0 + timedelta(minutes=PAY_MINUTES)
    code = rand_code()
    cur = db.execute(
        "INSERT INTO payments (code, user_id, amount, created_at, expires_at) VALUES (?,?,?,?,?)",
        (code, uid, amount, int(t0.timestamp()), int(t1.timestamp())),
    )
    db.commit()
    return cur.lastrowid, code, t0, t1


async def check_payment(pid, uid, who):
    """Foydalanuvchi "To'lovni tekshirish" bosdi. -> notfound / checking / paid / expired / sent"""
    row = db.execute("SELECT user_id, amount, status, expires_at, code FROM payments WHERE id=?", (pid,)).fetchone()
    if not row or row[0] != uid:
        return "notfound"
    _, amount, status, exp, code = row
    if status in ("checking", "paid"):
        return status
    if status != "pending" or now_ts() > exp:
        db.execute("UPDATE payments SET status='expired' WHERE id=? AND status='pending'", (pid,))
        db.commit()
        return "expired"
    db.execute("UPDATE payments SET status='checking' WHERE id=?", (pid,))
    db.commit()
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💰 <b>To'lovni tekshiring</b>\n\n🆔 <code>{code}</code> (№{pid})\n"
                f"🙋 {html.escape(who)} (<code>{uid}</code>)\n💵 Summa: <b>{fmt(amount)} so'm</b>\n\n"
                "Kartangizga pul tushganini tekshirib, tasdiqlang:",
                reply_markup=kb([btn("✅ Tasdiqlash", f"pok_{pid}", "success"),
                                 btn("❌ Rad etish", f"pno_{pid}", "danger")]),
            )
        except Exception:
            log.exception("admin xabar")
    return "sent"


# =====================================================================
#  ADMIN: tugmalar
# =====================================================================
def apply_payment(pid):
    """To'lovni tasdiqlaydi. -> (ok, user_id, amount)"""
    row = db.execute("SELECT user_id, amount, status, msg_chat, msg_id FROM payments WHERE id=?", (pid,)).fetchone()
    if not row or row[2] not in ("pending", "checking", "expired"):
        return False, None, None, None
    uid, amount = row[0], row[1]
    db.execute("UPDATE payments SET status='paid' WHERE id=?", (pid,))
    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid))
    db.commit()
    return True, uid, amount, (row[3], row[4])


async def notify_paid(uid, amount):
    try:
        await bot.send_message(
            uid,
            f"✅ <b>Balansingiz to'ldirildi!</b>\n+{fmt(amount)} so'm\n💰 Hozirgi balans: <b>{fmt(get_balance(uid))} so'm</b>",
            reply_markup=main_menu(),
        )
    except Exception:
        log.exception("foydalanuvchiga xabar")


@admin_router.callback_query(F.data.startswith("pok_"))
async def adm_pay_ok(call: CallbackQuery):
    pid = int(call.data[4:])
    ok, uid, amount, _ = apply_payment(pid)
    if not ok:
        await call.answer("Allaqachon ko'rilgan yoki topilmadi", show_alert=True)
        return
    await call.message.edit_text(call.message.html_text + f"\n\n✅ <b>Tasdiqlandi</b> (+{fmt(amount)} so'm)")
    await notify_paid(uid, amount)
    await call.answer("Tasdiqlandi")


@admin_router.callback_query(F.data.startswith("pno_"))
async def adm_pay_no(call: CallbackQuery):
    pid = int(call.data[4:])
    row = db.execute("SELECT user_id, status FROM payments WHERE id=?", (pid,)).fetchone()
    if not row or row[1] not in ("pending", "checking"):
        await call.answer("Allaqachon ko'rilgan yoki topilmadi", show_alert=True)
        return
    db.execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
    db.commit()
    await call.message.edit_text(call.message.html_text + "\n\n❌ <b>Rad etildi</b>")
    try:
        await bot.send_message(row[0], "❌ To'lovingiz tasdiqlanmadi. Muammo bo'lsa adminga yozing.", reply_markup=main_menu())
    except Exception:
        pass
    await call.answer("Rad etildi")


def mask_username(un):
    """raj123456 -> @raj*****"""
    un = (un or "").lstrip("@")
    if not un:
        return None
    keep = 3 if len(un) > 4 else 2 if len(un) > 2 else 1
    return "@" + un[:keep] + "*" * (len(un) - keep)


def channel_chat():
    c = str(ORDERS_CHANNEL_ID).strip()
    return int(c) if c.lstrip("-").isdigit() else c


def channel_text(oid, kind, item, price, private, sender_un, rcp):
    if private:
        frm, to = "🥷 Anonim", "🔒 Anonim"
    else:
        frm = mask_username(sender_un) or "Foydalanuvchi"
        to = mask_username(rcp) or "Foydalanuvchi"
    if kind == "gift":
        emoji, _, name = GIFTS.get(item, ("🎁 " + item,))[0].partition(" ")
        head = "✅ <b>Gift buyurtma bajarildi!</b>"
        what = f"{emoji} Gift: <b>{html.escape(name or item)}</b>"
    elif kind == "premium":
        head = "✅ <b>Yangi buyurtma bajarildi!</b>\n\n✈️ <b>Premium buyurtma</b>"
        what = f"✈️ Premium: <b>{html.escape(PREMIUM.get(item, (item,))[0])}</b>"
    else:
        head = "✅ <b>Yangi buyurtma bajarildi!</b>\n\n⭐ <b>Stars buyurtma</b>"
        what = f"⭐ Stars: <b>{html.escape(str(item))}</b>"
    return (
        f"{head}\n"
        "━━━━━━━━━━━━━━\n"
        f"📦 Buyurtma: <b>#{oid}</b>\n"
        f"👤 Kimdan: <b>{frm}</b>\n"
        f"🎯 Kimga: <b>{to}</b>\n"
        f"{what}\n"
        f"💰 To'lov: <b>{fmt(price)} so'm</b>\n"
        f"📅 {datetime.now(TZ):%Y-%m-%d %H:%M:%S}"
    )


async def send_to_channel(text):
    markup = kb([btn("🤖 Botga o'tish", url=f"https://t.me/{BOT_USERNAME}")]) if BOT_USERNAME else None
    await bot.send_message(channel_chat(), text, reply_markup=markup)


async def post_to_channel(order_id):
    """Bajarilgan buyurtmani kanalga yuboradi (foydalanuvchilar yashirin)."""
    if not ORDERS_CHANNEL_ID:
        return
    row = db.execute("SELECT user_id, kind, item, recipient, price FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return
    uid, kind, item, rcp, price = row
    try:
        u = get_user(uid)  # balance, spent, private, username, full_name
        text = channel_text(order_id, kind, item, price, bool(u[2]), u[3], rcp)
        await send_to_channel(text)
    except Exception as e:
        log.exception("kanalga yuborish")
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Buyurtma №{order_id} kanalga yuborilmadi: <code>{html.escape(str(e))}</code>\n"
                    "Botni kanalga ADMIN qilib qo'shing (xabar yuborish huquqi bilan).",
                )
            except Exception:
                pass


@admin_router.message(Command("testchannel"))
async def adm_testchannel(message: Message):
    text = channel_text(0, "gift", "bear", 3000, False, "testuser123", "@receiver456")
    await message.answer(f"🔎 Kanal holati: {await check_channel()}")
    try:
        await send_to_channel(text)
        await message.answer("✅ Kanalga test xabar yuborildi.")
    except Exception as e:
        await message.answer(
            f"❌ Yuborib bo'lmadi: <code>{html.escape(str(e))}</code>\n\n"
            "Botni kanalga ADMIN qilib qo'shing va «Xabar yuborish» huquqini bering."
        )


BOT_VERSION = "v5-kanal"


async def check_channel():
    """Kanalga ulanishni tekshiradi. -> matn"""
    if not ORDERS_CHANNEL_ID:
        return "sozlanmagan (ORDERS_CHANNEL_ID bo'sh)"
    try:
        chat = await bot.get_chat(channel_chat())
        me = await bot.get_me()
        m = await bot.get_chat_member(chat.id, me.id)
        if m.status not in ("administrator", "creator"):
            return f"❌ bot kanalda admin emas (holati: {m.status})"
        if m.status == "administrator" and getattr(m, "can_post_messages", True) is False:
            return "❌ botda «Xabar yuborish» huquqi yo'q"
        return f"✅ ulangan ({html.escape(chat.title or '')})"
    except Exception as e:
        return f"❌ {html.escape(str(e))}\nKanal: <code>{html.escape(str(ORDERS_CHANNEL_ID))}</code>"


async def startup_report():
    if not ADMIN_ID:
        return
    ch = await check_channel()
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🟢 <b>Bot ishga tushdi</b> ({BOT_VERSION})\n📢 Buyurtmalar kanali: {ch}",
            disable_notification=True,
        )
    except Exception:
        log.exception("startup_report")


async def finish_order(order_id):
    row = db.execute("SELECT user_id, label, price, status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row[3] != "pending":
        return None
    db.execute("UPDATE orders SET status='done' WHERE id=?", (order_id,))
    db.commit()
    uid, label, price, _ = row
    try:
        await bot.send_message(uid, f"🎉 <b>Buyurtmangiz №{order_id} bajarildi!</b>\n{label} yuborildi.", reply_markup=main_menu())
    except Exception:
        pass
    await post_to_channel(order_id)
    return label


async def cancel_order(order_id):
    row = db.execute("SELECT user_id, price, status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row[2] != "pending":
        return False
    uid, price, _ = row
    db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
    db.execute("UPDATE users SET balance=balance+?, total_spent=MAX(total_spent-?,0) WHERE user_id=?", (price, price, uid))
    db.commit()
    try:
        await bot.send_message(uid, f"❌ Buyurtma №{order_id} bekor qilindi. {fmt(price)} so'm balansingizga qaytarildi.", reply_markup=main_menu())
    except Exception:
        pass
    return True


@admin_router.callback_query(F.data.startswith("od_"))
async def adm_order_done(call: CallbackQuery):
    oid = int(call.data[3:])
    if await finish_order(oid) is None:
        await call.answer("Allaqachon ko'rilgan", show_alert=True)
        return
    await call.message.edit_text(call.message.html_text + "\n\n✅ <b>Bajarildi</b>")
    await call.answer("Bajarildi")


@admin_router.callback_query(F.data.startswith("oc_"))
async def adm_order_cancel(call: CallbackQuery):
    oid = int(call.data[3:])
    if not await cancel_order(oid):
        await call.answer("Allaqachon ko'rilgan", show_alert=True)
        return
    await call.message.edit_text(call.message.html_text + "\n\n❌ <b>Bekor qilindi, pul qaytarildi</b>")
    await call.answer("Bekor qilindi")


# =====================================================================
#  ADMIN: komandalar
# =====================================================================
def arg(message: Message, i=1):
    parts = (message.text or "").split()
    return parts[i] if len(parts) > i else None


@admin_router.message(Command("admin"))
async def adm_panel(message: Message):
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    bal = db.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
    pend = db.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
    done = db.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE status='done'").fetchone()
    await message.answer(
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Foydalanuvchilar: {users}\n💰 Jami balanslar: {fmt(bal)} so'm\n"
        f"⏳ Kutilayotgan buyurtmalar: {pend}\n✅ Bajarilgan: {done[0]} ta ({fmt(done[1])} so'm)\n\n"
        "<b>Komandalar:</b>\n"
        "/orders — kutilayotgan buyurtmalar\n"
        "/users — barcha foydalanuvchilar ro'yxati\n"
        "/testchannel — kanalga test xabar yuborish\n"
        "/addbalance &lt;user_id&gt; &lt;summa&gt; — balans qo'shish (minus ham bo'ladi)\n"
        "/confirm &lt;id&gt; · /reject &lt;id&gt; — to'lovni tasdiqlash/rad etish\n"
        "/done &lt;id&gt; — buyurtmani bajarildi qilish\n"
        "/broadcast &lt;matn&gt; — hammaga xabar\n"
        "/backup — bazani hozir zaxiralash\n"
        "/restore — zaxira faylga reply qilib yozing"
    )


@admin_router.message(Command("users"))
async def adm_users(message: Message):
    rows = db.execute(
        "SELECT user_id, username, full_name, balance, created_at FROM users ORDER BY created_at DESC, user_id DESC"
    ).fetchall()
    total = len(rows)
    if not total:
        await message.answer("Hali foydalanuvchi yo'q.")
        return
    head = f"👥 <b>Foydalanuvchilar: {total}</b>\n\n"
    chunk = head
    for i, (uid, un, fn, bal, ts) in enumerate(rows):
        name = f"@{un}" if un else html.escape(fn or "username yo'q")
        when = datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M") if ts else "—"
        line = f"{total - i}. {name} — <code>{uid}</code>\n    💰 {fmt(bal)} so'm · {when}\n"
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line
    if chunk:
        await message.answer(chunk)


@admin_router.message(Command("orders"))
async def adm_orders(message: Message):
    rows = db.execute("SELECT id, label, recipient, price FROM orders WHERE status='pending' ORDER BY id").fetchall()
    if not rows:
        await message.answer("Kutilayotgan buyurtma yo'q ✅")
        return
    for oid, label, rcp, price in rows:
        await message.answer(
            f"⏳ <b>Buyurtma №{oid}</b>\n🛍 {label}\n👤 {rcp}\n💲 {fmt(price)} so'm",
            reply_markup=kb([btn("✅ Bajarildi", f"od_{oid}", "success"), btn("❌ Bekor", f"oc_{oid}", "danger")]),
        )


@admin_router.message(Command("addbalance"))
async def adm_addbalance(message: Message):
    try:
        uid, amount = int(arg(message, 1)), int(arg(message, 2))
    except Exception:
        await message.answer("Ishlatish: /addbalance 123456789 50000")
        return
    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid))
    db.commit()
    await message.answer(f"✅ Balans o'zgardi. Hozir: {fmt(get_balance(uid))} so'm")
    if amount > 0:
        await notify_paid(uid, amount)


@admin_router.message(Command("confirm"))
async def adm_confirm(message: Message):
    try:
        pid = int(arg(message))
    except Exception:
        await message.answer("Ishlatish: /confirm 1")
        return
    ok, uid, amount, _ = apply_payment(pid)
    if not ok:
        await message.answer("To'lov topilmadi yoki allaqachon ko'rilgan.")
        return
    await message.answer(f"✅ №{pid} tasdiqlandi. +{fmt(amount)} so'm")
    await notify_paid(uid, amount)


@admin_router.message(Command("reject"))
async def adm_reject(message: Message):
    try:
        pid = int(arg(message))
    except Exception:
        await message.answer("Ishlatish: /reject 1")
        return
    db.execute("UPDATE payments SET status='rejected' WHERE id=? AND status IN ('pending','checking')", (pid,))
    db.commit()
    await message.answer(f"❌ To'lov №{pid} rad etildi.")


@admin_router.message(Command("done"))
async def adm_done(message: Message):
    try:
        oid = int(arg(message))
    except Exception:
        await message.answer("Ishlatish: /done 1")
        return
    label = await finish_order(oid)
    await message.answer(f"✅ Buyurtma №{oid} bajarildi." if label else "Buyurtma topilmadi yoki allaqachon ko'rilgan.")


@admin_router.message(Command("broadcast"))
async def adm_broadcast(message: Message):
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Ishlatish: /broadcast Matn")
        return
    ids = [r[0] for r in db.execute("SELECT user_id FROM users").fetchall()]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await message.answer(f"📣 Yuborildi: {ok}/{len(ids)}")


# =====================================================================
#  ZAXIRA (Render free diski o'chib ketadi - baza Telegramda saqlanadi)
# =====================================================================
async def do_backup(force=False):
    global last_changes, last_backup_msg
    if not ADMIN_ID:
        return False
    if not force and db.total_changes == last_changes:
        return False
    tmp = "backup_tmp.db"
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    db.backup(dst)
    dst.close()
    msg = await bot.send_document(
        ADMIN_ID,
        FSInputFile(tmp, filename="bot_backup.db"),
        caption=f"💾 Zaxira nusxa • {datetime.now(TZ):%d.%m.%Y %H:%M}",
        disable_notification=True,
    )
    try:
        if last_backup_msg:
            try:
                await bot.unpin_chat_message(ADMIN_ID, message_id=last_backup_msg)
            except Exception:
                pass
        await bot.pin_chat_message(ADMIN_ID, msg.message_id, disable_notification=True)
        if last_backup_msg:
            try:
                await bot.delete_message(ADMIN_ID, last_backup_msg)
            except Exception:
                pass
    except Exception:
        log.exception("pin")
    last_backup_msg = msg.message_id
    last_changes = db.total_changes
    return True


async def try_restore():
    """Baza fayli yo'q bo'lsa - adminning qadalgan zaxira xabaridan tiklaydi."""
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
        return None
    if not ADMIN_ID:
        return None
    try:
        chat = await bot.get_chat(ADMIN_ID)
        pm = getattr(chat, "pinned_message", None)
        if pm and pm.document and pm.document.file_name == "bot_backup.db":
            await bot.download(pm.document, destination=DB_PATH)
            log.info("Baza zaxiradan tiklandi")
            return pm.message_id
    except Exception:
        log.exception("tiklash")
    return None


@admin_router.message(Command("backup"))
async def adm_backup(message: Message):
    await do_backup(force=True)
    await message.answer("💾 Zaxira yuborildi va qadab qo'yildi.")


@admin_router.message(Command("restore"))
async def adm_restore(message: Message):
    global db
    doc = message.reply_to_message.document if message.reply_to_message else None
    if not doc:
        await message.answer("Zaxira fayl (bot_backup.db) xabariga reply qilib /restore deb yozing.")
        return
    tmp = "restore_tmp.db"
    await bot.download(doc, destination=tmp)
    try:
        t = sqlite3.connect(tmp)
        t.execute("SELECT COUNT(*) FROM users")
        t.close()
    except Exception:
        await message.answer("❌ Bu fayl to'g'ri baza emas.")
        return
    db.close()
    shutil.copy(tmp, DB_PATH)
    db = open_db()
    await message.answer("✅ Baza tiklandi.")


# =====================================================================
#  FON VAZIFALARI
# =====================================================================
async def expiry_loop():
    while True:
        try:
            rows = db.execute(
                "SELECT id, msg_chat, msg_id FROM payments WHERE status='pending' AND expires_at<?", (now_ts(),)
            ).fetchall()
            for pid, chat, mid in rows:
                db.execute("UPDATE payments SET status='expired' WHERE id=? AND status='pending'", (pid,))
                db.commit()
                if chat and mid:
                    try:
                        await bot.edit_message_text(
                            "⏰ Vaqt tugadi. To'lov bekor qilindi.", chat_id=chat, message_id=mid,
                            reply_markup=kb([back_btn("menu", "🏠 Asosiy menyu")]),
                        )
                    except Exception:
                        pass
        except Exception:
            log.exception("expiry_loop")
        await asyncio.sleep(20)


async def backup_loop():
    while True:
        await asyncio.sleep(600)
        try:
            await do_backup()
        except Exception:
            log.exception("backup_loop")


async def keepalive():
    """Render free uxlab qolmasligi uchun o'ziga har 5 daqiqada so'rov yuboradi."""
    if not BASE_URL:
        return
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        while True:
            await asyncio.sleep(300)
            try:
                await sess.get(BASE_URL + "/health")
            except Exception:
                pass


# =====================================================================
#  WEB SERVER (Render port talab qiladi) + WEB ILOVA
# =====================================================================
def webapp_page():
    css = (
        "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:18px;"
        "background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#111)}"
        "h2{margin:18px 0 8px}.row{display:flex;justify-content:space-between;padding:10px 12px;margin:6px 0;"
        "border-radius:10px;background:var(--tg-theme-secondary-bg-color,#f1f1f1)}"
        "button{width:100%;padding:14px;margin-top:20px;border:0;border-radius:12px;font-size:16px;"
        "background:var(--tg-theme-button-color,#2481cc);color:var(--tg-theme-button-text-color,#fff)}"
    )
    parts = [f"<h2>⭐ Stars</h2><div class='row'><span>1 Stars</span><b>{fmt(STAR_PRICE)} so'm</b></div>"]
    parts.append("<h2>✈️ Premium</h2>")
    parts += [f"<div class='row'><span>{v[0]}</span><b>{fmt(v[1])} so'm</b></div>" for v in PREMIUM.values()]
    parts.append("<h2>🎁 Gift</h2>")
    parts += [f"<div class='row'><span>{v[0]}</span><b>{fmt(v[1])} so'm</b></div>" for v in GIFTS.values()]
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<script src='https://telegram.org/js/telegram-web-app.js'></script>"
        f"<style>{css}</style></head><body><h1>Narxlar</h1>{''.join(parts)}"
        "<button onclick='Telegram.WebApp.close()'>Botga qaytish</button>"
        "<script>try{Telegram.WebApp.ready();Telegram.WebApp.expand()}catch(e){}</script></body></html>"
    )


class ApiError(Exception):
    pass


def verify_init_data(init_data):
    """Telegram Web App imzosini tekshiradi (boshqa odam yolg'on ID yubora olmasligi uchun)."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv):
            return None
        if now_ts() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def api_state(user):
    bal, spent, priv, _, _ = get_user(user.id)
    cnt = db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status!='cancelled'", (user.id,)).fetchone()[0]
    orders = [
        {"id": r[0], "label": r[1], "recipient": r[2], "price": r[3], "status": r[4]}
        for r in db.execute(
            "SELECT id, label, recipient, price, status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 15", (user.id,)
        ).fetchall()
    ]
    top = []
    for uid, un, fn, sp, pr in db.execute(
        "SELECT user_id, username, full_name, total_spent, private FROM users WHERE total_spent>0 ORDER BY total_spent DESC LIMIT 10"
    ).fetchall():
        name = f"Foydalanuvchi •••{str(uid)[-3:]}" if pr else (f"@{un}" if un else (fn or str(uid)))
        top.append({"name": name, "spent": sp, "me": uid == user.id})
    pay = None
    p = db.execute(
        "SELECT id, code, amount, status, expires_at FROM payments WHERE user_id=? "
        "AND (status='checking' OR (status='pending' AND expires_at>?)) ORDER BY id DESC LIMIT 1",
        (user.id, now_ts()),
    ).fetchone()
    if p:
        pay = {"id": p[0], "code": p[1], "amount": p[2], "status": p[3], "expires_at": p[4],
               "card": CARD_NUMBER, "owner": CARD_OWNER}
    return {
        "user": {"id": user.id, "name": user.full_name, "username": user.username or "",
                 "balance": bal, "spent": spent, "private": bool(priv), "orders": cnt},
        "prices": {
            "star_price": STAR_PRICE, "stars_min": STARS_MIN, "stars_max": STARS_MAX,
            "premium": [{"key": k, "label": v[0], "price": v[1]} for k, v in PREMIUM.items()],
            "gifts": [{"key": k, "label": v[0], "price": v[1]} for k, v in GIFTS.items()],
        },
        "topup": {"min": TOPUP_MIN, "max": TOPUP_MAX},
        "support": SUPPORT_USERNAME,
        "orders": orders, "top": top, "payment": pay,
    }


def api(handler):
    async def wrapper(request):
        u = verify_init_data(request.headers.get("X-Init-Data", ""))
        if not u:
            return web.json_response({"error": "Bu sahifani Telegram bot ichidan oching."}, status=401)
        full = (u.get("first_name", "") + " " + u.get("last_name", "")).strip()
        user = SimpleNamespace(id=u["id"], username=u.get("username", ""), full_name=full)
        if ensure_user(user):
            await notify_new_user(user)
        body = {}
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                body = {}
        try:
            return web.json_response(await handler(user, body))
        except ApiError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception:
            log.exception("api xato")
            return web.json_response({"error": "Server xatosi, qayta urining"}, status=500)
    return wrapper


def who_of(user):
    return f"@{user.username}" if user.username else user.full_name


@api
async def api_get_state(user, body):
    return api_state(user)


@api
async def api_buy(user, body):
    kind = body.get("kind")
    item = str(body.get("item", ""))
    rcp = str(body.get("recipient", "")).strip()
    if kind not in ("stars", "premium", "gift"):
        raise ApiError("Noto'g'ri mahsulot turi")
    if not USERNAME_RE.fullmatch(rcp):
        raise ApiError("Username noto'g'ri. Masalan: @azatrdv")
    rcp = "@" + rcp.lstrip("@")
    if kind == "stars":
        if not item.isdigit() or not (STARS_MIN <= int(item) <= STARS_MAX):
            raise ApiError(f"Stars miqdori {STARS_MIN} dan {STARS_MAX} gacha bo'lishi kerak")
        item = str(int(item))
    elif (kind == "premium" and item not in PREMIUM) or (kind == "gift" and item not in GIFTS):
        raise ApiError("Mahsulot topilmadi")
    res = do_purchase(user.id, kind, item, rcp)
    if res is None:
        raise ApiError("Balans yetarli emas")
    oid, label, price = res
    await notify_admin_order(oid, label, rcp, price, user.id, who_of(user))
    try:
        await bot.send_message(user.id, f"✅ <b>Buyurtma qabul qilindi! №{oid}</b>\n\n🛍 {label}\n👤 {rcp}\n💲 {fmt(price)} so'm")
    except Exception:
        pass
    st = api_state(user)
    st["order"] = {"id": oid, "label": label, "recipient": rcp, "price": price}
    return st


@api
async def api_topup(user, body):
    try:
        amount = int(body.get("amount"))
    except Exception:
        raise ApiError("Summani raqam bilan kiriting")
    if not (TOPUP_MIN <= amount <= TOPUP_MAX):
        raise ApiError(f"Summa {fmt(TOPUP_MIN)} dan {fmt(TOPUP_MAX)} so'mgacha bo'lishi kerak")
    new_payment(user.id, amount)
    return api_state(user)


@api
async def api_check(user, body):
    res = await check_payment(int(body.get("id", 0)), user.id, who_of(user))
    st = api_state(user)
    st["result"] = res
    return st


@api
async def api_cancel(user, body):
    db.execute("UPDATE payments SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",
               (int(body.get("id", 0)), user.id))
    db.commit()
    return api_state(user)


@api
async def api_private(user, body):
    db.execute("UPDATE users SET private=1-COALESCE(private,1) WHERE user_id=?", (user.id,))
    db.commit()
    return api_state(user)


APP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp.html")


async def h_health(request):
    return web.Response(text="Bot ishlayapti ✅")


async def h_app(request):
    try:
        with open(APP_FILE, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        text = webapp_page()  # webapp.html topilmasa - oddiy narxlar sahifasi
    return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def start_web():
    app = web.Application()
    app.router.add_get("/", h_health)
    app.router.add_get("/health", h_health)
    app.router.add_get("/app", h_app)
    app.router.add_get("/api/state", api_get_state)
    app.router.add_post("/api/buy", api_buy)
    app.router.add_post("/api/topup", api_topup)
    app.router.add_post("/api/check", api_check)
    app.router.add_post("/api/cancel", api_cancel)
    app.router.add_post("/api/private", api_private)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Web server %s portda ishga tushdi", PORT)


async def main():
    global db, last_backup_msg, last_changes, BOT_USERNAME
    await start_web()  # birinchi bo'lib port ochiladi - Render shuni kutadi
    await bot.delete_webhook(drop_pending_updates=True)
    last_backup_msg = await try_restore()
    db = open_db()
    last_changes = db.total_changes
    if WEBAPP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Do'kon", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception:
            log.exception("menu button")
    asyncio.create_task(expiry_loop())
    asyncio.create_task(backup_loop())
    asyncio.create_task(keepalive())
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    asyncio.create_task(startup_report())
    log.info("Bot ishga tushdi: @%s", me.username)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
