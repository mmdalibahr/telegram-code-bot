import os
import re
import sqlite3
import random
from datetime import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "7762361047"))
BASE_AMOUNT = 15_000_000
CHANNEL_LINK = "https://t.me/+PV1yox-rEZE5NjZk"
DB = "raffle.sqlite3"

CARDS = [
    ("6219861804814316", "محمدعلی بهره بر"),
    ("6037701902347183", "محمدعلی بهره بر"),
]

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN را در محیط تنظیم کنید.")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID را در محیط تنظیم کنید.")

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, full_name TEXT, phone TEXT,
            chances INTEGER, amount INTEGER UNIQUE, status TEXT,
            created_at TEXT, receipt_file_id TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER, ticket_code TEXT UNIQUE,
            user_id INTEGER, full_name TEXT, phone TEXT, created_at TEXT
        )""")
        # Migrate older databases without deleting existing orders or tickets.
        order_columns = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
        migrations = {
            "username": "TEXT DEFAULT ''", "full_name": "TEXT DEFAULT ''",
            "phone": "TEXT DEFAULT ''", "chances": "INTEGER DEFAULT 1",
            "amount": "INTEGER", "status": "TEXT DEFAULT 'awaiting_receipt'",
            "created_at": "TEXT DEFAULT ''", "receipt_file_id": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in order_columns:
                con.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")
        order_columns = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
        if "quantity" in order_columns:
            con.execute("UPDATE orders SET chances = COALESCE(chances, quantity, 1)")
            con.execute("UPDATE orders SET quantity = COALESCE(quantity, chances, 1)")

def money(n):
    return f"{n:,}"

def new_amount():
    # مبلغ هر سفارش یکتا و غیررُند است: از ۱۵ میلیون تا کمتر از ۱۶ میلیون تومان
    with db() as con:
        while True:
            amount = BASE_AMOUNT + random.randint(1_001, 999_997)
            if amount % 10_000 != 0 and not con.execute(
                "SELECT 1 FROM orders WHERE amount=?", (amount,)
            ).fetchone():
                return amount

def new_ticket_code():
    with db() as con:
        while True:
            code = str(random.randint(100, 9999))
            if len(set(code)) > 1 and not con.execute(
                "SELECT 1 FROM tickets WHERE ticket_code=?", (code,)
            ).fetchone():
                return code

def intro_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ موافقم و ادامه می‌دهم", callback_data="agree")],
        [InlineKeyboardButton("📣 عضویت در کانال", url=CHANNEL_LINK)]
    ])

WELCOME = (
    "🎉 به ربات قرعه‌کشی خوش آمدید\n\n"
    "شرکت در این قرعه‌کشی کاملاً اختیاری می‌باشد.\n\n"
    "🔒 حریم خصوصی: نام، شماره تماس و اطلاعات واریزی شما فقط برای "
    "ثبت‌نام در همین قرعه‌کشی استفاده می‌شود و در اختیار شخص دیگری قرار نمی‌گیرد.\n\n"
    "با زدن دکمه زیر موافقت خود را اعلام می‌کنید.\n\n"
    "برای شرکت، دکمه «خرید کد» را بزنید؛ سپس تعداد شانس، نام و نام خانوادگی "
    "و شماره همراه شما دریافت می‌شود.\n\n"
    f"📣 کانال قرعه‌کشی:\n{CHANNEL_LINK}\n\n"
    "📢 پس از ارسال رسید، پرداخت توسط مدیریت بررسی می‌شود. لطفاً تا اعلام نتیجه "
    "از ارسال مجدد وجه خودداری کنید."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(WELCOME, reply_markup=intro_keyboard())

async def agree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎟 خرید کد", callback_data="buy")]])
    await q.message.reply_text("برای ادامه، روی «خرید کد» بزنید.", reply_markup=kb)

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["step"] = "chances"
    await q.message.reply_text("🎟 چند شانس می‌خواهید؟\nلطفاً تعداد را به‌صورت عدد ارسال کنید.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    if not update.message or not update.message.text:
        return
    value = update.message.text.strip()

    if step == "chances":
        if not value.isdigit() or not 1 <= int(value) <= 2000:
            await update.message.reply_text("لطفاً عددی بین ۱ تا ۲۰۰۰ وارد کنید.")
            return
        context.user_data["chances"] = int(value)
        context.user_data["step"] = "name"
        await update.message.reply_text("✍️ نام و نام خانوادگی صاحب شانس را بنویسید.")
        return

    if step == "name":
        if len(value) < 3:
            await update.message.reply_text("لطفاً نام و نام خانوادگی را کامل وارد کنید.")
            return
        context.user_data["full_name"] = value
        context.user_data["step"] = "phone"
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 ارسال شماره همراه من", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
        await update.message.reply_text(
            "📱 شماره همراه خود را با دکمه زیر ارسال کنید یا شماره را دستی بنویسید.",
            reply_markup=kb
        )
        return

    if step == "phone":
        digits = re.sub(r"\D", "", value)
        if len(digits) < 10:
            await update.message.reply_text("شماره همراه معتبر وارد کنید.")
            return
        await make_order(update, context, digits)
        return

    await update.message.reply_text("برای شروع یا ثبت سفارش جدید /start را بزنید.")

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("step") != "phone":
        return
    contact = update.message.contact
    if contact.user_id and contact.user_id != update.effective_user.id:
        await update.message.reply_text("لطفاً شماره تماس خودتان را ارسال کنید.")
        return
    digits = re.sub(r"\D", "", contact.phone_number)
    await make_order(update, context, digits)

async def make_order(update, context, phone):
    user = update.effective_user
    amount = new_amount()
    chances = context.user_data["chances"]
    full_name = context.user_data["full_name"]
    with db() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
        if "quantity" in columns:
            cur = con.execute(
                """INSERT INTO orders
                (user_id, username, full_name, phone, chances, quantity, amount, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_receipt', ?)""",
                (user.id, user.username or "", full_name, phone, chances, chances, amount,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        else:
            cur = con.execute(
                """INSERT INTO orders
                (user_id, username, full_name, phone, chances, amount, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting_receipt', ?)""",
                (user.id, user.username or "", full_name, phone, chances, amount,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        order_id = cur.lastrowid
    context.user_data.clear()
    cards_text = "\n\n".join(
        f"{i}. {holder}\n{card}" for i, (card, holder) in enumerate(CARDS, 1)
    )
    message = (
        "💳 مبلغ پرداخت شما\n\n"
        "کارت‌های مقصد:\n" + cards_text + "\n\n"
        f"💰 مبلغ: {money(amount)} تومان\n"
        f"🎟 تعداد شانس: {chances}\n"
        "⏳ مهلت پرداخت: تا پایان امروز\n\n"
        "⚠️ مبلغ را دقیقاً به یکی از کارت‌های بالا واریز کنید؛ حتی اختلاف یک ریال "
        "ممکن است بررسی پرداخت را دشوار کند.\n\n"
        "پس از واریز، تصویر رسید را همین‌جا ارسال کنید. ثبت نهایی پس از تأیید مدیریت "
        "انجام می‌شود."
    )
    await update.message.reply_text(message, reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("📋 برای کپی مبلغ، عدد زیر را لمس کنید:", 
                                    reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(f"`{amount}`", parse_mode="Markdown")

async def receipt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='awaiting_receipt' ORDER BY id DESC LIMIT 1",
            (user.id,)
        ).fetchone()
        if not order:
            await update.message.reply_text("سفارش در انتظار رسید پیدا نشد. برای شروع /start را بزنید.")
            return
        con.execute("UPDATE orders SET status='pending_admin', receipt_file_id=? WHERE id=?",
                    (photo.file_id, order["id"]))
    caption = (
        f"🧾 رسید سفارش #{order['id']}\n"
        f"نام: {order['full_name']}\n"
        f"موبایل: {order['phone']}\n"
        f"تعداد شانس: {order['chances']}\n"
        f"مبلغ مورد انتظار: {money(order['amount'])} تومان\n"
        f"شناسه تلگرام: {user.id}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید پرداخت و صدور کد", callback_data=f"approve:{order['id']}"),
        InlineKeyboardButton("❌ رد رسید", callback_data=f"reject:{order['id']}")
    ]])
    await context.bot.send_photo(ADMIN_ID, photo.file_id, caption=caption, reply_markup=kb)
    await update.message.reply_text("⏳ رسید شما برای مدیریت ارسال شد. پس از بررسی، نتیجه و کدها برایتان ارسال می‌شود.")

async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("اجازه دسترسی ندارید.", show_alert=True)
        return
    await q.answer()
    action, order_id = q.data.split(":")
    order_id = int(order_id)
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] != "pending_admin":
            await q.edit_message_caption((q.message.caption or "") + "\n\nاین سفارش قبلاً بررسی شده است.")
            return
        if action == "reject":
            con.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
            await context.bot.send_message(order["user_id"], "❌ رسید شما تأیید نشد. برای راهنمایی با مدیریت تماس بگیرید.")
            await q.edit_message_caption((q.message.caption or "") + "\n\n❌ رد شد.")
            return
        codes = []
        for _ in range(order["chances"]):
            code = new_ticket_code()
            codes.append(code)
            con.execute(
                """INSERT INTO tickets (order_id, ticket_code, user_id, full_name, phone, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (order_id, code, order["user_id"], order["full_name"], order["phone"],
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        con.execute("UPDATE orders SET status='approved' WHERE id=?", (order_id,))
        total = con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    await context.bot.send_message(
        order["user_id"],
        "✅ پرداخت شما تأیید شد!\n\n"
        f"👤 نام: {order['full_name']}\n"
        f"🎟 کدهای شما:\n" + "\n".join(codes) +
        "\n\nاین کدها ثبت شدند؛ موفق باشید 🌷"
    )
    await q.edit_message_caption((q.message.caption or "") + "\n\n✅ پرداخت تأیید شد و کد صادر شد.")
    if total >= 2000 and total % 2000 == 0:
        pdf_path = create_pdf()
        with open(pdf_path, "rb") as pdf_file:
            await context.bot.send_document(
                ADMIN_ID, document=pdf_file,
                filename=f"raffle_tickets_{total}.pdf",
                caption=f"📄 فایل PDF فهرست {total} کد ثبت‌شده"
            )

def create_pdf():
    path = "raffle_tickets.pdf"
    c = canvas.Canvas(path, pagesize=A4)
    _, height = A4
    c.setFont("Helvetica", 10)
    y = height - 45
    c.drawString(40, y, "Raffle tickets export")
    y -= 25
    with db() as con:
        rows = con.execute(
            "SELECT ticket_code, full_name, phone, created_at FROM tickets ORDER BY id"
        ).fetchall()
    for row in rows:
        line = f"Code: {row['ticket_code']} | Name: {row['full_name']} | Phone: {row['phone']} | {row['created_at']}"
        c.drawString(35, y, line[:120])
        y -= 15
        if y < 40:
            c.showPage()
            c.setFont("Helvetica", 10)
            y = height - 45
    c.save()
    return path

async def export_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    path = create_pdf()
    with open(path, "rb") as pdf_file:
        await update.message.reply_document(document=pdf_file, filename="raffle_tickets.pdf")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export_pdf", export_pdf))
    app.add_handler(CallbackQueryHandler(agree, pattern="^agree$"))
    app.add_handler(CallbackQueryHandler(buy, pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(admin_decision, pattern="^(approve|reject):"))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.PHOTO, receipt_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
