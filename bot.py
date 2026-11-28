import os, re, sqlite3, random, secrets, io
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7762361047"))
DB = os.getenv("DB_PATH", "raffle.sqlite3")
CHANNEL_LINK = "https://t.me/+PV1yox-rEZE5NjZk"
CARDS = [
    ("6219861804814316", "محمدعلی بهره بر"),
    ("6037701902347183", "محمدعلی بهره بر"),
]
DEFAULT_CAPACITY = 2000
DEFAULT_PRICE = 15000000  # مبلغ پایه سفارش به تومان؛ پیش از اجرا حتماً در config بررسی شود
REPORT_EVERY = 2000

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def money(n):
    return f"{int(n):,}"

def init_db():
    """Create tables and safely add columns used by this bot to older databases."""
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS rounds(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            capacity INTEGER NOT NULL, base_amount INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL DEFAULT 1,
            user_id INTEGER NOT NULL, username TEXT, full_name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL DEFAULT 1,
            amount INTEGER NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'awaiting_receipt',
            created_at TEXT NOT NULL DEFAULT '', receipt_file_id TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL DEFAULT 1,
            order_id INTEGER NOT NULL, code TEXT, user_id INTEGER NOT NULL,
            full_name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '', UNIQUE(round_id, code))""")

        # Migrate databases created by earlier versions without deleting user data.
        def columns(table):
            return {row[1] for row in c.execute(f"PRAGMA table_info({table})")}

        order_cols = columns("orders")
        additions = {
            "round_id": "INTEGER NOT NULL DEFAULT 1",
            "quantity": "INTEGER NOT NULL DEFAULT 1",
            "username": "TEXT",
            "full_name": "TEXT NOT NULL DEFAULT ''",
            "phone": "TEXT NOT NULL DEFAULT ''",
            "receipt_file_id": "TEXT",
        }
        for name, definition in additions.items():
            if name not in order_cols:
                c.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
        order_cols = columns("orders")
        if "chances" in order_cols:
            c.execute("UPDATE orders SET quantity=chances WHERE quantity=1 AND chances IS NOT NULL")

        ticket_cols = columns("tickets")
        ticket_additions = {
            "round_id": "INTEGER NOT NULL DEFAULT 1",
            "code": "TEXT",
            "user_id": "INTEGER NOT NULL DEFAULT 0",
            "full_name": "TEXT NOT NULL DEFAULT ''",
            "phone": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in ticket_additions.items():
            if name not in ticket_cols:
                c.execute(f"ALTER TABLE tickets ADD COLUMN {name} {definition}")
        ticket_cols = columns("tickets")
        if "ticket_code" in ticket_cols:
            c.execute("UPDATE tickets SET code=ticket_code WHERE code IS NULL")

        if not c.execute("SELECT id FROM rounds LIMIT 1").fetchone():
            c.execute("INSERT INTO rounds(title,capacity,base_amount,active,created_at) VALUES(?,?,?,?,?)",
                      ("دوره اول", DEFAULT_CAPACITY, DEFAULT_PRICE, 1, now()))
        # Ensure legacy rows point to an existing round.
        first_round = c.execute("SELECT id FROM rounds ORDER BY id LIMIT 1").fetchone()["id"]
        c.execute("UPDATE orders SET round_id=? WHERE round_id IS NULL OR round_id=0", (first_round,))
        c.execute("UPDATE tickets SET round_id=? WHERE round_id IS NULL OR round_id=0", (first_round,))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_round_code ON tickets(round_id, code)")

def active_round():
    with conn() as c:
        return c.execute("SELECT * FROM rounds WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()

def round_count(rid):
    with conn() as c:
        return c.execute("SELECT COUNT(*) n FROM tickets WHERE round_id=?", (rid,)).fetchone()["n"]

def fmt_round(r):
    return f"🎁 {r['title']}\\nظرفیت: {r['capacity']:,}\\nکد صادرشده: {round_count(r['id']):,}\\nباقی‌مانده: {max(0,r['capacity']-round_count(r['id'])):,}"

def amount_for(base):
    # مبلغ اختصاصی و غیررُند برای شناسایی آسان‌تر واریزی
    with conn() as c:
        while True:
            amount = base + random.randint(1001, 999997)
            if amount % 1000 and not c.execute("SELECT 1 FROM orders WHERE amount=?", (amount,)).fetchone():
                return amount

def unique_code(rid):
    with conn() as c:
        while True:
            code = f"{random.randint(0,9999):04d}"
            if not c.execute("SELECT 1 FROM tickets WHERE round_id=? AND code=?", (rid,code)).fetchone():
                return code

def intro_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ موافقم و ادامه می‌دهم", callback_data="agree")],
        [InlineKeyboardButton("📣 مشاهده کانال قرعه‌کشی", url=CHANNEL_LINK)]
    ])

WELCOME = (
    "🎉 به جشنواره قرعه‌کشی خوش آمدید 🎉\\n\\n"
    "شرکت در این قرعه‌کشی کاملاً اختیاری می‌باشد.\\n\\n"
    "🔒 حریم خصوصی: نام، شماره تماس و اطلاعات واریزی شما فقط برای همین جشنواره استفاده می‌شود و در اختیار کسی قرار نمی‌گیرد.\\n\\n"
    "با زدن دکمه زیر موافقت خود را اعلام می‌کنید.\\n\\n"
    "برای شرکت، دکمه «خرید کد» را بزنید. ابتدا نام و نام خانوادگی و شماره همراه صاحب رسید از شما پرسیده می‌شود و سپس مراحل بعدی انجام می‌گیرد.\\n\\n"
    f"📣 کانال قرعه‌کشی:\\n{CHANNEL_LINK}\\n\\n"
    "📢 با توجه به حجم بالای ورودی، پردازش هر واریز ممکن است چند دقیقه طول بکشد. پس از اعلام ثبت رسید، نیازی به ارسال دوباره نیست."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    r = active_round()
    extra = "\\n\\n" + fmt_round(r) if r else "\\n\\nفعلاً دوره فعالی وجود ندارد."
    await update.message.reply_text(WELCOME + extra, reply_markup=intro_kb())

async def agree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("برای ثبت‌نام و خرید کد، دکمه زیر را انتخاب کنید.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎟 خرید کد", callback_data="buy")]]))

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    r = active_round()
    if not r or round_count(r["id"]) >= r["capacity"]:
        await q.message.reply_text("ظرفیت این دوره تکمیل شده یا دوره فعالی وجود ندارد.")
        return
    context.user_data.clear()
    context.user_data.update(step="name", round_id=r["id"])
    await q.message.reply_text("✍️ لطفاً نام و نام خانوادگی صاحب رسید را وارد کنید.")

async def ask_phone(update, context):
    context.user_data["step"] = "phone"
    kb = ReplyKeyboardMarkup([[KeyboardButton("📱 ارسال شماره همراه من", request_contact=True)]],
                             resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("📱 شماره همراه را با دکمه زیر ارسال کنید (یا شماره را دستی بنویسید).",
                                    reply_markup=kb)

async def make_order(update, context, phone):
    r_id = context.user_data.get("round_id")
    with conn() as c:
        r = c.execute("SELECT * FROM rounds WHERE id=?", (r_id,)).fetchone()
        if not r or not r["active"]:
            await update.message.reply_text("این دوره دیگر فعال نیست. /start را بزنید.")
            context.user_data.clear()
            return
        sold = c.execute("SELECT COUNT(*) n FROM tickets WHERE round_id=?", (r_id,)).fetchone()["n"]
        pending = c.execute("SELECT COALESCE(SUM(quantity),0) n FROM orders WHERE round_id=? AND status IN ('awaiting_receipt','pending_admin')", (r_id,)).fetchone()["n"]
        available = r["capacity"] - sold - pending
        qty = context.user_data["quantity"]
        if qty > available:
            await update.message.reply_text(f"ظرفیت باقی‌مانده برای سفارش جدید: {max(0,available)}")
            return
        amount = amount_for(r["base_amount"])
        cur = c.execute("""INSERT INTO orders(round_id,user_id,username,full_name,phone,quantity,amount,status,created_at)
            VALUES(?,?,?,?,?,?,?,'awaiting_receipt',?)""",
            (r_id, update.effective_user.id, update.effective_user.username or "",
             context.user_data["full_name"], phone, qty, amount, now()))
        oid = cur.lastrowid
    name = context.user_data["full_name"]
    context.user_data.clear()
    cards = "\\n\\n".join(f"{i}. {holder}\\n{card}" for i,(card,holder) in enumerate(CARDS,1))
    msg = (f"💳 مبلغ پرداخت شما — {r['title']}\\n\\nکارت‌های مقصد:\\n{cards}\\n\\n"
           f"💰 مبلغ: {money(amount)} تومان\\n🎟 تعداد شانس: {qty}\\n"
           "⏳ مهلت پرداخت: تا پایان دوره\\n\\n"
           "⚠️ مبلغ را دقیقاً به یکی از کارت‌های بالا واریز کنید.\\n"
           "پس از واریز، تصویر رسید را همین‌جا ارسال کنید. خرید فقط پس از تأیید مدیریت نهایی می‌شود.")
    await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(f"📋 کپی مبلغ پرداخت:\\n{amount}", reply_markup=ReplyKeyboardRemove())

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    val = (update.message.text or "").strip()
    if step == "name":
        if len(val) < 3:
            await update.message.reply_text("نام و نام خانوادگی را کامل وارد کنید.")
            return
        context.user_data["full_name"] = val
        context.user_data["step"] = "quantity"
        await update.message.reply_text("🎟 چند کد می‌خواهید؟ تعداد را به عدد وارد کنید.")
    elif step == "quantity":
        if not val.isdigit() or not 1 <= int(val) <= 2000:
            await update.message.reply_text("تعداد باید عددی بین ۱ تا ۲۰۰۰ باشد.")
            return
        context.user_data["quantity"] = int(val)
        await ask_phone(update, context)
    elif step == "phone":
        phone = re.sub(r"\\D", "", val)
        if len(phone) < 10:
            await update.message.reply_text("شماره همراه معتبر وارد کنید.")
            return
        await make_order(update, context, phone)
    else:
        await update.message.reply_text("برای شروع /start را بزنید.")

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("step") != "phone":
        return
    contact = update.message.contact
    if contact.user_id and contact.user_id != update.effective_user.id:
        await update.message.reply_text("لطفاً شماره تماس خودتان را ارسال کنید.")
        return
    await make_order(update, context, re.sub(r"\\D", "", contact.phone_number))

async def receipt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    with conn() as c:
        order = c.execute("SELECT * FROM orders WHERE user_id=? AND status='awaiting_receipt' ORDER BY id DESC LIMIT 1", (user.id,)).fetchone()
        if not order:
            await update.message.reply_text("سفارش در انتظار رسید پیدا نشد. برای شروع /start را بزنید.")
            return
        c.execute("UPDATE orders SET status='pending_admin',receipt_file_id=? WHERE id=?", (photo.file_id,order["id"]))
    cap = (f"🧾 رسید سفارش #{order['id']}\\nدوره: {order['round_id']}\\nنام: {order['full_name']}\\n"
           f"موبایل: {order['phone']}\\nتعداد کد: {order['quantity']}\\nمبلغ مورد انتظار: {money(order['amount'])} تومان\\n"
           f"شناسه تلگرام: {user.id}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید پرداخت و صدور کد",callback_data=f"approve:{order['id']}"),
                                InlineKeyboardButton("❌ رد رسید",callback_data=f"reject:{order['id']}")]])
    await context.bot.send_photo(ADMIN_ID, photo.file_id, caption=cap, reply_markup=kb)
    await update.message.reply_text("⏳ رسید برای مدیریت ارسال شد. پس از بررسی، نتیجه اعلام می‌شود.")

def create_pdf(rid):
    path = f"tickets_round_{rid}.pdf"
    with conn() as c:
        r = c.execute("SELECT * FROM rounds WHERE id=?", (rid,)).fetchone()
        rows = c.execute("SELECT code,full_name,phone,created_at FROM tickets WHERE round_id=? ORDER BY id", (rid,)).fetchall()
    cv = canvas.Canvas(path, pagesize=A4)
    w,h = A4
    cv.setFont("Helvetica", 9)
    y = h-40
    cv.drawString(35,y, f"Raffle report | Round: {rid} | {len(rows)} tickets")
    y -= 24
    for x in rows:
        line = f"Code: {x['code']} | Name: {x['full_name']} | Phone: {x['phone']} | {x['created_at']}"
        cv.drawString(35,y,line[:130])
        y -= 14
        if y < 35:
            cv.showPage(); cv.setFont("Helvetica",9); y=h-40
    cv.save()
    return path

async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("اجازه دسترسی ندارید.", show_alert=True); return
    await q.answer()
    action, oid = q.data.split(":"); oid = int(oid)
    with conn() as c:
        order = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not order or order["status"] != "pending_admin":
            await q.edit_message_caption((q.message.caption or "") + "\\nقبلاً بررسی شده است.")
            return
        if action == "reject":
            c.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
            await context.bot.send_message(order["user_id"], "❌ رسید شما تأیید نشد. برای پیگیری با مدیریت تماس بگیرید.")
            await q.edit_message_caption((q.message.caption or "") + "\\n❌ رد شد.")
            return
        used = c.execute("SELECT COUNT(*) n FROM tickets WHERE round_id=?", (order["round_id"],)).fetchone()["n"]
        r = c.execute("SELECT capacity FROM rounds WHERE id=?", (order["round_id"],)).fetchone()
        if used + order["quantity"] > r["capacity"]:
            await q.edit_message_caption((q.message.caption or "") + "\\n⚠️ ظرفیت دوره پیش از تأیید تکمیل شده؛ کد صادر نشد.")
            return
        codes=[]
        for _ in range(order["quantity"]):
            code=unique_code(order["round_id"]); codes.append(code)
            c.execute("INSERT INTO tickets(round_id,order_id,code,user_id,full_name,phone,created_at) VALUES(?,?,?,?,?,?,?)",
                      (order["round_id"],oid,code,order["user_id"],order["full_name"],order["phone"],now()))
        c.execute("UPDATE orders SET status='approved' WHERE id=?", (oid,))
        total = c.execute("SELECT COUNT(*) n FROM tickets WHERE round_id=?", (order["round_id"],)).fetchone()["n"]
    await context.bot.send_message(order["user_id"], "✅ پرداخت شما تأیید شد!\\n\\n"
        f"👤 نام: {order['full_name']}\\n🎟 کدهای شما:\\n" + "\\n".join(codes))
    await q.edit_message_caption((q.message.caption or "") + "\\n✅ تأیید شد و کد صادر شد.")
    if total % REPORT_EVERY == 0 or (total // REPORT_EVERY > (total-order["quantity"]) // REPORT_EVERY):
        path=create_pdf(order["round_id"])
        with open(path,"rb") as f:
            await context.bot.send_document(ADMIN_ID, f, filename=path,
                caption=f"📄 گزارش دوره {order['round_id']} — {total} کد صادرشده")

async def newround(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(context.args) < 3:
        await update.message.reply_text("فرمت: /newround نام_دوره ظرفیت مبلغ_پایه_تومان\\nمثال: /newround Dore2 10000 15000000")
        return
    title, cap, price = context.args[0], context.args[1], context.args[2]
    if not cap.isdigit() or not price.isdigit() or int(cap)<1 or int(price)<1:
        await update.message.reply_text("ظرفیت و مبلغ باید عدد مثبت باشند."); return
    with conn() as c:
        c.execute("UPDATE rounds SET active=0 WHERE active=1")
        c.execute("INSERT INTO rounds(title,capacity,base_amount,active,created_at) VALUES(?,?,?,?,?)",
                  (title.replace("_"," "),int(cap),int(price),1,now()))
        rid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
    await update.message.reply_text(f"✅ دوره جدید ساخته و فعال شد. شناسه: {rid}\\n{title} | ظرفیت {int(cap):,}")

async def rounds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with conn() as c: rows=c.execute("SELECT * FROM rounds ORDER BY id DESC").fetchall()
    await update.message.reply_text("\\n\\n".join(fmt_round(r)+("\\n🟢 فعال" if r["active"] else "\\n⚪️ غیرفعال") for r in rows) or "دوره‌ای ثبت نشده.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with conn() as c:
        orders=c.execute("SELECT status,COUNT(*) n FROM orders GROUP BY status").fetchall()
        rs=c.execute("SELECT * FROM rounds ORDER BY id DESC").fetchall()
    await update.message.reply_text("📊 وضعیت سفارش‌ها:\\n"+("\\n".join(f"{x['status']}: {x['n']}" for x in orders) or "بدون سفارش")+
        "\\n\\n"+"\\n\\n".join(fmt_round(r) for r in rs))

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    r=active_round()
    if context.args and context.args[0].isdigit():
        rid=int(context.args[0])
    elif r: rid=r["id"]
    else:
        await update.message.reply_text("دوره‌ای فعال نیست."); return
    path=create_pdf(rid)
    with open(path,"rb") as f: await update.message.reply_document(f, filename=path)

def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN تنظیم نشده است.")
    init_db()
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("newround",newround))
    app.add_handler(CommandHandler("rounds",rounds_cmd))
    app.add_handler(CommandHandler("status",status_cmd))
    app.add_handler(CommandHandler("export_pdf",export_cmd))
    app.add_handler(CallbackQueryHandler(agree,pattern="^agree$"))
    app.add_handler(CallbackQueryHandler(buy,pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(admin_decision,pattern="^(approve|reject):"))
    app.add_handler(MessageHandler(filters.CONTACT,contact_handler))
    app.add_handler(MessageHandler(filters.PHOTO,receipt_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
