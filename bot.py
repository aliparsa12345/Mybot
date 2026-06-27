import os, json, sqlite3, logging, asyncio
import pandas as pd
from io import BytesIO
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

# ── لاگ ──────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── تنظیمات ──────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN")
OWNER_ID  = int(os.getenv("OWNER_ID", "0"))
OWNER_UN  = os.getenv("OWNER_USERNAME", "historyoflx0")

Path("data").mkdir(exist_ok=True)
Path("uploads").mkdir(exist_ok=True)

DB   = "data/db.sqlite"
ADM  = "data/admins.json"

# ── ادمین ─────────────────────────────────────
def get_admins():
    if Path(ADM).exists():
        return set(json.load(open(ADM)))
    return set()

def save_admins(s):
    json.dump(list(s), open(ADM, "w"))

def is_admin(uid):
    return uid == OWNER_ID or uid in get_admins()

# ── دیتابیس ───────────────────────────────────
def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA cache_size=-32000")
    return c

def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS data (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT DEFAULT '',
            phone   TEXT DEFAULT '',
            card    TEXT DEFAULT '',
            natid   TEXT DEFAULT '',
            account TEXT DEFAULT '',
            amount  TEXT DEFAULT '',
            date    TEXT DEFAULT '',
            note    TEXT DEFAULT '',
            extra   TEXT DEFAULT ''
        )""")
        for col, idx in [("name","i_name"),("phone","i_phone"),
                         ("card","i_card"),("natid","i_natid"),("account","i_acc")]:
            c.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON data({col})")
    log.info("DB ready")

# ── نگاشت ستون ────────────────────────────────
MAP = {
    "نام":"name","نام و نام خانوادگی":"name","نام کامل":"name","اسم":"name",
    "name":"name","full_name":"name","fullname":"name",
    "تلفن":"phone","موبایل":"phone","شماره":"phone","شماره موبایل":"phone",
    "شماره همراه":"phone","phone":"phone","mobile":"phone","tel":"phone",
    "شماره کارت":"card","کارت":"card","card":"card","card_number":"card",
    "کد ملی":"natid","کدملی":"natid","ملی":"natid","national_id":"natid","codemeli":"natid",
    "شماره حساب":"account","حساب":"account","account":"account","iban":"account","شبا":"account",
    "مبلغ":"amount","amount":"amount","واریز":"amount","price":"amount",
    "تاریخ":"date","date":"date","datetime":"date",
    "توضیحات":"note","توضیح":"note","description":"note","desc":"note","note":"note",
}
KNOWN = set(MAP.values()) | {"id"}

def fix_df(df, src=""):
    ren = {}
    for c in df.columns:
        k = c.strip().lower().replace("\u200c","")
        if k in MAP: ren[c] = MAP[k]
    df = df.rename(columns=ren)
    for c in ["name","phone","card","natid","account","amount","date","note"]:
        if c not in df.columns: df[c] = ""
    extra = [c for c in df.columns if c not in KNOWN]
    df["extra"] = df[extra].astype(str).agg(" | ".join, axis=1) if extra else ""
    return df.fillna("").astype(str).replace("nan","").replace("None","")

ENCS = ["utf-8","utf-8-sig","windows-1256","latin-1"]

def import_csv(data: bytes, src: str) -> int:
    for enc in ENCS:
        try:
            total = 0
            c = conn()
            for chunk in pd.read_csv(BytesIO(data), dtype=str, encoding=enc,
                                     chunksize=3000, on_bad_lines="skip"):
                fix_df(chunk, src).to_sql("data", c, if_exists="append", index=False)
                total += len(chunk)
            c.close()
            return total
        except UnicodeDecodeError:
            continue
        except Exception as e:
            raise e
    raise ValueError("encoding error")

def import_excel(data: bytes, src: str) -> int:
    df = pd.read_excel(BytesIO(data), dtype=str)
    c = conn()
    total = 0
    for i in range(0, len(df), 3000):
        fix_df(df.iloc[i:i+3000].copy(), src).to_sql("data", c, if_exists="append", index=False)
        total += len(df.iloc[i:i+3000])
    c.close()
    return total

def do_import(path: str) -> tuple:
    name = Path(path).name
    ext  = name.rsplit(".",1)[-1].lower() if "." in name else ""
    try:
        data = open(path,"rb").read()
        if ext in ("xlsx","xls"):
            return import_excel(data, name), "Excel"
        if ext == "csv":
            return import_csv(data, name), "CSV"
        if ext == "json":
            df = pd.read_json(BytesIO(data), dtype=str)
            fix_df(df, name).to_sql("data", conn(), if_exists="append", index=False)
            return len(df), "JSON"
        if ext == "db":
            src = sqlite3.connect(path)
            tabs = [r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            total = 0
            c = conn()
            for t in tabs:
                df = pd.read_sql(f"SELECT * FROM {t}", src, dtype=str)
                fix_df(df, name).to_sql("data", c, if_exists="append", index=False)
                total += len(df)
            src.close(); c.close()
            return total, "SQLite"
        return 0, "unsupported"
    except Exception as e:
        log.exception("import error")
        return -1, str(e)

# ── جستجو ─────────────────────────────────────
FIELDS = {
    "s_name":    ("نام و نام خانوادگی", "name",    "👤"),
    "s_natid":   ("کد ملی",             "natid",   "🪪"),
    "s_phone":   ("شماره همراه",        "phone",   "📞"),
    "s_card":    ("شماره کارت",         "card",    "💳"),
    "s_account": ("شماره حساب",         "account", "🏦"),
}

def search(col, q, limit=30):
    with conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            f"SELECT * FROM data WHERE {col} LIKE ? LIMIT ?",
            (f"%{q.strip()}%", limit)
        ).fetchall()]

def stats():
    with conn() as c:
        t  = c.execute("SELECT COUNT(*) FROM data").fetchone()[0]
        ph = c.execute("SELECT COUNT(*) FROM data WHERE phone!=''").fetchone()[0]
        ca = c.execute("SELECT COUNT(*) FROM data WHERE card!=''").fetchone()[0]
        ni = c.execute("SELECT COUNT(*) FROM data WHERE natid!=''").fetchone()[0]
        ac = c.execute("SELECT COUNT(*) FROM data WHERE account!=''").fetchone()[0]
    sz = round(Path(DB).stat().st_size/1_048_576,1) if Path(DB).exists() else 0
    return t, ph, ca, ni, ac, sz

# ── فرمت ──────────────────────────────────────
def fmt(r, i):
    lines = [f"*#{i}*  ─────────────────────"]
    if r.get("name"):    lines.append(f"👤  `{r['name']}`")
    if r.get("phone"):   lines.append(f"📞  `{r['phone']}`")
    if r.get("card"):    lines.append(f"💳  `{r['card']}`")
    if r.get("natid"):   lines.append(f"🪪  `{r['natid']}`")
    if r.get("account"): lines.append(f"🏦  `{r['account']}`")
    if r.get("amount"):  lines.append(f"💰  `{r['amount']}`")
    if r.get("date"):    lines.append(f"📅  `{r['date']}`")
    if r.get("note"):    lines.append(f"📝  {r['note'][:100]}")
    if r.get("extra") and r["extra"].replace("|","").strip():
        lines.append(f"ℹ️  {r['extra'][:150]}")
    return "\n".join(lines)

# ── کیبورد ────────────────────────────────────
def kb_main(uid):
    rows = [
        [InlineKeyboardButton("🔍 جستجو",        callback_data="menu_search"),
         InlineKeyboardButton("📊 آمار",          callback_data="menu_stats")],
        [InlineKeyboardButton("📂 آپلود فایل",    callback_data="menu_upload")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("👥 مدیریت ادمین‌ها", callback_data="menu_admins")])
    return InlineKeyboardMarkup(rows)

def kb_search():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 نام و نام خانوادگی", callback_data="s_name")],
        [InlineKeyboardButton("🪪 کد ملی",              callback_data="s_natid")],
        [InlineKeyboardButton("📞 شماره همراه",         callback_data="s_phone")],
        [InlineKeyboardButton("💳 شماره کارت",          callback_data="s_card")],
        [InlineKeyboardButton("🏦 شماره حساب",          callback_data="s_account")],
        [InlineKeyboardButton("🔙 برگشت",               callback_data="menu_back")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 جستجوی جدید", callback_data="menu_search"),
        InlineKeyboardButton("🏠 منو",          callback_data="menu_back"),
    ]])

def kb_admins():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن ادمین",  callback_data="adm_add"),
         InlineKeyboardButton("➖ حذف ادمین",    callback_data="adm_rem")],
        [InlineKeyboardButton("📋 لیست",          callback_data="adm_list")],
        [InlineKeyboardButton("🔙 برگشت",         callback_data="menu_back")],
    ])

# ── state ──────────────────────────────────────
ST_SEARCH = "ST_SEARCH"
ST_AADADD = "ST_ADADD"
ST_ADREM  = "ST_ADREM"

# ── هندلرها ───────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        try:
            un = f"@{u.username}" if u.username else "—"
            await ctx.bot.send_message(
                OWNER_ID,
                f"🚨 *کاربر ناشناس*\n👤 {u.full_name}\n🆔 `{u.id}`\n📛 {un}",
                parse_mode=ParseMode.MARKDOWN
            )
        except: pass
        await update.message.reply_text(
            f"⛔️ این ربات برای شما نیست.\n"
            f"مالک این ربات @{OWNER_UN} است و شما هیچ حقی ندارید از این ربات استفاده کنید."
        )
        return
    t, *_ = stats()
    await update.message.reply_text(
        f"سلام *{u.first_name}* 👋\n\n"
        f"🗄 *ربات مدیریت اطلاعات*\n"
        f"📁 رکوردها: *{t:,}*\n\n"
        "یک گزینه انتخاب کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main(u.id)
    )

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data
    if not is_admin(uid):
        await q.edit_message_text("⛔️ دسترسی ندارید.")
        return

    if data == "menu_back":
        t, *_ = stats()
        await q.edit_message_text(
            f"🏠 *منوی اصلی*\n📁 رکوردها: *{t:,}*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(uid)
        )

    elif data == "menu_search":
        await q.edit_message_text(
            "🔍 *جستجو*\nنوع جستجو را انتخاب کنید:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_search()
        )

    elif data in FIELDS:
        label, col, icon = FIELDS[data]
        ctx.user_data["st"]  = ST_SEARCH
        ctx.user_data["col"] = col
        ctx.user_data["lbl"] = label
        await q.edit_message_text(
            f"{icon} *جستجو: {label}*\n\nعبارت را تایپ کنید:",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "menu_stats":
        t, ph, ca, ni, ac, sz = stats()
        await q.edit_message_text(
            "📊 *آمار دیتابیس*\n"
            "━━━━━━━━━━━━━━\n\n"
            f"📁 کل رکوردها:  *{t:,}*\n"
            f"📞 شماره همراه: *{ph:,}*\n"
            f"💳 شماره کارت:  *{ca:,}*\n"
            f"🪪 کد ملی:      *{ni:,}*\n"
            f"🏦 شماره حساب:  *{ac:,}*\n\n"
            f"💾 حجم دیتابیس: *{sz} MB*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")
            ]])
        )

    elif data == "menu_upload":
        ctx.user_data["st"] = None
        await q.edit_message_text(
            "📂 *آپلود فایل*\n\n"
            "فایل خود را ارسال کنید:\n"
            "`xlsx` `xls` `csv` `json` `db`",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "menu_admins":
        if uid != OWNER_ID:
            await q.answer("فقط مالک", show_alert=True); return
        await q.edit_message_text(
            "👥 *مدیریت ادمین‌ها*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admins()
        )

    elif data == "adm_list":
        admins = get_admins()
        txt = "👥 *ادمین‌ها:*\n\n" + "\n".join(f"• `{a}`" for a in admins) if admins else "هیچ ادمینی ثبت نشده."
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admins())

    elif data == "adm_add":
        ctx.user_data["st"] = ST_AADADD
        await q.edit_message_text("➕ آیدی عددی ادمین جدید را بفرستید:")

    elif data == "adm_rem":
        ctx.user_data["st"] = ST_ADREM
        await q.edit_message_text("➖ آیدی عددی ادمینی که می‌خواهید حذف کنید:")

async def msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if not is_admin(uid): return
    st   = ctx.user_data.get("st")
    text = (update.message.text or "").strip()

    if st == ST_SEARCH:
        ctx.user_data["st"] = None
        col = ctx.user_data.get("col","name")
        lbl = ctx.user_data.get("lbl","")
        if not text:
            await update.message.reply_text("❌ عبارت خالی است."); return
        w = await update.message.reply_text("🔄 در حال جستجو در دیتابیس...")
        rows = await asyncio.get_event_loop().run_in_executor(None, search, col, text)
        await w.delete()
        if not rows:
            await update.message.reply_text(
                f"❌ نتیجه‌ای برای `{text}` پیدا نشد.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back()
            ); return
        out = f"✅ *{len(rows)} نتیجه* — `{text}`\n\n"
        for i, r in enumerate(rows, 1):
            block = fmt(r, i) + "\n\n"
            if len(out) + len(block) > 3800:
                await update.message.reply_text(out, parse_mode=ParseMode.MARKDOWN)
                out = ""
            out += block
        await update.message.reply_text(out, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())

    elif st == ST_AADADD:
        ctx.user_data["st"] = None
        if not text.isdigit():
            await update.message.reply_text("❌ آیدی باید عدد باشد."); return
        a = get_admins(); a.add(int(text)); save_admins(a)
        await update.message.reply_text(f"✅ ادمین `{text}` اضافه شد.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admins())

    elif st == ST_ADREM:
        ctx.user_data["st"] = None
        if not text.isdigit():
            await update.message.reply_text("❌ آیدی باید عدد باشد."); return
        a = get_admins(); a.discard(int(text)); save_admins(a)
        await update.message.reply_text(f"✅ ادمین `{text}` حذف شد.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admins())
    else:
        await update.message.reply_text("🏠 منو:", reply_markup=kb_main(uid))

async def file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    doc = update.message.document
    if not doc: return
    name = doc.file_name or "file"
    ext  = name.rsplit(".",1)[-1].lower() if "." in name else ""
    if ext not in ("xlsx","xls","csv","json","db"):
        await update.message.reply_text("❌ فرمت پشتیبانی نمی‌شود.\nمجاز: `xlsx xls csv json db`",
            parse_mode=ParseMode.MARKDOWN); return
    sz = round((doc.file_size or 0)/1_048_576, 1)
    m = await update.message.reply_text(
        f"📥 *در حال دریافت فایل...*\n📄 `{name}` — {sz} MB",
        parse_mode=ParseMode.MARKDOWN
    )
    path = f"uploads/{uid}_{name}"
    tgf = await doc.get_file()
    await tgf.download_to_drive(path)
    await m.edit_text(
        f"✅ فایل دریافت شد!\n🔄 *در حال پردازش داده‌ها...*\n_(لطفاً صبر کنید)_",
        parse_mode=ParseMode.MARKDOWN
    )
    import time
    t0 = time.time()
    count, info = await asyncio.get_event_loop().run_in_executor(None, do_import, path)
    elapsed = round(time.time()-t0, 1)
    try: Path(path).unlink()
    except: pass
    if count < 0:
        await m.edit_text(f"❌ خطا:\n`{info}`", parse_mode=ParseMode.MARKDOWN); return
    t, *_, sz2 = stats()
    await m.edit_text(
        f"🎉 *فایل وارد شد!*\n\n"
        f"📄 `{name}`\n"
        f"📦 فرمت: `{info}`\n"
        f"✅ رکورد جدید: *{count:,}*\n"
        f"⏱ زمان: *{elapsed}s*\n\n"
        f"📊 کل دیتابیس: *{t:,}* رکورد — *{sz2} MB*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 جستجو", callback_data="menu_search"),
            InlineKeyboardButton("📊 آمار",  callback_data="menu_stats"),
        ]])
    )

# ── اجرا ──────────────────────────────────────
def main():
    init_db()
    app = (
        Application.builder().token(BOT_TOKEN)
        .read_timeout(120).write_timeout(120)
        .connect_timeout(30).build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.Document.ALL, file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

