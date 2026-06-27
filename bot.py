import os
import json
import sqlite3
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────
#  تنظیمات
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID   = int(os.getenv("OWNER_ID", "0"))   # آیدی عددی خودت

DB_FILE    = "data/database.db"
ADMINS_FILE = "data/admins.json"
os.makedirs("data", exist_ok=True)
os.makedirs("uploads", exist_ok=True)

# ─────────────────────────────────────────────
#  مدیریت ادمین‌ها
# ─────────────────────────────────────────────
def load_admins() -> set:
    if os.path.exists(ADMINS_FILE):
        with open(ADMINS_FILE) as f:
            return set(json.load(f))
    return set()

def save_admins(admins: set):
    with open(ADMINS_FILE, "w") as f:
        json.dump(list(admins), f)

def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in load_admins()

# ─────────────────────────────────────────────
#  دیتابیس SQLite داخلی
# ─────────────────────────────────────────────
def get_conn():
    return sqlite3.connect(DB_FILE)

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name   TEXT,
                phone       TEXT,
                card_number TEXT,
                national_id TEXT,
                amount      TEXT,
                date        TEXT,
                description TEXT,
                extra       TEXT
            )
        """)
    print("✅ دیتابیس آماده است.")

# ─────────────────────────────────────────────
#  ایمپورت فایل
# ─────────────────────────────────────────────
COLUMN_MAP = {
    "نام": "full_name", "اسم": "full_name", "name": "full_name", "full_name": "full_name",
    "تلفن": "phone", "موبایل": "phone", "phone": "phone", "mobile": "phone", "شماره": "phone",
    "شماره کارت": "card_number", "card": "card_number", "card_number": "card_number", "کارت": "card_number",
    "کد ملی": "national_id", "ملی": "national_id", "national_id": "national_id", "code": "national_id",
    "مبلغ": "amount", "amount": "amount", "واریز": "amount",
    "تاریخ": "date", "date": "date",
    "توضیحات": "description", "description": "description", "desc": "description",
}

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in COLUMN_MAP:
            rename[col] = COLUMN_MAP[key]
    df = df.rename(columns=rename)
    for c in ["full_name","phone","card_number","national_id","amount","date","description"]:
        if c not in df.columns:
            df[c] = ""
    # ستون‌های اضافه را به extra تبدیل کن
    known = set(COLUMN_MAP.values()) | {"id"}
    extra_cols = [c for c in df.columns if c not in known]
    if extra_cols:
        df["extra"] = df[extra_cols].astype(str).agg(" | ".join, axis=1)
    else:
        df["extra"] = ""
    return df

def import_file(filepath: str) -> tuple[int, str]:
    ext = filepath.rsplit(".", 1)[-1].lower()
    try:
        if ext in ("xlsx", "xls"):
            df = pd.read_excel(filepath, dtype=str)
        elif ext == "csv":
            df = pd.read_csv(filepath, dtype=str)
        elif ext == "json":
            df = pd.read_json(filepath, dtype=str)
        elif ext == "db":
            # اگر SQLite بود، همه جداول را ایمپورت کن
            src = sqlite3.connect(filepath)
            tables = src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            total = 0
            for (t,) in tables:
                df = pd.read_sql(f"SELECT * FROM {t}", src, dtype=str)
                df = normalize_columns(df)
                df.to_sql("records", get_conn(), if_exists="append", index=False,
                          method="multi")
                total += len(df)
            src.close()
            return total, "db"
        else:
            return 0, "unsupported"

        df = normalize_columns(df)
        df.to_sql("records", get_conn(), if_exists="append", index=False, method="multi")
        return len(df), ext
    except Exception as e:
        return -1, str(e)

# ─────────────────────────────────────────────
#  جستجو
# ─────────────────────────────────────────────
def search_records(query: str) -> list[dict]:
    q = f"%{query.strip()}%"
    sql = """
        SELECT * FROM records WHERE
            full_name   LIKE ? OR
            phone       LIKE ? OR
            card_number LIKE ? OR
            national_id LIKE ? OR
            description LIKE ? OR
            extra       LIKE ?
        LIMIT 20
    """
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (q,)*6).fetchall()
    return [dict(r) for r in rows]

def stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        phones = conn.execute("SELECT COUNT(*) FROM records WHERE phone != ''").fetchone()[0]
        cards  = conn.execute("SELECT COUNT(*) FROM records WHERE card_number != ''").fetchone()[0]
        ids    = conn.execute("SELECT COUNT(*) FROM records WHERE national_id != ''").fetchone()[0]
    return {"total": total, "phones": phones, "cards": cards, "ids": ids}

# ─────────────────────────────────────────────
#  فرمت پیام
# ─────────────────────────────────────────────
def fmt_record(r: dict, index: int = 1) -> str:
    lines = [f"📌 *نتیجه {index}*"]
    if r.get("full_name"): lines.append(f"👤 نام: `{r['full_name']}`")
    if r.get("phone"):     lines.append(f"📞 تلفن: `{r['phone']}`")
    if r.get("card_number"): lines.append(f"💳 شماره کارت: `{r['card_number']}`")
    if r.get("national_id"):  lines.append(f"🪪 کد ملی: `{r['national_id']}`")
    if r.get("amount"):    lines.append(f"💰 مبلغ: `{r['amount']}`")
    if r.get("date"):      lines.append(f"📅 تاریخ: `{r['date']}`")
    if r.get("description"): lines.append(f"📝 توضیح: {r['description']}")
    if r.get("extra"):     lines.append(f"ℹ️ سایر: {r['extra']}")
    return "\n".join(lines)

# ─────────────────────────────────────────────
#  کیبورد اصلی
# ─────────────────────────────────────────────
def main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🔍 جستجو", callback_data="menu_search"),
         InlineKeyboardButton("📊 آمار", callback_data="menu_stats")],
        [InlineKeyboardButton("📂 آپلود فایل", callback_data="menu_upload")],
    ]
    if is_admin(user_id):
        kb.append([InlineKeyboardButton("👥 مدیریت ادمین‌ها", callback_data="menu_admins")])
    if user_id == OWNER_ID:
        kb.append([InlineKeyboardButton("🗑 پاک کردن همه داده‌ها", callback_data="menu_clear")])
    return InlineKeyboardMarkup(kb)

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن ادمین", callback_data="admin_add"),
         InlineKeyboardButton("➖ حذف ادمین", callback_data="admin_remove")],
        [InlineKeyboardButton("📋 لیست ادمین‌ها", callback_data="admin_list")],
        [InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")],
    ])

# ─────────────────────────────────────────────
#  وضعیت مکالمه
# ─────────────────────────────────────────────
WAITING_SEARCH, WAITING_ADMIN_ADD, WAITING_ADMIN_REMOVE = range(3)

# ─────────────────────────────────────────────
#  هندلرها
# ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔️ شما دسترسی ندارید.")
        return
    text = (
        f"سلام *{user.first_name}* 👋\n\n"
        "به ربات مدیریت اطلاعات خوش آمدید.\n"
        "از منو زیر استفاده کنید:"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=main_keyboard(user.id))

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if not is_admin(uid):
        await q.edit_message_text("⛔️ دسترسی ندارید.")
        return

    if data == "menu_back":
        await q.edit_message_text("📋 منوی اصلی:", reply_markup=main_keyboard(uid))

    elif data == "menu_search":
        ctx.user_data["state"] = WAITING_SEARCH
        await q.edit_message_text(
            "🔍 *جستجو*\n\nعبارت مورد نظر را بنویسید:\n"
            "_(نام / شماره / شماره کارت / کد ملی)_",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "menu_stats":
        s = stats()
        txt = (
            "📊 *آمار دیتابیس*\n\n"
            f"📁 کل رکوردها: `{s['total']}`\n"
            f"📞 دارای تلفن: `{s['phones']}`\n"
            f"💳 دارای شماره کارت: `{s['cards']}`\n"
            f"🪪 دارای کد ملی: `{s['ids']}`"
        )
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([[
                                      InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")
                                  ]]))

    elif data == "menu_upload":
        ctx.user_data["state"] = None
        await q.edit_message_text(
            "📂 *آپلود فایل دیتابیس*\n\n"
            "فایل خود را ارسال کنید.\n"
            "فرمت‌های پشتیبانی شده:\n"
            "`Excel (.xlsx/.xls)` | `CSV` | `JSON` | `SQLite (.db)`",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "menu_admins":
        if uid != OWNER_ID:
            await q.edit_message_text("⛔️ فقط مالک ربات می‌تواند ادمین‌ها را مدیریت کند.")
            return
        await q.edit_message_text("👥 *مدیریت ادمین‌ها*", parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=admin_keyboard())

    elif data == "menu_clear":
        if uid != OWNER_ID:
            return
        await q.edit_message_text(
            "⚠️ *آیا مطمئن هستید؟*\n\nهمه داده‌ها پاک خواهند شد!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، پاک کن", callback_data="confirm_clear"),
                 InlineKeyboardButton("❌ خیر", callback_data="menu_back")]
            ])
        )

    elif data == "confirm_clear":
        if uid != OWNER_ID:
            return
        with get_conn() as conn:
            conn.execute("DELETE FROM records")
        await q.edit_message_text("✅ همه داده‌ها پاک شدند.",
                                  reply_markup=InlineKeyboardMarkup([[
                                      InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")
                                  ]]))

    elif data == "admin_list":
        admins = load_admins()
        if admins:
            txt = "👥 *لیست ادمین‌ها:*\n\n" + "\n".join(f"• `{a}`" for a in admins)
        else:
            txt = "هیچ ادمینی اضافه نشده."
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=admin_keyboard())

    elif data == "admin_add":
        ctx.user_data["state"] = WAITING_ADMIN_ADD
        await q.edit_message_text(
            "➕ آیدی عددی ادمین جدید را بفرستید:\n_(مثال: 123456789)_",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "admin_remove":
        ctx.user_data["state"] = WAITING_ADMIN_REMOVE
        await q.edit_message_text(
            "➖ آیدی عددی ادمینی که می‌خواهید حذف کنید را بفرستید:",
            parse_mode=ParseMode.MARKDOWN
        )

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    state = ctx.user_data.get("state")
    text  = (update.message.text or "").strip()

    # ── جستجو ──────────────────────────────
    if state == WAITING_SEARCH:
        ctx.user_data["state"] = None
        if not text:
            await update.message.reply_text("❌ عبارت جستجو خالی است.")
            return
        results = search_records(text)
        if not results:
            await update.message.reply_text(
                f"❌ نتیجه‌ای برای *{text}* پیدا نشد.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 منو", callback_data="menu_back")
                ]])
            )
            return
        header = f"✅ *{len(results)} نتیجه* برای جستجوی `{text}`:\n\n"
        chunks = [header]
        for i, r in enumerate(results, 1):
            block = fmt_record(r, i) + "\n\n" + "─"*28 + "\n\n"
            if sum(len(c) for c in chunks) + len(block) > 3800:
                await update.message.reply_text(
                    "".join(chunks), parse_mode=ParseMode.MARKDOWN
                )
                chunks = []
            chunks.append(block)
        if chunks:
            await update.message.reply_text(
                "".join(chunks), parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 منو", callback_data="menu_back")
                ]])
            )

    # ── افزودن ادمین ───────────────────────
    elif state == WAITING_ADMIN_ADD:
        ctx.user_data["state"] = None
        if not text.isdigit():
            await update.message.reply_text("❌ آیدی باید عدد باشد.")
            return
        admins = load_admins()
        admins.add(int(text))
        save_admins(admins)
        await update.message.reply_text(f"✅ ادمین `{text}` اضافه شد.",
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=admin_keyboard())

    # ── حذف ادمین ──────────────────────────
    elif state == WAITING_ADMIN_REMOVE:
        ctx.user_data["state"] = None
        if not text.isdigit():
            await update.message.reply_text("❌ آیدی باید عدد باشد.")
            return
        admins = load_admins()
        admins.discard(int(text))
        save_admins(admins)
        await update.message.reply_text(f"✅ ادمین `{text}` حذف شد.",
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=admin_keyboard())

    else:
        # هیچ حالتی فعال نیست
        await update.message.reply_text(
            "از منو استفاده کنید:",
            reply_markup=main_keyboard(uid)
        )

async def file_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    doc = update.message.document
    if not doc:
        return

    name = doc.file_name or "file"
    ext  = name.rsplit(".", 1)[-1].lower()
    if ext not in ("xlsx", "xls", "csv", "json", "db"):
        await update.message.reply_text(
            "❌ فرمت پشتیبانی نمی‌شود.\n"
            "فرمت‌های مجاز: `xlsx, xls, csv, json, db`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    msg = await update.message.reply_text("⏳ در حال دریافت و پردازش فایل...")
    path = f"uploads/{uid}_{name}"
    file = await doc.get_file()
    await file.download_to_drive(path)

    count, info = import_file(path)
    os.remove(path)

    if count == -1:
        await msg.edit_text(f"❌ خطا در پردازش فایل:\n`{info}`", parse_mode=ParseMode.MARKDOWN)
    elif count == 0:
        await msg.edit_text("⚠️ فایل خالی بود یا هیچ رکوردی یافت نشد.")
    else:
        await msg.edit_text(
            f"✅ *{count} رکورد* با موفقیت وارد شد!\n\n"
            f"📄 فرمت: `{info}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 مشاهده آمار", callback_data="menu_stats"),
                InlineKeyboardButton("🔍 جستجو", callback_data="menu_search"),
            ]])
        )

# ─────────────────────────────────────────────
#  اجرا
# ─────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("🤖 ربات در حال اجرا است...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
