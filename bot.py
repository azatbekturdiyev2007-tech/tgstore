
import asyncio
import sqlite3
import os
from datetime import datetime, timedelta
try:
    from dotenv import load_dotenv
    load_dotenv()
except: pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi! Render -> Environment da BOT_TOKEN ni qo'y")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
CARD_NUMBER = os.getenv("CARD_NUMBER", "8600 1234 5678 9012")
CARD_OWNER = os.getenv("CARD_OWNER", "Ism Familiya")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------- DATABASE ----------
con = sqlite3.connect("bot.db", check_same_thread=False)
cur = con.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
    balance INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    amount INTEGER, status TEXT DEFAULT 'pending',
    expires_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    type TEXT, item TEXT, recipient TEXT, price INTEGER,
    status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")
con.commit()

def get_or_create_user(user_id, username, full_name):
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (user_id, username, full_name) VALUES (?,?,?)", (user_id, username, full_name))
        con.commit()
        return 0
    return row[0]

def get_balance(user_id):
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    return r[0] if r else 0

def fmt(n):
    return f"{n:,}".replace(",", " ")

# ---------- NARXLAR - SHU YERDAN O'ZGARTIRASAN ----------
PREMIUM_PRICES = {
    "premium_3m": {"label": "3 oy", "price": 285_000},
    "premium_6m": {"label": "6 oy", "price": 450_000},
    "premium_12m": {"label": "12 oy", "price": 750_000},
}
STARS_PRICES = {
    "stars_50": {"label": "50 ⭐", "price": 15_000},
    "stars_100": {"label": "100 ⭐", "price": 28_000},
    "stars_500": {"label": "500 ⭐", "price": 135_000},
}
GIFT_PRICES = {
    "gift_1": {"label": "NFT Gift - Oddiy", "price": 35_000},
    "gift_2": {"label": "NFT Gift - Premium", "price": 85_000},
}

# ---------- FSM ----------
class TopUp(StatesGroup):
    waiting_amount = State()

class BuyFlow(StatesGroup):
    choosing_plan = State()
    waiting_recipient = State()
    confirming = State()

# ---------- KEYBOARDS ----------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars olish", callback_data="menu_stars")],
        [InlineKeyboardButton(text="🎁 Gift olish", callback_data="menu_gift")],
        [InlineKeyboardButton(text="⭐️ Premium olish", callback_data="menu_premium")],
        [InlineKeyboardButton(text="💳 Balansni to'ldirish", callback_data="menu_topup")],
        [InlineKeyboardButton(text="👤 Profil", callback_data="menu_profile"),
         InlineKeyboardButton(text="ℹ️ Yordam", callback_data="menu_help")],
    ])

def back_to_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="back_menu")]
    ])

def build_plan_keyboard(prefix, prices):
    btns = []
    for key, v in prices.items():
        btns.append([InlineKeyboardButton(text=f"{v['label']} — {fmt(v['price'])} so'm", callback_data=f"buy_{prefix}_{key}")])
    btns.append([InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

# ---------- START ----------
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer(
        f"Assalomu alaykum, {message.from_user.first_name}! 👋\n\n"
        "Bu bot orqali ⭐ Stars, 🎁 NFT Gifts va ⭐️ Telegram Premium sotib olishingiz mumkin.\n\n"
        "Boshlash uchun menyudan tanlang:",
        reply_markup=main_menu()
    )

@router.callback_query(F.data == "back_menu")
async def back_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Asosiy menyu 👇", reply_markup=main_menu())
    await call.answer()

@router.callback_query(F.data == "menu_profile")
async def profile(call: CallbackQuery):
    bal = get_balance(call.from_user.id)
    await call.message.edit_text(
        f"👤 <b>Profilingiz</b>\n\n🆔 ID: <code>{call.from_user.id}</code>\n👤 Username: @{call.from_user.username or 'yo‘q'}\n💰 Balans: <b>{fmt(bal)} so'm</b>",
        reply_markup=back_to_menu(), parse_mode="HTML"
    )

@router.callback_query(F.data == "menu_help")
async def help_handler(call: CallbackQuery):
    await call.message.edit_text(
        "ℹ️ <b>Yordam</b>\n\n1. Avval 💳 Balansni to'ldiring.\n2. Kerakli bo'limni tanlang: Stars / Gift / Premium.\n3. Qabul qiluvchining @username ni kiriting.\n4. To'lov balansdan yechiladi.\n\nSavollar bo'lsa adminga yozing.",
        reply_markup=back_to_menu(), parse_mode="HTML"
    )

# ---------- UMUMIY SOTIB OLISH OQIMI - Stars / Gift / Premium ----------
@router.callback_query(F.data.in_(["menu_stars", "menu_gift", "menu_premium"]))
async def shop_menu(call: CallbackQuery, state: FSMContext):
    mapping = {
        "menu_stars": ("stars", STARS_PRICES, "⭐ Stars olish"),
        "menu_gift": ("gift", GIFT_PRICES, "🎁 Gift olish"),
        "menu_premium": ("premium", PREMIUM_PRICES, "⭐️ Premium olish"),
    }
    kind, prices, title = mapping[call.data]
    await state.set_state(BuyFlow.choosing_plan)
    await state.update_data(kind=kind)
    bal = get_balance(call.from_user.id)
    await call.message.edit_text(
        f"{title}\n\n💰 Balansingiz: <b>{fmt(bal)} so'm</b>\n\nKerakli tarifni tanlang:",
        reply_markup=build_plan_keyboard(kind, prices), parse_mode="HTML"
    )

@router.callback_query(BuyFlow.choosing_plan, F.data.startswith("buy_"))
async def choose_plan(call: CallbackQuery, state: FSMContext):
    # buy_premium_premium_3m -> premium_3m
    parts = call.data.split("_", 2)  # ['buy','premium','premium_3m']
    kind = parts[1]
    plan_key = parts[2]
    prices = {"premium": PREMIUM_PRICES, "stars": STARS_PRICES, "gift": GIFT_PRICES}[kind]
    if plan_key not in prices:
        await call.answer("Xatolik, qayta urining", show_alert=True)
        return
    await state.update_data(plan=plan_key, kind=kind)
    await state.set_state(BuyFlow.waiting_recipient)
    price = prices[plan_key]["price"]
    await call.message.edit_text(
        f"Tanladingiz: <b>{prices[plan_key]['label']} — {fmt(price)} so'm</b>\n\n"
        "Kimga yuborilsin? O'zingiz uchun bo'lsa o'zingizning, do'stingiz uchun bo'lsa uning @username ni yuboring.\n\nMasalan: <code>@durov</code>",
        reply_markup=back_to_menu(), parse_mode="HTML"
    )

@router.message(BuyFlow.waiting_recipient)
async def get_recipient(message: Message, state: FSMContext):
    username = message.text.strip()
    if not username.startswith("@") or len(username) < 5:
        await message.answer("❌ Iltimos, to'g'ri @username kiriting. Masalan: @durov")
        return
    data = await state.get_data()
    kind, plan_key = data["kind"], data["plan"]
    prices = {"premium": PREMIUM_PRICES, "stars": STARS_PRICES, "gift": GIFT_PRICES}[kind]
    plan = prices[plan_key]
    balance = get_balance(message.from_user.id)
    await state.update_data(recipient=username)

    if balance < plan["price"]:
        need = plan["price"] - balance
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Balansni to'ldirish", callback_data="menu_topup")],
            [InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="back_menu")]
        ])
        await message.answer(
            f"❌ <b>Balans yetarli emas!</b>\n\nKerak: <b>{fmt(plan['price'])} so'm</b>\nSizda: <b>{fmt(balance)} so'm</b>\nYetishmayapti: <b>{fmt(need)} so'm</b>\n\nIltimos, avval balansni to'ldiring.",
            reply_markup=kb, parse_mode="HTML"
        )
        await state.clear()
        return

    await state.set_state(BuyFlow.confirming)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="confirm_buy")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="back_menu")]
    ])
    await message.answer(
        f"🧾 <b>Buyurtmani tasdiqlang</b>\n\nMahsulot: {plan['label']}\nQabul qiluvchi: {username}\nNarxi: {fmt(plan['price'])} so'm\nBalansdan keyin qoladi: {fmt(balance - plan['price'])} so'm",
        reply_markup=kb, parse_mode="HTML"
    )

@router.callback_query(BuyFlow.confirming, F.data == "confirm_buy")
async def confirm_buy(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    kind, plan_key = data["kind"], data["plan"]
    prices = {"premium": PREMIUM_PRICES, "stars": STARS_PRICES, "gift": GIFT_PRICES}[kind]
    plan = prices[plan_key]
    user_id = call.from_user.id
    cur.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?", (plan["price"], plan["price"], user_id))
    cur.execute("INSERT INTO orders (user_id, type, item, recipient, price, status) VALUES (?,?,?,?,?, 'pending')", (user_id, kind, plan_key, data["recipient"], plan["price"]))
    con.commit()
    order_id = cur.lastrowid
    await state.clear()
    await call.message.edit_text(f"✅ <b>Buyurtma qabul qilindi! №{order_id}</b>\n\n{plan['label']} -> {data['recipient']}\nAdmin tasdiqlagach xabar beramiz.", reply_markup=main_menu(), parse_mode="HTML")
    await bot.send_message(ADMIN_ID, f"🆕 Yangi buyurtma №{order_id}\nTuri: {kind}\nKimdan: @{call.from_user.username} ({user_id})\nKimga: {data['recipient']}\nMahsulot: {plan['label']} - {fmt(plan['price'])} so'm\n\nTasdiqlash uchun: /done {order_id}")

# ---------- BALANS TO'LDIRISH ----------
@router.callback_query(F.data == "menu_topup")
async def topup_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.waiting_amount)
    await call.message.edit_text("💳 <b>Balansni to'ldirish</b>\n\nSummani kiriting (so'mda).\nLimit: <b>1 000</b> dan <b>2 500 000</b> so'mgacha.\n\nMasalan: <code>50000</code>", reply_markup=back_to_menu(), parse_mode="HTML")

@router.message(TopUp.waiting_amount)
async def topup_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.replace(" ", "").replace(",", ""))
    except ValueError:
        await message.answer("❌ Iltimos, faqat raqam kiriting. Masalan: 50000")
        return
    if amount < 1000 or amount > 2_500_000:
        await message.answer("❌ Summa 1 000 dan 2 500 000 so'mgacha bo'lishi kerak.")
        return
    expires = datetime.now() + timedelta(minutes=5)
    cur.execute("INSERT INTO payments (user_id, amount, expires_at) VALUES (?,?,?)", (message.from_user.id, amount, expires))
    con.commit()
    payment_id = cur.lastrowid
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ To'lovni tekshirish", callback_data=f"check_pay_{payment_id}")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cancel_pay_{payment_id}")]
    ])
    await message.answer(
        f"🧾 <b>To'lov №{payment_id}</b>\n\nSumma: <b>{fmt(amount)} so'm</b>\nKarta: <code>{CARD_NUMBER}</code>\nEga: {CARD_OWNER}\n\n⏳ To'lov uchun <b>5 daqiqa</b> vaqtingiz bor.\nTo'lovni qilib, <b>«To'lovni tekshirish»</b> ni bosing.",
        reply_markup=kb, parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("check_pay_"))
async def check_pay(call: CallbackQuery):
    pay_id = int(call.data.split("_")[-1])
    cur.execute("SELECT amount, status, expires_at, user_id FROM payments WHERE id=?", (pay_id,))
    row = cur.fetchone()
    if not row:
        await call.answer("To'lov topilmadi", show_alert=True); return
    amount, status, expires_at, uid = row
    if status != "pending":
        await call.answer(f"Bu to'lov allaqachon: {status}", show_alert=True); return
    if datetime.now() > datetime.fromisoformat(expires_at):
        cur.execute("UPDATE payments SET status='expired' WHERE id=?", (pay_id,)); con.commit()
        await call.message.edit_text("⏰ Vaqt tugadi. To'lov bekor qilindi.", reply_markup=main_menu()); return
    await bot.send_message(ADMIN_ID, f"💰 To'lovni tekshirish №{pay_id}\nFoydalanuvchi: @{call.from_user.username} ({uid})\nSumma: {fmt(amount)} so'm\n\nTasdiqlash uchun: /confirm {pay_id}\nBekor qilish: /reject {pay_id}")
    await call.answer("So'rov adminga yuborildi. Tasdiqlanishi kutilmoqda...", show_alert=True)

@router.callback_query(F.data.startswith("cancel_pay_"))
async def cancel_pay(call: CallbackQuery):
    pay_id = int(call.data.split("_")[-1])
    cur.execute("UPDATE payments SET status='cancelled' WHERE id=?", (pay_id,)); con.commit()
    await call.message.edit_text("❌ To'lov bekor qilindi.", reply_markup=main_menu())

# ---------- ADMIN KOMANDALARI ----------
@router.message(Command("confirm"))
async def admin_confirm(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Sen admin emassan."); return
    try:
        pay_id = int(message.text.split()[1])
    except:
        await message.answer("Ishlatish: /confirm 1"); return
    cur.execute("SELECT user_id, amount, status FROM payments WHERE id=?", (pay_id,))
    row = cur.fetchone()
    if not row or row[2] != "pending":
        await message.answer("To'lov topilmadi yoki allaqachon ko'rilgan."); return
    uid, amount, _ = row
    cur.execute("UPDATE payments SET status='paid' WHERE id=?", (pay_id,))
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, uid))
    con.commit()
    await message.answer(f"✅ To'lov №{pay_id} tasdiqlandi. {fmt(amount)} so'm qo'shildi.")
    await bot.send_message(uid, f"✅ Balansingiz to'ldirildi!\n+{fmt(amount)} so'm\nHozirgi balans: {fmt(get_balance(uid))} so'm", reply_markup=main_menu())

@router.message(Command("reject"))
async def admin_reject(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Sen admin emassan."); return
    try:
        pay_id = int(message.text.split()[1])
    except:
        await message.answer("Ishlatish: /reject 1"); return
    cur.execute("UPDATE payments SET status='cancelled' WHERE id=?", (pay_id,)); con.commit()
    await message.answer(f"❌ To'lov №{pay_id} bekor qilindi.")

@router.message(Command("done"))
async def admin_done(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Sen admin emassan."); return
    try:
        order_id = int(message.text.split()[1])
    except:
        await message.answer("Ishlatish: /done 1"); return
    cur.execute("SELECT user_id, item FROM orders WHERE id=?", (order_id,))
    row = cur.fetchone()
    if not row:
        await message.answer("Buyurtma topilmadi."); return
    cur.execute("UPDATE orders SET status='done' WHERE id=?", (order_id,)); con.commit()
    await message.answer(f"✅ Buyurtma №{order_id} bajarildi deb belgilandi.")
    await bot.send_message(row[0], f"🎉 Buyurtmangiz №{order_id} bajarildi! {row[1]} yuborildi.")

async def main():
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
