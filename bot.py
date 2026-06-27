"""
╔══════════════════════════════════════════════════════╗
║         ULTRA BOT — Maximum Power Edition            ║
║         نوشته شده برای حجم داده بالا                 ║
╚══════════════════════════════════════════════════════╝
"""
import os, io, json, bz2, zipfile, sqlite3, logging, asyncio, time
from pathlib import Path
from typing import Optional
import pandas as pd

try:
    import rarfile
    RAR_OK = True
except ImportError:
    RAR_OK = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ──────────────────────────────────────────────────────
#  لاگ
# ──────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("data/bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────
#  تنظیمات
# ──────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN",      "YOUR_BOT_TOKEN_HERE")
OWNER_ID       = int(os.getenv("OWNER_ID",   "0"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "historyoflx0")   # بدون @

DB_FILE     = "data/main.db"
ADMINS_FILE = "data/admins.json"
UPLOAD_DIR  = Path("uploads")

for d in ("data", "uploads"):
    Path(d).mkdir(exist_ok=True)

CHUNK_ROWS   = 5_000          # هر بار چند رکورد به دیتابیس بنویس
MAX_TG_SIZE  = 2_000_000_000  # تلگرام ۲ گیگ محدودیت داره

# ──────────────────────────────────────────────────────
#  مدیریت ادمین
# ──────────────────────────────────────────────────────
def load_admins() -> set:
    if Path(ADMINS_FILE).exists():
        with open(ADMINS_FILE) as f:
            return set(json.load(f))
    return set()

def save_admins(admins: set):
    with open(ADMINS_FILE, "w") as f:
        json.dump(list(admins), f)

def is_admin(uid: int) -> bool:
    return uid == OWNER_ID or uid in load_admins()

# ──────────────────────────────────────────────────────
#  دیتابیس
# ──────────────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")   # 64MB cache
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn

def init_db():
    with get_conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name      TEXT DEFAULT '',
                phone          TEXT DEFAULT '',
                card_number    TEXT DEFAULT '',
                national_id    TEXT DEFAULT '',
                account_number TEXT DEFAULT '',
                amount         TEXT DEFAULT '',
                date           TEXT DEFAULT '',
                description    TEXT DEFAULT '',
                extra          TEXT DEFAULT '',
                source_file    TEXT DEFAULT ''
            )
        """)
        # ایندکس‌ها برای جستجوی سریع
        indexes = [
            ("idx_name",    "full_name"),
            ("idx_phone",   "phone"),
            ("idx_card",    "card_number"),
            ("idx_natid",   "national_id"),
            ("idx_account", "account_number"),
        ]
        for idx_name, col in indexes:
            c.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON records({col})")
        # اضافه کردن ستون‌های جدید اگه نبودن
        existing = {r[1] for r in c.execute("PRAGMA table_info(records)")}
        for col in ("account_number", "source_file"):
            if col not in existing:
                c.execute(f"ALTER TABLE records ADD COLUMN {col} TEXT DEFAULT ''")
    log.info("✅ دیتابیس و ایندکس‌ها آماده‌اند.")

def db_stats() -> dict:
    with get_conn() as c:
        total = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        ph    = c.execute("SELECT COUNT(*) FROM records WHERE phone!=''").fetchone()[0]
        ca    = c.execute("SELECT COUNT(*) FROM records WHERE card_number!=''").fetchone()[0]
        ni    = c.execute("SELECT COUNT(*) FROM records WHERE national_id!=''").fetchone()[0]
        ac    = c.execute("SELECT COUNT(*) FROM records WHERE account_number!=''").fetchone()[0]
        size  = Path(DB_FILE).stat().st_size if Path(DB_FILE).exists() else 0
    return {"total": total, "phones": ph, "cards": ca,
            "ids": ni, "accounts": ac, "size_mb": round(size/1_048_576, 1)}

# ──────────────────────────────────────────────────────
#  نگاشت ستون
# ──────────────────────────────────────────────────────
COLUMN_MAP = {
    # نام
    "نام":"full_name","نام و نام خانوادگی":"full_name","نام کامل":"full_name",
    "اسم":"full_name","name":"full_name","full_name":"full_name","fullname":"full_name",
    "firstname":"full_name","lastname":"full_name","first_name":"full_name","last_name":"full_name",
    # موبایل
    "تلفن":"phone","موبایل":"phone","شماره موبایل":"phone","شماره همراه":"phone",
    "همراه":"phone","phone":"phone","mobile":"phone","tel":"phone","شماره":"phone",
    "phonenumber":"phone","phone_number":"phone","cellphone":"phone",
    # کارت
    "شماره کارت":"card_number","کارت":"card_number","card":"card_number",
    "card_number":"card_number","cardnumber":"card_number","شماره‌کارت":"card_number",
    # کد ملی
    "کد ملی":"national_id","ملی":"national_id","کدملی":"national_id","کد_ملی":"national_id",
    "national_id":"national_id","nationalid":"national_id","national":"national_id",
    "codemeli":"national_id","code_meli":"national_id",
    # شماره حساب
    "شماره حساب":"account_number","حساب":"account_number","شماره‌حساب":"account_number",
    "account":"account_number","account_number":"account_number","accountnumber":"account_number",
    "iban":"account_number","شبا":"account_number",
    # مبلغ
    "مبلغ":"amount","amount":"amount","واریز":"amount","مقدار":"amount",
    "price":"amount","value":"amount","مبلغ واریزی":"amount",
    # تاریخ
    "تاریخ":"date","date":"date","datetime":"date","تاریخ واریز":"date","created_at":"date",
    # توضیحات
    "توضیحات":"description","description":"description","desc":"description",
    "توضیح":"description","note":"description","notes":"description",
}
KNOWN_COLS = set(COLUMN_MAP.values()) | {"id","source_file"}

def normalize_df(df: pd.DataFrame, source: str = "") -> pd.DataFrame:
    rename = {}
    for col in df.columns:
        key = col.strip().lower().replace("\u200c", "").replace("\u200b", "")
        if key in COLUMN_MAP:
            rename[col] = COLUMN_MAP[key]
    df = df.rename(columns=rename)
    # ستون‌های اجباری
    for c in ["full_name","phone","card_number","national_id",
              "account_number","amount","date","description"]:
        if c not in df.columns:
            df[c] = ""
    # ستون‌های اضافه → extra
    extra_cols = [c for c in df.columns if c not in KNOWN_COLS]
    df["extra"] = df[extra_cols].astype(str).agg(" | ".join, axis=1) if extra_cols else ""
    df["source_file"] = source
    # پاکسازی NaN
    df = df.fillna("").astype(str)
    df = df.replace("nan", "").replace("None", "")
    return df

# ──────────────────────────────────────────────────────
#  ایمپورت — پشتیبانی از فایل‌های بزرگ با chunk
# ──────────────────────────────────────────────────────
ENCODINGS = ("utf-8", "utf-8-sig", "windows-1256", "windows-1252", "latin-1")

def read_csv_smart(data: bytes, source: str) -> int:
    """CSV را با تشخیص خودکار encoding و chunk بخواند."""
    total = 0
    for enc in ENCODINGS:
        try:
            reader = pd.read_csv(
                io.BytesIO(data), dtype=str, encoding=enc,
                chunksize=CHUNK_ROWS, low_memory=False,
                on_bad_lines="skip",
            )
            conn = get_conn()
            for chunk in reader:
                chunk = normalize_df(chunk, source)
                chunk.to_sql("records", conn, if_exists="append",
                             index=False, method="multi")
                total += len(chunk)
            conn.close()
            return total
        except (UnicodeDecodeError, Exception):
            continue
    raise ValueError("هیچ encoding مناسبی پیدا نشد.")

def read_excel_chunked(data: bytes, source: str) -> int:
    """Excel را با chunk بخواند."""
    df = pd.read_excel(io.BytesIO(data), dtype=str, engine="openpyxl")
    total = 0
    conn = get_conn()
    for start in range(0, len(df), CHUNK_ROWS):
        chunk = normalize_df(df.iloc[start:start+CHUNK_ROWS].copy(), source)
        chunk.to_sql("records", conn, if_exists="append", index=False, method="multi")
        total += len(chunk)
    conn.close()
    return total

def read_sqlite_file(filepath: str, source: str) -> int:
    src   = sqlite3.connect(filepath)
    tabs  = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    total = 0
    conn  = get_conn()
    for t in tabs:
        count = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for offset in range(0, count, CHUNK_ROWS):
            df = pd.read_sql(
                f"SELECT * FROM {t} LIMIT {CHUNK_ROWS} OFFSET {offset}",
                src, dtype=str
            )
            df = normalize_df(df, source)
            df.to_sql("records", conn, if_exists="append", index=False, method="multi")
            total += len(df)
    src.close()
    conn.close()
    return total

def process_inner_file(data: bytes, inner_name: str, source: str) -> int:
    ext = inner_name.rsplit(".", 1)[-1].lower() if "." in inner_name else ""
    if ext == "csv":
        return read_csv_smart(data, source)
    if ext in ("xlsx", "xls"):
        return read_excel_chunked(data, source)
    if ext == "json":
        df = pd.read_json(io.BytesIO(data), dtype=str)
        df = normalize_df(df, source)
        conn = get_conn()
        df.to_sql("records", conn, if_exists="append", index=False, method="multi")
        conn.close()
        return len(df)
    return 0

def import_file(filepath: str) -> tuple[int, str, str]:
    """
    Returns: (count, format_str, error_msg)
    """
    name = Path(filepath).name
    ext  = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    source = name

    try:
        # ── مستقیم ──────────────────────────
        if ext in ("xlsx", "xls"):
            with open(filepath, "rb") as f:
                data = f.read()
            return read_excel_chunked(data, source), "Excel", ""

        if ext == "csv":
            with open(filepath, "rb") as f:
                data = f.read()
            return read_csv_smart(data, source), "CSV", ""

        if ext == "json":
            df = pd.read_json(filepath, dtype=str)
            df = normalize_df(df, source)
            conn = get_conn()
            df.to_sql("records", conn, if_exists="append", index=False, method="multi")
            conn.close()
            return len(df), "JSON", ""

        if ext == "db":
            return read_sqlite_file(filepath, source), "SQLite", ""

        # ── bz2 ─────────────────────────────
        if ext == "bz2":
            inner = name[:-4]
            with bz2.open(filepath, "rb") as f:
                data = f.read()
            n = process_inner_file(data, inner, source)
            return n, f"BZ2/{inner.rsplit('.',1)[-1].upper()}", ""

        # ── zip / z01-z99 ────────────────────
        if ext == "zip" or (len(ext) >= 2 and ext[0] == "z" and ext[1:].isdigit()):
            if not zipfile.is_zipfile(filepath):
                return 0, ext, "فایل ZIP معتبر نیست"
            total = 0
            with zipfile.ZipFile(filepath, "r") as zf:
                for inner in zf.namelist():
                    if inner.endswith("/"):
                        continue
                    data = zf.read(inner)
                    total += process_inner_file(data, inner, f"{source}/{inner}")
            return total, f"ZIP({ext.upper()})", ""

        # ── rar ──────────────────────────────
        if ext == "rar":
            if not RAR_OK:
                return 0, "RAR", "کتابخانه rarfile نصب نیست"
            total = 0
            with rarfile.RarFile(filepath, "r") as rf:
                for inner in rf.namelist():
                    data = rf.read(inner)
                    total += process_inner_file(data, inner, f"{source}/{inner}")
            return total, "RAR", ""

        return 0, ext, "فرمت پشتیبانی نمی‌شود"

    except MemoryError:
        return -1, ext, "حافظه کافی نیست — فایل خیلی بزرگ است"
    except Exception as e:
        log.exception("import_file error")
        return -1, ext, str(e)

# ──────────────────────────────────────────────────────
#  جستجو
# ──────────────────────────────────────────────────────
SEARCH_FIELDS = {
    "stype_name":    ("نام و نام خانوادگی", "full_name",      "👤"),
    "stype_natid":   ("کد ملی",             "national_id",    "🪪"),
    "stype_phone":   ("شماره همراه",        "phone",          "📞"),
    "stype_card":    ("شماره کارت",         "card_number",    "💳"),
    "stype_account": ("شماره حساب",         "account_number", "🏦"),
}

def search_db(col: str, query: str, limit: int = 50) -> list[dict]:
    q = f"%{query.strip()}%"
    with get_conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"SELECT * FROM records WHERE {col} LIKE ? LIMIT ?",
            (q, limit)
        ).fetchall()
    return [dict(r) for r in rows]

# ──────────────────────────────────────────────────────
#  فرمت خروجی
# ──────────────────────────────────────────────────────
SEP = "┄" * 26

def fmt_record(r: dict, i: int) -> str:
    lines = [f"*#{i}*  ━━━━━━━━━━━━━━━━━━━━━━"]
    if r.get("full_name"):      lines.append(f"👤  `{r['full_name']}`")
    if r.get("phone"):          lines.append(f"📞  `{r['phone']}`")
    if r.get("card_number"):    lines.append(f"💳  `{r['card_number']}`")
    if r.get("national_id"):    lines.append(f"🪪  `{r['national_id']}`")
    if r.get("account_number"): lines.append(f"🏦  `{r['account_number']}`")
    if r.get("amount"):         lines.append(f"💰  `{r['amount']}`")
    if r.get("date"):           lines.append(f"📅  `{r['date']}`")
    if r.get("description"):    lines.append(f"📝  {r['description']}")
    if r.get("extra") and r["extra"].replace("|","").strip():
        lines.append(f"ℹ️  {r['extra'][:200]}")
    if r.get("source_file"):    lines.append(f"📁  _{r['source_file']}_")
    return "\n".join(lines)

async def send_results(msg_obj, results: list[dict], query: str):
    if not results:
        await msg_obj.reply_text(
            f"❌ نتیجه‌ای برای `{query}` در دیتابیس پیدا نشد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb()
        )
        return

    header = (
        f"✅ *{len(results)} نتیجه* یافت شد\n"
        f"🔎 جستجو: `{query}`\n\n"
    )
    chunks, cur = [], header
    for i, r in enumerate(results, 1):
        block = fmt_record(r, i) + f"\n{SEP}\n\n"
        if len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = ""
        cur += block
    if cur:
        chunks.append(cur)

    for idx, chunk in enumerate(chunks):
        kb = back_kb() if idx == len(chunks) - 1 else None
        await msg_obj.reply_text(chunk, parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=kb)
        if len(chunks) > 1:
            await asyncio.sleep(0.3)

# ──────────────────────────────────────────────────────
#  کیبوردها
# ──────────────────────────────────────────────────────
def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 جستجوی جدید", callback_data="menu_search"),
        InlineKeyboardButton("🏠 منو", callback_data="menu_back"),
    ]])

def main_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍 جستجو هوشمند",    callback_data="menu_search"),
         InlineKeyboardButton("📊 آمار دیتابیس",     callback_data="menu_stats")],
        [InlineKeyboardButton("📂 آپلود فایل/آرشیو", callback_data="menu_upload")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("👥 مدیریت ادمین‌ها", callback_data="menu_admins")])
    if uid == OWNER_ID:
        rows.append([InlineKeyboardButton("🗄 فایل‌های آپلود شده", callback_data="menu_sources")])
        rows.append([InlineKeyboardButton("⚙️ تنظیمات پیشرفته",  callback_data="menu_advanced")])
    return InlineKeyboardMarkup(rows)

def search_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 نام و نام خانوادگی", callback_data="stype_name")],
        [InlineKeyboardButton("🪪 کد ملی",             callback_data="stype_natid")],
        [InlineKeyboardButton("📞 شماره همراه",        callback_data="stype_phone")],
        [InlineKeyboardButton("💳 شماره کارت",         callback_data="stype_card")],
        [InlineKeyboardButton("🏦 شماره حساب",         callback_data="stype_account")],
        [InlineKeyboardButton("🔙 برگشت",              callback_data="menu_back")],
    ])

def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن ادمین",  callback_data="admin_add"),
         InlineKeyboardButton("➖ حذف ادمین",    callback_data="admin_remove")],
        [InlineKeyboardButton("📋 لیست ادمین‌ها", callback_data="admin_list")],
        [InlineKeyboardButton("🔙 برگشت",         callback_data="menu_back")],
    ])

def advanced_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 پاک کردن همه داده‌ها", callback_data="adv_clear")],
        [InlineKeyboardButton("📦 بهینه‌سازی دیتابیس",   callback_data="adv_vacuum")],
        [InlineKeyboardButton("🔙 برگشت",                 callback_data="menu_back")],
    ])

# ──────────────────────────────────────────────────────
#  state keys
# ──────────────────────────────────────────────────────
S_SEARCH   = "SEARCH"
S_ADM_ADD  = "ADM_ADD"
S_ADM_REM  = "ADM_REM"

# ──────────────────────────────────────────────────────
#  هندلرها
# ──────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        uname = f"@{user.username}" if user.username else "—"
        # اطلاع به مالک
        try:
            await ctx.bot.send_message(
                OWNER_ID,
                f"🚨 *کاربر ناشناس*\n\n"
                f"👤 نام: {user.full_name}\n"
                f"🆔 آیدی: `{user.id}`\n"
                f"📛 یوزرنیم: {uname}\n"
                f"⏰ زمان: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
        await update.message.reply_text(
            "⛔️ *دسترسی رد شد*\n\n"
            f"این ربات برای شما نیست.\n"
            f"مالک این ربات @{OWNER_USERNAME} است\n"
            "و شما هیچ حقی ندارید از این ربات استفاده کنید.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    s = db_stats()
    await update.message.reply_text(
        f"👋 سلام *{user.first_name}*\n\n"
        "╔══════════════════════╗\n"
        "║  🗄 ULTRA DATA BOT  ║\n"
        "╚══════════════════════╝\n\n"
        f"📁 رکوردها: *{s['total']:,}*  |  💾 حجم: *{s['size_mb']} MB*\n\n"
        "یک گزینه انتخاب کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_kb(user.id)
    )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data

    if not is_admin(uid):
        await q.edit_message_text("⛔️ دسترسی ندارید.")
        return

    # ── منو اصلی ────────────────────────────
    if data == "menu_back":
        s = db_stats()
        await q.edit_message_text(
            f"🏠 *منوی اصلی*\n\n"
            f"📁 رکوردها: *{s['total']:,}*  |  💾 *{s['size_mb']} MB*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_kb(uid)
        )

    elif data == "menu_search":
        await q.edit_message_text(
            "🔍 *جستجوی هوشمند*\n\nنوع جستجو را انتخاب کنید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=search_type_kb()
        )

    elif data in SEARCH_FIELDS:
        label, col, icon = SEARCH_FIELDS[data]
        ctx.user_data["state"]  = S_SEARCH
        ctx.user_data["s_col"]  = col
        ctx.user_data["s_label"]= label
        await q.edit_message_text(
            f"{icon} *جستجو: {label}*\n\n"
            "عبارت مورد نظر را تایپ کنید\n"
            "_(می‌توانید بخشی از آن را بنویسید)_",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "menu_stats":
        s = db_stats()
        await q.edit_message_text(
            "📊 *آمار دیتابیس*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📁 کل رکوردها:       *{s['total']:,}*\n"
            f"📞 دارای شماره:      *{s['phones']:,}*\n"
            f"💳 دارای کارت:       *{s['cards']:,}*\n"
            f"🪪 دارای کد ملی:     *{s['ids']:,}*\n"
            f"🏦 دارای شماره حساب: *{s['accounts']:,}*\n\n"
            f"💾 حجم دیتابیس: *{s['size_mb']} MB*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")
            ]])
        )

    elif data == "menu_upload":
        ctx.user_data["state"] = None
        await q.edit_message_text(
            "📂 *آپلود فایل یا آرشیو*\n\n"
            "فایل خود را ارسال کنید.\n\n"
            "📋 *فرمت‌های پشتیبانی شده:*\n"
            "• `xlsx` `xls` — اکسل\n"
            "• `csv` — فایل متنی\n"
            "• `json` — جیسون\n"
            "• `db` — SQLite\n"
            "• `bz2` — فشرده BZ2\n"
            "• `zip` — فشرده ZIP\n"
            "• `rar` — فشرده RAR\n"
            "• `z01` تا `z24` — آرشیو چند‌بخشی\n\n"
            "⚡️ فایل‌های بزرگ (تا ۲GB) پشتیبانی می‌شود.",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "menu_admins":
        if uid != OWNER_ID:
            await q.answer("⛔️ فقط مالک", show_alert=True)
            return
        await q.edit_message_text(
            "👥 *مدیریت ادمین‌ها*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_kb()
        )

    elif data == "menu_sources":
        if uid != OWNER_ID:
            return
        with get_conn() as c:
            rows = c.execute(
                "SELECT source_file, COUNT(*) as cnt FROM records "
                "WHERE source_file!='' GROUP BY source_file ORDER BY cnt DESC LIMIT 20"
            ).fetchall()
        if rows:
            txt = "🗄 *فایل‌های آپلود شده:*\n\n" + \
                  "\n".join(f"• `{r[0]}` — *{r[1]:,}* رکورد" for r in rows)
        else:
            txt = "هنوز فایلی آپلود نشده."
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([[
                                      InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")
                                  ]]))

    elif data == "menu_advanced":
        if uid != OWNER_ID:
            return
        await q.edit_message_text(
            "⚙️ *تنظیمات پیشرفته*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=advanced_kb()
        )

    elif data == "adv_vacuum":
        await q.edit_message_text("⏳ در حال بهینه‌سازی دیتابیس...")
        with get_conn() as c:
            c.execute("VACUUM")
            c.execute("ANALYZE")
        await q.edit_message_text(
            "✅ دیتابیس بهینه شد.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")
            ]])
        )

    elif data == "adv_clear":
        if uid != OWNER_ID:
            return
        await q.edit_message_text(
            "⚠️ *آیا مطمئن هستید؟*\n\nهمه داده‌ها پاک می‌شوند!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بله، پاک کن", callback_data="confirm_clear"),
                 InlineKeyboardButton("❌ خیر",          callback_data="menu_back")]
            ])
        )

    elif data == "confirm_clear":
        if uid != OWNER_ID:
            return
        with get_conn() as c:
            c.execute("DELETE FROM records")
        await q.edit_message_text(
            "✅ همه داده‌ها پاک شدند.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")
            ]])
        )

    elif data == "admin_list":
        admins = load_admins()
        txt = ("👥 *ادمین‌ها:*\n\n" + "\n".join(f"• `{a}`" for a in admins)) \
              if admins else "هیچ ادمینی ثبت نشده."
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=admin_kb())

    elif data == "admin_add":
        ctx.user_data["state"] = S_ADM_ADD
        await q.edit_message_text(
            "➕ آیدی عددی ادمین جدید را بفرستید:",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "admin_remove":
        ctx.user_data["state"] = S_ADM_REM
        await q.edit_message_text(
            "➖ آیدی عددی ادمین را برای حذف بفرستید:",
            parse_mode=ParseMode.MARKDOWN
        )

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    if not is_admin(uid):
        return

    state = ctx.user_data.get("state")
    text  = (update.message.text or "").strip()

    if state == S_SEARCH:
        ctx.user_data["state"] = None
        col   = ctx.user_data.get("s_col", "full_name")
        label = ctx.user_data.get("s_label", "")
        if not text:
            await update.message.reply_text("❌ لطفاً عبارتی وارد کنید.")
            return
        wait = await update.message.reply_text(
            f"🔄 *در حال جستجو...*\n🔎 {label}: `{text}`",
            parse_mode=ParseMode.MARKDOWN
        )
        results = await asyncio.get_event_loop().run_in_executor(
            None, search_db, col, text
        )
        await wait.delete()
        await send_results(update.message, results, text)

    elif state == S_ADM_ADD:
        ctx.user_data["state"] = None
        if not text.isdigit():
            await update.message.reply_text("❌ آیدی باید عدد باشد.")
            return
        a = load_admins(); a.add(int(text)); save_admins(a)
        await update.message.reply_text(
            f"✅ ادمین `{text}` اضافه شد.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb()
        )

    elif state == S_ADM_REM:
        ctx.user_data["state"] = None
        if not text.isdigit():
            await update.message.reply_text("❌ آیدی باید عدد باشد.")
            return
        a = load_admins(); a.discard(int(text)); save_admins(a)
        await update.message.reply_text(
            f"✅ ادمین `{text}` حذف شد.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb()
        )

    else:
        await update.message.reply_text(
            "🏠 منوی اصلی:", reply_markup=main_kb(uid)
        )

async def file_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    doc = update.message.document
    if not doc:
        return

    name = doc.file_name or "file"
    ext  = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    allowed = (
        {"xlsx","xls","csv","json","db","bz2","zip","rar"} |
        {f"z{str(i).zfill(2)}" for i in range(1, 25)}
    )
    if ext not in allowed:
        await update.message.reply_text(
            f"❌ فرمت `.{ext}` پشتیبانی نمی‌شود.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    size_mb = round(doc.file_size / 1_048_576, 1) if doc.file_size else 0
    msg = await update.message.reply_text(
        f"📥 *در حال دریافت فایل...*\n\n"
        f"📄 نام: `{name}`\n"
        f"💾 حجم: *{size_mb} MB*\n\n"
        f"⏳ لطفاً صبر کنید...",
        parse_mode=ParseMode.MARKDOWN
    )

    path = UPLOAD_DIR / f"{uid}_{name}"
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(str(path))

    await msg.edit_text(
        f"✅ فایل دریافت شد!\n\n"
        f"📄 `{name}` ({size_mb} MB)\n\n"
        f"🔄 *در حال پردازش و وارد کردن داده‌ها...*\n"
        f"_(این عملیات برای فایل‌های بزرگ چند دقیقه طول می‌کشد)_",
        parse_mode=ParseMode.MARKDOWN
    )

    t0 = time.time()
    count, fmt, err = await asyncio.get_event_loop().run_in_executor(
        None, import_file, str(path)
    )
    elapsed = round(time.time() - t0, 1)

    try:
        path.unlink()
    except Exception:
        pass

    if count < 0:
        await msg.edit_text(
            f"❌ *خطا در پردازش فایل*\n\n`{err}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    s = db_stats()
    await msg.edit_text(
        f"🎉 *فایل با موفقیت وارد شد!*\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📄 فایل: `{name}`\n"
        f"📦 فرمت: `{fmt}`\n"
        f"✅ رکوردهای جدید: *{count:,}*\n"
        f"⏱ زمان پردازش: *{elapsed}s*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 کل دیتابیس: *{s['total']:,}* رکورد\n"
        f"💾 حجم: *{s['size_mb']} MB*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 جستجو", callback_data="menu_search"),
             InlineKeyboardButton("📊 آمار",  callback_data="menu_stats")],
            [InlineKeyboardButton("🏠 منو",   callback_data="menu_back")],
        ])
    )

# ──────────────────────────────────────────────────────
#  اجرا
# ──────────────────────────────────────────────────────
def main():
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    log.info("🚀 ULTRA BOT در حال اجرا...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

