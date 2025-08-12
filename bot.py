# bot.py — Telegram bot tư vấn cơ bi-a (webhook cho Render)
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

# ===== ENV =====
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL", "gpt-4o-mini")
CATALOG_PATH = os.getenv("CATALOG_PATH", "Exc.csv")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")          # ví dụ: https://<service>.onrender.com/telegram
PORT = int(os.getenv("PORT", "8080"))           # Render tự set PORT

if not (BOT_TOKEN and OPENAI_API_KEY):
    raise RuntimeError("Thiếu BOT_TOKEN hoặc OPENAI_API_KEY trong env")
if not WEBHOOK_URL:
    # Chấp nhận thiếu tạm thời lần deploy đầu; sẽ báo rõ ràng trong log
    print("⚠️ Chưa có WEBHOOK_URL. Sau khi có URL Render, hãy thêm env này và redeploy.")

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
        raise RuntimeError("Không đọc được CSV.")
    df.columns = [str(c).strip() for c in df.columns]
    rename_map = {
        "Mã":"ma","Hàng thường":"hang_thuong","Hàng thường":"hang_thuong",
        "Cao cấp":"cao_cap","Cao cấp":"cao_cap","Thời gian làm":"thoi_gian_lam","Thời gian làm":"thoi_gian_lam"
    }
    for k,v in rename_map.items():
        if k in df.columns: df = df.rename(columns={k:v})
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
    try: return resp.output_text.strip()
    except Exception: return str(resp)

# ===== Search + Pagination =====
PAGE_SIZE = 10
def format_item_row(r: pd.Series) -> str:
    ma = str(r.get("ma","")).strip()
    ht = str(r.get("hang_thuong","")).strip()
    cc = str(r.get("cao_cap","")).strip()
    tg = str(r.get("thoi_gian_lam","")).strip()
    extra=[]
    for c in CATALOG.columns:
        if c not in ["ma","hang_thuong","cao_cap","thoi_gian_lam"]:
            v=str(r.get(c,"")).strip()
            if v and v.lower()!="nan": extra.append(f"{c}: {v}")
    line=f"• {ma or '[không có mã]'}"
    if ht: line+=f" | Hàng thường: {ht}"
    if cc: line+=f" | Cao cấp: {cc}"
    if tg: line+=f" | Thời gian: {tg}"
    return (line+(" | "+" | ".join(extra) if extra else "")).strip()

def build_page(df: pd.DataFrame, page:int)->tuple[str,InlineKeyboardMarkup|None]:
    total=len(df); 
    if total==0: return "Không có dữ liệu.", None
    maxp=(total+PAGE_SIZE-1)//PAGE_SIZE; page=max(1,min(page,maxp))
    start=(page-1)*PAGE_SIZE; chunk=df.iloc[start:start+PAGE_SIZE]
    lines=[format_item_row(r) for _,r in chunk.iterrows()]
    text=f"📚 Trang {page}/{maxp} — Tổng {total} sản phẩm\n"+"\n".join(lines)
    btn=[]
    row=[]
    if page>1: row.append(InlineKeyboardButton("⬅️ Trang trước", callback_data=f"CATALOG|P={page-1}"))
    if page<maxp: row.append(InlineKeyboardButton("Trang sau ➡️", callback_data=f"CATALOG|P={page+1}"))
    if row: btn.append(row)
    return text, InlineKeyboardMarkup(btn) if btn else None

def search_df(q:str, df:pd.DataFrame)->pd.DataFrame:
    qn=q.lower(); mask=pd.Series([False]*len(df))
    for col in df.columns:
        try: mask = mask | df[col].astype(str).str.lower().str.contains(qn, na=False)
        except Exception: pass
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

# Nội dung tĩnh (có thể đưa vào .env hoặc sửa trực tiếp)
WARRANTY_TEXT = os.getenv(
    "WARRANTY_TEXT",
    "🛡️ Cam kết và bảo hành:\n- Tùy theo chất lượng sản phẩm. Chúng tôi cam kết đạt >95% đối với Exc và >90% đối với zencues. \n- Sơn lại miễn phí 1 lần. tối đa không quá 1 năm.",
)
LEADTIME_TEXT = os.getenv(
    "LEADTIME_TEXT",
    "⏱️ Thời gian sản xuất (tham khảo):\n- Hàng thường: 2–3 tháng.\n- Hàng cao cấp: 3–4 tháng.\n(Lịch có thể thay đổi theo mẫu gỗ & độ phức tạp inlay).",
)
CONTACT_TEXT = os.getenv(
    "CONTACT_TEXT",
    "📞 Liên hệ:\n- Telegram: @eccues\n- Sản xuất lại: China \nVui lòng nhắn mã sản phẩm + ngân sách + thời gian cần hàng.",
)

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
    try: page=int((q.data or "").split("P=",1)[1])
    except Exception: page=1
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

# ===== Main (webhook cho Render) =====
async def _post_init(app):  # set menu trái
    await set_bot_commands(app)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("catalog", cmd_catalog))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("warranty", cmd_warranty))
    app.add_handler(CommandHandler("leadtime", cmd_leadtime))
    app.add_handler(CommandHandler("contact", cmd_contact))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if not WEBHOOK_URL:
        print("❌ Chưa cấu hình WEBHOOK_URL — hãy thêm env và redeploy.")
    # Webhook mode cho Render
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=WEBHOOK_URL or f"http://localhost:{PORT}/telegram",
    )

if __name__ == "__main__":
    main()
