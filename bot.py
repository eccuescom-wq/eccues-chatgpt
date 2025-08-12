# bot.py — Telegram bot tư vấn cơ bi-a (Render, webhook)
import os, re, logging
from typing import Tuple
import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---- Try to import aiohttp.web for health endpoints
try:
    from aiohttp import web
except Exception:
    web = None

# ===== ENV =====
load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
MODEL           = os.getenv("MODEL", "gpt-4o-mini")
CATALOG_PATH    = os.getenv("CATALOG_PATH", "Exc.csv")

# Render cung cấp RENDER_EXTERNAL_URL, dùng làm mặc định nếu WEBHOOK_URL trống
_webhook_base   = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
if _webhook_base:
    if _webhook_base.endswith("/"):
        _webhook_base = _webhook_base[:-1]
    WEBHOOK_URL = f"{_webhook_base}/telegram"
else:
    WEBHOOK_URL = None  # sẽ cảnh báo lúc chạy

PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Thiếu BOT_TOKEN hoặc OPENAI_API_KEY trong biến môi trường.")

# ===== Logging =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("telebot")

# ===== Load CSV =====
def load_catalog(path: str) -> pd.DataFrame:
    df = None
    for enc in ["utf-8", "utf-8-sig", "latin1", "cp1252", "gb18030"]:
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except Exception:
            pass
    if df is None:
        logger.warning("⚠️ Không đọc được CSV từ %s — bot vẫn chạy nhưng /catalog sẽ trống.", path)
        df = pd.DataFrame()
    else:
        df.columns = [str(c).strip() for c in df.columns]
        rename_map = {
            "Mã":"ma","Hàng thường":"hang_thuong","Hàng th??ng":"hang_thuong",
            "Cao cấp":"cao_cap","Cao c?p":"cao_cap",
            "Thời gian làm":"thoi_gian_lam","Th?i gian làm":"thoi_gian_lam"
        }
        for k,v in rename_map.items():
            if k in df.columns:
                df = df.rename(columns={k:v})
    return df

CATALOG = load_catalog(CATALOG_PATH)

# ===== OpenAI =====
oai = OpenAI(api_key=OPENAI_API_KEY)
SYSTEM_PROMPT = """Bạn là trợ lý bán cơ bi-a của cửa hàng.
- Trả lời ngắn gọn, lịch sự, tiếng Việt tự nhiên.
- Luôn hỏi lại: khách cần hàng thường hay cao cấp, ngân sách, thời gian cần hàng.
- Thuật ngữ: Ren/joint, đầu tẩy=ferrule, nối=extension, chuôi=后把, ngọn=前肢, tay trơn=素面.
- Nếu không chắc giá cụ thể, đưa khung giá và bước chốt: xác nhận mã/gỗ/leadtime/đặt cọc."""
def llm_reply(user_text: str, extra_context: str = "") -> str:
    prompt = f"Khách hỏi: {user_text}\n\nDanh mục (trích yếu):\n{extra_context}"
    resp = oai.responses.create(
        model=MODEL,
        input=[{"role":"system","content":SYSTEM_PROMPT},
               {"role":"user","content":prompt}],
    )
    try:
        return resp.output_text.strip()
    except Exception:
        return str(resp)

# ===== Search + Pagination =====
PAGE_SIZE = 10
def format_item_row(r: pd.Series) -> str:
    def g(col): return str(r.get(col,"")).strip()
    ma, ht, cc, tg = g("ma"), g("hang_thuong"), g("cao_cap"), g("thoi_gian_lam")
    extra=[]
    for c in CATALOG.columns:
        if c not in ["ma","hang_thuong","cao_cap","thoi_gian_lam"]:
            v=g(c)
            if v and v.lower()!="nan": extra.append(f"{c}: {v}")
    line=f"• {ma or '[không có mã]'}"
    if ht: line+=f" | Hàng thường: {ht}"
    if cc: line+=f" | Cao cấp: {cc}"
    if tg: line+=f" | Thời gian: {tg}"
    return (line+(" | "+" | ".join(extra) if extra else "")).strip()

def build_page(df: pd.DataFrame, page:int)->tuple[str,InlineKeyboardMarkup|None]:
    total=len(df)
    if total==0: return "Không có dữ liệu.", None
    maxp=(total+PAGE_SIZE-1)//PAGE_SIZE
    page=max(1,min(page,maxp))
    start=(page-1)*PAGE_SIZE
    chunk=df.iloc[start:start+PAGE_SIZE]
    lines=[format_item_row(r) for _,r in chunk.iterrows()]
    text=f"📚 Trang {page}/{maxp} — Tổng {total} sản phẩm\n"+"\n".join(lines)
    btn=[]; row=[]
    if page>1: row.append(InlineKeyboardButton("⬅️ Trang trước", callback_data=f"CATALOG|P={page-1}"))
    if page<maxp: row.append(InlineKeyboardButton("Trang sau ➡️", callback_data=f"CATALOG|P={page+1}"))
    if row: btn.append(row)
    return text, InlineKeyboardMarkup(btn) if btn else None

def search_df(q:str, df:pd.DataFrame)->pd.DataFrame:
    if df.empty: return df
    qn=q.lower(); mask=pd.Series([False]*len(df))
    for col in df.columns:
        try:
            mask = mask | df[col].astype(str).str.lower().str.contains(qn, na=False)
        except Exception:
            pass
    return df[mask]

# ===== Menu (trên trái + bàn phím nhanh) =====
MENU_LABELS={
    "catalog":"📦 Danh sách sản phẩm",
    "warranty":"🛡️ Chế độ bảo hành",
    "leadtime":"⏱️ Thời gian sản xuất",
    "contact":"📞 Liên hệ",
}
MAIN_KB=ReplyKeyboardMarkup(
    [[KeyboardButton(MENU_LABELS["catalog"])],
     [KeyboardButton(MENU_LABELS["warranty"]), KeyboardButton(MENU_LABELS["leadtime"])],
     [KeyboardButton(MENU_LABELS["contact"])]],
    resize_keyboard=True,
)

WARRANTY_TEXT=os.getenv("WARRANTY_TEXT","🛡️ Bảo hành 12 tháng (lỗi kỹ thuật). Không áp dụng hao mòn/va đập/ngấm nước. Hỗ trợ cân chỉnh trọn đời.")
LEADTIME_TEXT=os.getenv("LEADTIME_TEXT","⏱️ Hàng thường: 2–3 tháng; Cao cấp: 3–4 tháng (tuỳ mẫu gỗ & inlay).")
CONTACT_TEXT=os.getenv("CONTACT_TEXT","📞 Zalo/Telegram: @yourshop | Hotline: 09xx xxx xxx | Địa chỉ xưởng: ...")

LIST_PAT=re.compile(r"(có\s+những\s+sản\s+phẩm|cung cấp sản phẩm|danh\s*sách|list|catalog)", re.I)

async def set_bot_commands(app):
    cmds=[BotCommand("start","Bắt đầu"),BotCommand("help","Hướng dẫn"),BotCommand("menu","Hiện menu"),
          BotCommand("catalog","Danh sách"),BotCommand("search","Tìm kiếm"),
          BotCommand("warranty","Bảo hành"),BotCommand("leadtime","Thời gian"),BotCommand("contact","Liên hệ")]
    await app.bot.set_my_commands(cmds)

# ===== Handlers =====
async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào bạn 👋 Dùng menu bên trái hoặc lệnh:\n"
        "/catalog, /search <từ khoá>, /warranty, /leadtime, /contact",
        reply_markup=MAIN_KB)

async def cmd_help(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ví dụ: /search ebony, /search 2187", reply_markup=MAIN_KB)

async def cmd_menu(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Menu nhanh:", reply_markup=MAIN_KB)

async def cmd_catalog(update:Update, context:ContextTypes.DEFAULT_TYPE):
    context.user_data["catalog_view"]={"mode":"ALL"}
    text,kb=build_page(CATALOG,1)
    await update.message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)

async def cmd_search(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Dùng: /search <từ khoá>"); return
    q=" ".join(context.args).strip()
    results=search_df(q, CATALOG)
    context.user_data["catalog_view"]={"mode":"SEARCH","query":q}
    context.user_data["last_results"]=results
    if results.empty:
        await update.message.reply_text(f"Không thấy kết quả cho “{q}”", reply_markup=MAIN_KB); return
    text,kb=build_page(results,1)
    await update.message.reply_text(f"Kết quả cho “{q}”:\n{text}", reply_markup=kb, disable_web_page_preview=True)

async def cmd_warranty(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WARRANTY_TEXT, reply_markup=MAIN_KB)

async def cmd_leadtime(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(LEADTIME_TEXT, reply_markup=MAIN_KB)

async def cmd_contact(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CONTACT_TEXT, reply_markup=MAIN_KB)

async def on_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    try:
        page=int((q.data or "").split("P=",1)[1])
    except Exception:
        page=1
    view=context.user_data.get("catalog_view",{"mode":"ALL"})
    df=context.user_data.get("last_results", CATALOG) if view.get("mode")=="SEARCH" else CATALOG
    text,kb=build_page(df,page)
    await q.edit_message_text(text=text, reply_markup=kb, disable_web_page_preview=True)

async def on_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt=(update.message.text or "").strip()
    if txt==MENU_LABELS["catalog"]: await cmd_catalog(update,context); return
    if txt==MENU_LABELS["warranty"]: await cmd_warranty(update,context); return
    if txt==MENU_LABELS["leadtime"]: await cmd_leadtime(update,context); return
    if txt==MENU_LABELS["contact"]: await cmd_contact(update,context); return
    if LIST_PAT.search(txt): await cmd_catalog(update,context); return
    found = search_df(txt, CATALOG).head(10)
    extra = "\n".join(format_item_row(r) for _, r in found.iterrows())
    answer = llm_reply(txt, extra_context=extra)
    await update.message.reply_text(answer, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ===== Main (webhook với aiohttp riêng, có / và /healthz) =====
import asyncio
from aiohttp import web

async def _post_init(app):
    await set_bot_commands(app)

async def health(request):
    return web.Response(text="ok")

async def amain():
    # 1) Tạo Application (PTB)
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()

    # 2) Khởi động vòng đời PTB (custom server -> tự init/start/stop)
    await application.initialize()
    await application.start()

    if not WEBHOOK_URL:
        # Render nên set WEBHOOK_URL = https://<service>.onrender.com/telegram
        print("⚠️ Chưa có WEBHOOK_URL. Hãy đặt biến môi trường WEBHOOK_URL và redeploy.")
    else:
        # 3) Đăng ký webhook với Telegram
        await application.bot.set_webhook(WEBHOOK_URL)

    # 4) Tạo web server aiohttp và gắn routes
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/healthz", health)
    # ĐÂY là handler webhook của PTB (nhận POST từ Telegram)
    web_app.router.add_post("/telegram", application.webhook_handler())

    # 5) Run server
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    # 6) Treo tiến trình
    try:
        await asyncio.Event().wait()
    finally:
        # 7) Shutdown gọn gàng nếu Render stop
        await application.stop()
        await application.shutdown()
        await runner.cleanup()

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()
