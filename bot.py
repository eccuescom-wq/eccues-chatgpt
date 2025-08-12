import os
import re
import pandas as pd
import asyncio
from aiohttp import web
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL", "gpt-4o-mini")
CATALOG_PATH = os.getenv("CATALOG_PATH", "Exc.csv")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))

oai = OpenAI(api_key=OPENAI_API_KEY)

# ===== MENU =====
MENU_LABELS = {
    "catalog": "📋 Danh sách sản phẩm",
    "warranty": "🛡️ Chế độ bảo hành",
    "leadtime": "⏳ Thời gian sản xuất",
    "contact": "📞 Liên hệ"
}

# ===== TEXTS =====
WARRANTY_TEXT = (
    "🛡️ *Chế độ bảo hành*\n\n"
    "- Hàng cao cấp: cam kết chất lượng giống chính hãng >95%\n"
    "- Hàng trung bình: >90%\n"
    "- Zen: >90%\n"
    "- Sơn lại miễn phí 1 lần (thời gian không quá 1 năm)"
)
LEADTIME_TEXT = (
    "⏳ *Thời gian sản xuất*\n\n"
    "Thông thường từ 3–4 tháng tùy mẫu."
)
CONTACT_TEXT = (
    "📞 *Liên hệ*\n\n"
    "Telegram: @eccues\n"
    "Facebook: https://www.facebook.com/share/1CJbMHsZEM/?mibextid=wwXIfr"
)

# ===== PROMPT =====
SYSTEM_PROMPT = """Bạn là trợ lý bán cơ bi-a. Mục tiêu: trả lời NGẮN, ĐÚNG TRỌNG TÂM.

QUY TẮC:
- Nếu bắt được mã sản phẩm → trả lời từ CSV (hàng thường/cao cấp) rồi hỏi 1 câu duy nhất để chốt.
- Nếu không bắt được mã → gợi ý 1–2 lựa chọn gần nhất từ CSV.
- Không nói giá chung chung, không mở đầu dài dòng.
- Chỉ hỏi 1 câu duy nhất ở cuối.
- Ngôn ngữ: tiếng Việt hoặc tiếng anh nếu khách hỏi bằng Tiếng Anh
- Chỉ cần "Xin chào" lần đầu tiên. Khi khách hàng hỏi có mã sản phẩm thì cho họ 2 giá Hàng thường và cao cấp luôn.
- Không cần hỏi thời gian, khi nào khách chọn hàng thường hay cao cấp mới trả lời thời gian cho họ.
"""

# ===== CSV =====
CATALOG = pd.read_csv(CATALOG_PATH)

# ===== UTILS =====
SKU_PAT = re.compile(r"\b([A-Za-z]*\d{2,}[A-Za-z0-9]*)\b", re.I)
BUDGET_PAT = re.compile(r"(\d+)(?:\s*-\s*(\d+))?\s*(m|tr|triệu|trieu)?", re.I)

def parse_budget(text: str):
    m = BUDGET_PAT.search(text)
    if not m: return None
    lo = int(m.group(1))
    hi = m.group(2)
    return (lo, int(hi) if hi else None)  # triệu

def lookup_by_sku(s: str, df: pd.DataFrame) -> pd.DataFrame:
    s = s.lower()
    mask = pd.Series([False]*len(df))
    for c in df.columns:
        try:
            mask |= df[c].astype(str).str.lower().str.contains(s, na=False)
        except Exception:
            pass
    return df[mask].head(1)

def render_csv_answer(row: pd.Series, budget=None) -> str:
    ma = str(row.get("ma","")).strip()
    ht = str(row.get("hang_thuong","")).strip()
    cc = str(row.get("cao_cap","")).strip()
    tg = str(row.get("thoi_gian_lam","")).strip()

    parts = []
    if ma: parts.append(f"{ma}:")
    if ht: parts.append(f"Hàng thường {ht}.")
    if cc: parts.append(f"Cao cấp {cc}.")
    if tg: parts.append(f"Thời gian làm {tg}.")

    # Gợi ý theo ngân sách
    if budget:
        lo, hi = budget
        def parse_price(s):
            m = re.search(r"(\d+)", s)
            return int(m.group(1)) if m else None
        ht_price = parse_price(ht)
        cc_price = parse_price(cc)
        pick = []
        if ht_price and ((hi and ht_price<=hi) or (not hi and ht_price<=lo)):
            pick.append("Hàng thường hợp ngân sách.")
        if cc_price and ((hi and cc_price<=hi) or (not hi and cc_price<=lo)):
            pick.append("Cao cấp hợp ngân sách.")
        if pick:
            parts.append(" / ".join(pick))

    parts.append("Bạn chọn hàng thường hay cao cấp?")
    return " ".join(parts)

def search_df(query, df):
    q = query.lower()
    mask = pd.Series([False]*len(df))
    for c in df.columns:
        try:
            mask |= df[c].astype(str).str.lower().str.contains(q, na=False)
        except Exception:
            pass
    return df[mask]

def llm_reply(user_text, extra_context=""):
    prompt = f"Ngữ cảnh thêm:\n{extra_context}\n\nCâu hỏi: {user_text}"
    resp = oai.responses.create(
        model=MODEL,
        input=[{"role":"system","content":SYSTEM_PROMPT},
               {"role":"user","content":prompt}],
        temperature=0.2,
        max_output_tokens=180
    )
    return resp.output_text.strip()

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[KeyboardButton(MENU_LABELS["catalog"])],
          [KeyboardButton(MENU_LABELS["warranty"])],
          [KeyboardButton(MENU_LABELS["leadtime"])],
          [KeyboardButton(MENU_LABELS["contact"])]]
    await update.message.reply_text(
        "Chào mừng bạn đến với ECCues!",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    for _, r in CATALOG.head(10).iterrows():
        ma = str(r.get("ma","")).strip()
        ht = str(r.get("hang_thuong","")).strip()
        cc = str(r.get("cao_cap","")).strip()
        tg = str(r.get("thoi_gian_lam","")).strip()
        lines.append(f"{ma}: Thường {ht}, Cao cấp {cc}, {tg}")
    await update.message.reply_text("\n".join(lines))

async def cmd_warranty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WARRANTY_TEXT, parse_mode="Markdown")

async def cmd_leadtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(LEADTIME_TEXT, parse_mode="Markdown")

async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CONTACT_TEXT, parse_mode="Markdown")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()

    if txt == MENU_LABELS["catalog"]:
        await cmd_catalog(update, context); return
    if txt == MENU_LABELS["warranty"]:
        await cmd_warranty(update, context); return
    if txt == MENU_LABELS["leadtime"]:
        await cmd_leadtime(update, context); return
    if txt == MENU_LABELS["contact"]:
        await cmd_contact(update, context); return

    budget = parse_budget(txt)
    m = SKU_PAT.search(txt)
    if m:
        sku = m.group(1)
        df1 = lookup_by_sku(sku, CATALOG)
        if not df1.empty:
            ans = render_csv_answer(df1.iloc[0], budget)
            await update.message.reply_text(ans)
            return

    found = search_df(txt, CATALOG).head(3)
    extra = "\n".join([f"{r['ma']}: Thường {r['hang_thuong']}, Cao cấp {r['cao_cap']}, {r['thoi_gian_lam']}" for _, r in found.iterrows()])
    ans = llm_reply(txt, extra_context=extra)
    await update.message.reply_text(ans)

# ===== WEBHOOK SERVER =====
async def _post_init(app):
    pass

async def health(request):
    return web.Response(text="ok")

async def amain():
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()

    # đăng ký handlers (start, catalog, warranty, leadtime, contact, on_text, ...)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # (nếu bạn có thêm các CommandHandler khác, giữ nguyên)

    await application.initialize()
    await application.start()

    # Đăng ký webhook với Telegram
    if WEBHOOK_URL:
        await application.bot.set_webhook(WEBHOOK_URL)

    # --- AioHTTP server + health + webhook ---
    async def health(request):
        return web.Response(text="ok")

    async def telegram_webhook(request):
        data = await request.json()
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
