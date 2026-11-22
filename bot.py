import os
import re
import sqlite3
import secrets
import string
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BASE_PRICE = int(os.getenv("BASE_PRICE", "100000"))
DB_PATH = os.getenv("DB_PATH", "raffle.sqlite3")

if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in environment variables.")
if not ADMIN_ID:
    raise RuntimeError("Set ADMIN_ID (your numeric Telegram user ID).")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            quantity INTEGER NOT NULL,
            amount INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'awaiting_receipt',
            receipt_file_id TEXT,
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )""")

def make_code():
    alphabet = string.ascii_uppercase + string.digits
    return "RAFFLE-" + "".join(secrets.choice(alphabet) for _ in range(10))

def new_amount(con):
    # Unique, non-rounded amount: base price per ticket + a small unique suffix.
    # The customer must transfer the exact displayed total.
    while True:
        suffix = secrets.randbelow(999) + 1
        amount = BASE_PRICE + suffix
        if not con.execute("SELECT 1 FROM orders WHERE amount=?", (amount,)).fetchone():
            return amount

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! به سامانه ثبت‌نام قرعه‌کشی خوش آمدید.\\n"
        "برای شروع ثبت سفارش، /buy را بزنید.\\n"
        "پس از ثبت سفارش، رسید پرداخت را همین‌جا به‌صورت عکس ارسال کنید."
    )

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"قیمت پایه هر کد: {BASE_PRICE:,} تومان\\n"
        "تعداد کد موردنظر را به‌صورت عددی بفرستید (حداکثر ۲۰)."
    )
    context.user_data["waiting_quantity"] = True

async def handle_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_quantity"):
        return
    raw = (update.message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 20:
        await update.message.reply_text("لطفاً عددی بین ۱ تا ۲۰ بفرستید.")
        return

    quantity = int(raw)
    context.user_data["waiting_quantity"] = False
    user = update.effective_user

    with db() as con:
        amount = new_amount(con)
        cur = con.execute(
            """INSERT INTO orders
            (user_id, username, quantity, amount, status, created_at)
            VALUES (?, ?, ?, ?, 'awaiting_receipt', ?)""",
            (user.id, user.username or "", quantity, amount,
             datetime.now(timezone.utc).isoformat())
        )
        order_id = cur.lastrowid

    context.user_data["current_order_id"] = order_id
    await update.message.reply_text(
        f"سفارش شماره #{order_id}\\n"
        f"تعداد کد: {quantity}\\n"
        f"مبلغ دقیق قابل واریز: {amount:,} تومان\\n\\n"
        "پس از واریز، تصویر رسید را همین‌جا ارسال کنید. "
        "کدها فقط پس از بررسی و تأیید مدیر صادر می‌شوند.\\n\\n"
        "توجه: این ربات پرداخت بانکی را خودکار بررسی نمی‌کند؛ "
        "رسید باید با تراکنش واقعی تطبیق داده شود."
    )

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo
    if not photo:
        return
    user_id = update.effective_user.id
    file_id = photo[-1].file_id

    with db() as con:
        order = con.execute(
            """SELECT * FROM orders WHERE user_id=?
            AND status='awaiting_receipt' ORDER BY id DESC LIMIT 1""",
            (user_id,)
        ).fetchone()
        if not order:
            await update.message.reply_text("سفارشِ منتظر رسید پیدا نشد. ابتدا /buy را بزنید.")
            return
        con.execute(
            "UPDATE orders SET receipt_file_id=?, status='pending_review' WHERE id=?",
            (file_id, order["id"])
        )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("تأیید و صدور کد", callback_data=f"approve:{order['id']}"),
        InlineKeyboardButton("رد رسید", callback_data=f"reject:{order['id']}")
    ]])
    caption = (
        f"رسید سفارش #{order['id']}\\n"
        f"کاربر: {user_id} (@{update.effective_user.username or 'بدون‌نام'})\\n"
        f"تعداد: {order['quantity']}\\n"
        f"مبلغ مورد انتظار: {order['amount']:,} تومان"
    )
    await context.bot.send_photo(
        chat_id=ADMIN_ID, photo=file_id, caption=caption, reply_markup=keyboard
    )
    await update.message.reply_text("رسید دریافت شد و برای بررسی مدیر ارسال شد.")

async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.edit_message_caption("این عملیات فقط برای مدیر مجاز است.")
        return

    action, order_id_text = query.data.split(":")
    order_id = int(order_id_text)

    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] != "pending_review":
            await query.edit_message_caption("این سفارش قبلاً بررسی شده یا وجود ندارد.")
            return

        if action == "reject":
            con.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
            await context.bot.send_message(
                order["user_id"],
                f"رسید سفارش #{order_id} تأیید نشد. برای راهنمایی با مدیر تماس بگیرید."
            )
            await query.edit_message_caption((query.message.caption or "") + "\\n\\nوضعیت: رد شد")
            return

        if action == "approve":
            codes = []
            for _ in range(order["quantity"]):
                code = make_code()
                con.execute(
                    "INSERT INTO tickets (order_id, user_id, code, created_at) VALUES (?, ?, ?, ?)",
                    (order_id, order["user_id"], code, datetime.now(timezone.utc).isoformat())
                )
                codes.append(code)
            con.execute("UPDATE orders SET status='approved' WHERE id=?", (order_id,))

    message = (
        f"پرداخت سفارش #{order_id} تأیید شد.\\n"
        "کدهای شرکت در قرعه‌کشی شما:\\n" +
        "\\n".join(codes) +
        "\\n\\nاین کدها را تا زمان اعلام نتایج نگه دارید."
    )
    await context.bot.send_message(order["user_id"], message)
    await query.edit_message_caption((query.message.caption or "") + "\\n\\nوضعیت: تأیید شد و کدها صادر شدند.")

async def mycodes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        rows = con.execute(
            "SELECT code FROM tickets WHERE user_id=? ORDER BY id DESC",
            (update.effective_user.id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("هنوز کدی برای شما صادر نشده است.")
        return
    await update.message.reply_text("کدهای ثبت‌شده شما:\\n" + "\\n".join(r["code"] for r in rows))

async def admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as con:
        rows = con.execute(
            "SELECT id, user_id, quantity, amount, status FROM orders ORDER BY id DESC LIMIT 30"
        ).fetchall()
    if not rows:
        await update.message.reply_text("هنوز سفارشی ثبت نشده است.")
        return
    lines = [
        f"#{r['id']} | کاربر {r['user_id']} | {r['quantity']} کد | "
        f"{r['amount']:,} تومان | {r['status']}"
        for r in rows
    ]
    await update.message.reply_text("\\n".join(lines))

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("mycodes", mycodes))
    app.add_handler(CommandHandler("orders", admin_orders))
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r"^(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quantity))
    app.add_handler(MessageHandler(filters.PHOTO, handle_receipt))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
