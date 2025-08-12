# bot.py — ECCues Telegram Bot (Render, PTB v22, aiohttp webhook)
import os
import re
import asyncio
import logging
import pandas as pd
from aiohttp import web
from dotenv import load_dotenv

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ================== ENV & LOGGING ==================
load_dotenv()
BOT_TOKEN    = os.getenv("BOT_TOKEN")
CATALOG_PATH = os.getenv("CATALOG_PATH", "Exc.csv")
WEBHOOK_URL  = os.getenv("WEBHOOK_URL")  # ví dụ: https://<service>.onrender.com/telegram
PORT         = int(os.getenv("PORT", "8080"))

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
LEADTIME_TEXT = "⏳ *Thời gian sản xuất*: thường 3–4 tháng (tuỳ mẫu)."
CONTACT_TEXT = (
    "📞 *Liên hệ chốt đơn*\n"
    "Telegram: @eccues\n"
    "Facebook: https://www.facebook.com/share/1CJbMHsZEM/?mibextid=wwXIfr"
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
    # bỏ cột Unnamed
    df = df.loc[:, ~df.columns.str.contains("^Unnamed", case=False)]

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

    # đảm bảo các cột quan trọng tồn tại
    for c in ["ma", "hang_thuong", "cao_cap"]:
        if c not in df.columns:
            df[c] = ""
    return df

CATALOG = load_catalog(CATALOG_PATH)

def clean_price_text(s: str) -> str:
    """Chuẩn hóa hiển thị giá: '17m' -> '17 triệu'; '22' -> '22 triệu'."""
    if not s: return ""
    s = str(s).strip()
    m = re.search(r"(\d+)", s)
    if not m: return s
    n = m.group(1)
    return f"{n} triệu"

def find_by_sku_or_keyword(q: str, df: pd.DataFrame) -> pd.Series | None:
    """Ưu tiên khớp theo mã (chuỗi có số), nếu không thì tìm theo từ khóa trong mọi cột."""
    if df.empty: return None
    qn = q.lower()

    # 1) Thử khớp dạng có số (mã sản phẩm: 2187, Ace2187, Exc0601...)
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

    # 2) Không có mã: tìm theo từ khóa
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
    # KHÔNG nêu thời gian theo yêu cầu
    return " ".join(parts).strip()

def detect_variant(txt: str) -> str | None:
    """Trả về 'hang_thuong' hoặc 'cao_cap' nếu người dùng nói rõ."""
    t = txt.lower()
    if re.search(r"\b(cao\s*cấp|cao cap)\b", t): return "cao_cap"
    if re.search(r"\b(thường|thuong)\b", t):     return "hang_thuong"
    return None

# ================== HANDLERS ==================
async def set_commands(app):
    cmds = [
        BotCommand("start", "Bắt đầu"),
        BotCommand("warranty", "Chế độ bảo hành"),
        BotCommand("leadtime", "Thời gian sản xuất"),
        BotCommand("contact", "Liên hệ")
    ]
    await app.bot.set_my_commands(cmds)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Chào 1 lần duy nhất (lưu cờ greeted theo user)
    greeted = context.user_data.get("greeted", False)
    if not greeted:
        await update.message.reply_text(
            "Chào mừng bạn đến ECCues. Gõ mã hoặc tên mẫu để xem giá (Thường/Cao cấp).",
            reply_markup=MAIN_KB
        )
        context.user_data["greeted"] = True
    else:
        # Không chào lại, chỉ nhắc ngắn
        await update.message.reply_text("Gõ mã hoặc tên mẫu để xem giá.", reply_markup=MAIN_KB)

async def cmd_warranty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WARRANTY_TEXT, parse_mode="Markdown")

async def cmd_leadtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(LEADTIME_TEXT, parse_mode="Markdown")

async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CONTACT_TEXT, parse_mode="Markdown")

async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if CATALOG.empty:
        await update.message.reply_text("Danh mục chưa có dữ liệu.")
        return
    lines = []
    for _, r in CATALOG.head(10).iterrows():
        lines.append(make_price_line(r))
    await update.message.reply_text("\n".join(lines))

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
    last_product = context.user_data.get("last_product")  # lưu mã/ngắn gọn
    if variant and last_product:
        # Gửi liên hệ ngay, không lan man
        await update.message.reply_text(
            f"Bạn chọn {('Cao cấp' if variant=='cao_cap' else 'Thường')} cho {last_product}.\n\n{CONTACT_TEXT}",
            parse_mode="Markdown"
        )
        # clear context để phiên sau không dính
        context.user_data.pop("last_product", None)
        return

    # Nhận diện sản phẩm theo mã/từ khóa
    row = find_by_sku_or_keyword(text, CATALOG)
    if row is not None:
        line = make_price_line(row)
        await update.message.reply_text(line)
        # lưu lại sản phẩm để nếu người dùng trả lời "cao cấp/thường" thì gửi contact
        context.user_data["last_product"] = (str(row.get("ma","")) or "").strip() or "mẫu đã chọn"
        # KHÔNG hỏi thời gian. Chỉ chờ khách nói "cao cấp" hoặc "thường".
        return

    # Fallback cực ngắn, không lan man
    await update.message.reply_text("Vui lòng gửi mã hoặc tên mẫu để báo giá Thường/Cao cấp.")

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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await application.initialize()
    await application.start()

    # Đăng ký webhook với Telegram
    if WEBHOOK_URL:
        await application.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        logger.info("Webhook set to %s", WEBHOOK_URL)
    else:
        logger.warning("Chưa có WEBHOOK_URL. Hãy set WEBHOOK_URL=https://<service>.onrender.com/telegram")

    # Aiohttp app: /healthz và /telegram
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