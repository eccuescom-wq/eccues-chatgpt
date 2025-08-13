# bot.py — ECCues Telegram Bot (Render, PTB v22, aiohttp webhook, có phân trang)
import os
import re
import asyncio
import logging
import pandas as pd
from aiohttp import web
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, BotCommand
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ================== ENV & LOGGING ==================
load_dotenv()
BOT_TOKEN    = os.getenv("BOT_TOKEN")
CATALOG_PATH = os.getenv("CATALOG_PATH", "Exc.csv")

# Lấy URL webhook từ ENV hoặc RENDER_EXTERNAL_URL; đảm bảo kết thúc bằng /telegram
WEBHOOK_URL  = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
if WEBHOOK_URL:
    WEBHOOK_URL = WEBHOOK_URL.rstrip("/")
    if not WEBHOOK_URL.endswith("/telegram"):
        WEBHOOK_URL = WEBHOOK_URL + "/telegram"

PORT         = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Thiếu BOT_TOKEN trong biến môi trường.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("eccues-bot")

# ================== NỘI DUNG CỐ ĐỊNH ==================
MENU_LABELS = {
    "catalog":  "📋 Danh sách sản phẩm",
    "warranty": "🛡️ Chế độ bảo hành",
    "leadtime": "⏳ Thời gian sản xuất",
    "contact":  "📞 Liên hệ"
}

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(MENU_LABELS["catalog"])],
        [KeyboardButton(MENU_LABELS["warranty"])],
        [KeyboardButton(MENU_LABELS["leadtime"])],
        [KeyboardButton(MENU_LABELS["contact"])],
    ],
    resize_keyboard=True
)

WARRANTY_TEXT = (
    "🛡️ *Chế độ bảo hành*\n"
    "- Hàng cao cấp: cam kết chất lượng giống chính hãng >95%\n"
    "- Hàng trung bình: >90%\n"
    "- Zen: >90%\n"
    "- Sơn lại miễn phí 1 lần (thời gian không quá 1 năm)"
)
LEADTIME_TEXT = "⏳ *Thời gian sản xuất*: thường 3–4 tháng (tuỳ mẫu)."  # Không hỏi thời gian khi tư vấn
CONTACT_TEXT = (
    "📞 *Liên hệ chốt đơn*\n"
    "Telegram: @eccues\n"
    "Facebook: https://www.facebook.com/ord.exc"
)

# ================== ĐỌC CSV & TIỆN ÍCH ==================
def load_catalog(path: str) -> pd.DataFrame:
    df = None
    for enc in ["utf-8", "utf-8-sig", "latin1", "cp1252", "gb18030"]:
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except Exception:
            pass
    if df is None:
        logger.warning("Không đọc được CSV '%s'. BOT vẫn chạy nhưng không tra được sản phẩm.", path)
        df = pd.DataFrame()

    # dọn cột
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, ~df.columns.str.contains("^Unnamed", case=False)]

    # chuẩn tên cột phổ biến
    rename_map = {
        "Mã": "ma",
        "Hàng thường": "hang_thuong",
        "Hàng th??ng": "hang_thuong",
        "Cao cấp": "cao_cap",
        "Cao c?p": "cao_cap",
        "Thời gian làm": "thoi_gian_lam",
        "Th?i gian làm": "thoi_gian_lam",
    }
    for k, v in rename_map.items():
        if k in df.columns:
            df = df.rename(columns={k: v})

    # đảm bảo cột chính tồn tại
    for c in ["ma", "hang_thuong", "cao_cap"]:
        if c not in df.columns:
            df[c] = ""
    return df

CATALOG = load_catalog(CATALOG_PATH)

def clean_price_text(s: str) -> str:
    """'17m'/'17' -> '17 triệu' (chỉ hiển thị ngắn gọn)."""
    if not s: return ""
    s = str(s).strip()
    m = re.search(r"(\d+)", s)
    if not m: return s
    return f"{m.group(1)} triệu"

def find_by_sku_or_keyword(q: str, df: pd.DataFrame) -> pd.Series | None:
    """Ưu tiên khớp theo mã (chuỗi có số), nếu không thì tìm theo từ khóa."""
    if df.empty: return None
    qn = q.lower()

    # 1) Dạng mã có số (2187, Ace2187, Exc0601...)
    m = re.search(r"\b([A-Za-z]*\d{2,}[A-Za-z0-9]*)\b", q)
    if m:
        key = m.group(1).lower()
        mask = pd.Series([False]*len(df))
        for c in df.columns:
            try:
                mask |= df[c].astype(str).str.lower().str.contains(key, na=False)
            except Exception:
                pass
        subset = df[mask]
        if not subset.empty:
            return subset.iloc[0]

    # 2) Không có mã: tìm theo keyword
    mask = pd.Series([False]*len(df))
    for c in df.columns:
        try:
            mask |= df[c].astype(str).str.lower().str.contains(qn, na=False)
        except Exception:
            pass
    subset = df[mask]
    if not subset.empty:
        return subset.iloc[0]
    return None

def make_price_line(row: pd.Series) -> str:
    ma = (str(row.get("ma","")) or "").strip() or "(không mã)"
    ht = clean_price_text(row.get("hang_thuong",""))
    cc = clean_price_text(row.get("cao_cap",""))
    parts = [ma + ":"]
    if ht: parts.append(f"Thường {ht}.")
    if cc: parts.append(f"Cao cấp {cc}.")
    return " ".join(parts).strip()

def detect_variant(txt: str) -> str | None:
    """Trả về 'hang_thuong' hoặc 'cao_cap' nếu người dùng nói rõ."""
    t = txt.lower()
    if re.search(r"\b(cao\s*cấp|cao cap)\b", t): return "cao_cap"
    if re.search(r"\b(thường|thuong)\b", t):     return "hang_thuong"
    return None

# ================== PHÂN TRANG DANH MỤC ==================
PAGE_SIZE = 10

def build_catalog_page(df: pd.DataFrame, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(df)
    if total == 0:
        return "Danh mục trống.", None
    max_page = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, max_page))
    start = (page - 1) * PAGE_SIZE
    chunk = df.iloc[start:start + PAGE_SIZE]

    lines = [make_price_line(r) for _, r in chunk.iterrows()]
    header = f"📚 Trang {page}/{max_page} — Tổng {total} SP"
    text = header + "\n" + "\n".join(lines)

    btn_row = []
    if page > 1:
        btn_row.append(InlineKeyboardButton("⬅️ Trang trước", callback_data=f"CAT|P={page-1}"))
    if page < max_page:
        btn_row.append(InlineKeyboardButton("Trang sau ➡️", callback_data=f"CAT|P={page+1}"))
    kb = InlineKeyboardMarkup([btn_row]) if btn_row else None
    return text, kb

# ================== HANDLERS ==================
async def set_commands(app):
    cmds = [
        BotCommand("start",    "Bắt đầu"),
        BotCommand("catalog",  "Danh sách sản phẩm"),
        BotCommand("warranty", "Chế độ bảo hành"),
        BotCommand("leadtime", "Thời gian sản xuất"),
        BotCommand("contact",  "Liên hệ"),
    ]
    await app.bot.set_my_commands(cmds)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Chào 1 lần duy nhất
    greeted = context.user_data.get("greeted", False)
    if not greeted:
        await update.message.reply_text(
            "Chào mừng bạn đến ECCues. Gõ mã hoặc tên mẫu để xem giá (Thường/Cao cấp).",
            reply_markup=MAIN_KB
        )
        context.user_data["greeted"] = True
    else:
        await update.message.reply_text("Gõ mã hoặc tên mẫu để xem giá.", reply_markup=MAIN_KB)

async def cmd_warranty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WARRANTY_TEXT, parse_mode="Markdown")

async def cmd_leadtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(LEADTIME_TEXT, parse_mode="Markdown")

async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CONTACT_TEXT, parse_mode="Markdown")

async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = build_catalog_page(CATALOG, page=1)
    await update.message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)

async def on_catalog_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    try:
        page = int(data.split("P=", 1)[1])
    except Exception:
        page = 1
    text, kb = build_catalog_page(CATALOG, page=page)
    await q.edit_message_text(text=text, reply_markup=kb, disable_web_page_preview=True)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # Menu nhanh
    if text == MENU_LABELS["catalog"]:
        await cmd_catalog(update, context);  return
    if text == MENU_LABELS["warranty"]:
        await cmd_warranty(update, context); return
    if text == MENU_LABELS["leadtime"]:
        await cmd_leadtime(update, context); return
    if text == MENU_LABELS["contact"]:
        await cmd_contact(update, context);  return

    # Nếu người dùng vừa chọn biến thể sau khi đã có sản phẩm trước đó
    variant = detect_variant(text)
    last_product = context.user_data.get("last_product")
    if variant and last_product:
        await update.message.reply_text(
            f"Bạn chọn {('Cao cấp' if variant=='cao_cap' else 'Thường')} cho {last_product}.\n\n{CONTACT_TEXT}",
            parse_mode="Markdown"
        )
        context.user_data.pop("last_product", None)
        return

    # Nhận diện sản phẩm theo mã/từ khóa → trả giá ngắn gọn
    row = find_by_sku_or_keyword(text, CATALOG)
    if row is not None:
        line = make_price_line(row)
        await update.message.reply_text(line)
        context.user_data["last_product"] = (str(row.get("ma","")) or "").strip() or "mẫu đã chọn"
        return

    # Fallback cực ngắn
    await update.message.reply_text("Gửi mã hoặc tên mẫu để báo giá Thường/Cao cấp.")

# ================== AIOHTTP WEB SERVER (Render) ==================
async def _post_init(app):
    await set_commands(app)

async def health(request):
    return web.Response(text="ok")

async def amain():
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()
    application.add_handler(CommandHandler("start",    cmd_start))
    application.add_handler(CommandHandler("warranty", cmd_warranty))
    application.add_handler(CommandHandler("leadtime", cmd_leadtime))
    application.add_handler(CommandHandler("contact",  cmd_contact))
    application.add_handler(CommandHandler("catalog",  cmd_catalog))
    application.add_handler(CallbackQueryHandler(on_catalog_nav, pattern=r"^CAT\|P=\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await application.initialize()
    await application.start()

    # Đăng ký webhook với Telegram
    if WEBHOOK_URL:
        await application.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        logger.info("Webhook set to %s", WEBHOOK_URL)
    else:
        logger.warning("Chưa có WEBHOOK_URL. Set WEBHOOK_URL=https://<service>.onrender.com/telegram")

    # Aiohttp app: /healthz, / (GET) và /telegram (POST)
    async def telegram_webhook(request):
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return web.Response(text="ok")

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/healthz", health)
    web_app.router.add_post("/telegram", telegram_webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("HTTP server started on 0.0.0.0:%s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await application.stop()
        await application.shutdown()
        await runner.cleanup()

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()
