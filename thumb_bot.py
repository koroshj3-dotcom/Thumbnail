import os
import sys
import time
import asyncio
import logging
import sqlite3
import threading
from dotenv import load_dotenv

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait

# بارگذاری متغیرهای محیطی
load_dotenv()

API_ID_STR = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID_STR = os.environ.get("ADMIN_ID")

if not API_ID_STR or not API_HASH or not BOT_TOKEN:
    print("\n❌ خطای بحرانی: اطلاعات ورود در فایل .env پیدا نشد!\n")
    sys.exit(1)

API_ID = int(API_ID_STR)
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else 0
THUMB_FILE = "intro.png" # اسم عکسی که به عنوان کاور استفاده میشه

logging.basicConfig(level=logging.INFO)

# --- وب‌سرور برای زنده نگه داشتن در هاستینگ‌ها ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active!")
    def log_message(self, format, *args):
        return

def start_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_health_check_server, daemon=True).start()

# --- دیتابیس صف انتظار ---
def init_db():
    conn = sqlite3.connect("thumb_database.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS queue
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  chat_id INTEGER,
                  message_id INTEGER,
                  status TEXT)''')
    conn.commit()
    conn.close()

def add_to_db(user_id, chat_id, message_id):
    conn = sqlite3.connect("thumb_database.db")
    c = conn.cursor()
    c.execute("INSERT INTO queue (user_id, chat_id, message_id, status) VALUES (?, ?, ?, 'waiting')",
              (user_id, chat_id, message_id))
    item_id = c.lastrowid
    conn.commit()
    conn.close()
    return item_id

def update_db_status(item_id, status):
    conn = sqlite3.connect("thumb_database.db")
    c = conn.cursor()
    c.execute("UPDATE queue SET status = ? WHERE id = ?", (status, item_id))
    conn.commit()
    conn.close()

# --- کلاینت تلگرام ---
app = Client(
    "fast_thumb_bot", 
    api_id=API_ID, 
    api_hash=API_HASH, 
    bot_token=BOT_TOKEN,
    workers=4
)

user_sessions = {}

def get_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = {'queue': [], 'is_processing': False, 'dashboard_msg': None, 'cancel_flag': False}
    return user_sessions[user_id]

def make_progress_bar(current, total):
    percentage = current * 100 / total if total > 0 else 0
    completed = int(percentage / 10)
    bar = "█" * completed + "░" * (10 - completed)
    return f"[{bar}] {percentage:.1f}%"

async def telegram_progress(current, total, user_id, action_text, last_edit):
    session = get_session(user_id)
    if session['cancel_flag']:
        raise Exception("Cancelled")
        
    now = time.time()
    if now - last_edit[0] >= 3 or current == total:
        last_edit[0] = now
        bar = make_progress_bar(current, total)
        await update_dashboard(user_id, current_action=f"{action_text}\n{bar}")

async def update_dashboard(user_id, current_action=""):
    session = get_session(user_id)
    if not session['dashboard_msg']: return
    queue = session['queue']
    text = "📊 **داشبورد وضعیت ویدیوها**\n\n"
    for i, item in enumerate(queue):
        if item['status'] == 'completed': text += f"✅ ویدیو {i+1}: انجام شد\n"
        elif item['status'] == 'processing': text += f"🔄 ویدیو {i+1}: در حال پردازش\n{current_action}\n"
        elif item['status'] == 'waiting': text += f"🕒 ویدیو {i+1}: صف انتظار...\n"
        elif item['status'] == 'cancelled': text += f"❌ ویدیو {i+1}: لغو شد\n"
            
    text += f"\nمجموع: {len(queue)}"
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 لغو عملیات‌ها", callback_data="cancel_all")]])
    
    try: await session['dashboard_msg'].edit_text(text, reply_markup=reply_markup)
    except FloodWait as e: await asyncio.sleep(e.value)
    except Exception: pass

async def process_user_queue(client: Client, user_id: int):
    session = get_session(user_id)
    session['is_processing'] = True
    
    while True:
        waiting_items = [item for item in session['queue'] if item['status'] == 'waiting']
        if not waiting_items or session['cancel_flag']: break
            
        current_item = waiting_items[0]
        current_item['status'] = 'processing'
        db_id = current_item['db_id']
        update_db_status(db_id, 'processing')
        
        message = current_item['message']
        input_path = f"video_{message.id}.mp4"
        
        try:
            # 1. دانلود سریع
            last_edit = [0]
            input_path = await message.download(
                file_name=input_path, progress=telegram_progress, progress_args=(user_id, "📥 دانلود روی سرور...", last_edit)
            )

            # 2. آپلود با کاور جدید (بدون رندر)
            last_edit = [0]
            await client.send_video(
                chat_id=message.chat.id, 
                video=input_path, 
                thumb=THUMB_FILE, # جایگذاری کاور
                caption=message.caption or "✅ کاور اختصاصی تنظیم شد.",
                supports_streaming=True,
                progress=telegram_progress, progress_args=(user_id, "📤 ارسال به تلگرام...", last_edit)
            )
            current_item['status'] = 'completed'
            update_db_status(db_id, 'completed')
            await update_dashboard(user_id, "✅ با موفقیت ارسال شد")
            
        except Exception as e:
            if str(e) == "Cancelled":
                current_item['status'] = 'cancelled'
                update_db_status(db_id, 'cancelled')
            else:
                logging.error(f"Error: {e}")
                current_item['status'] = 'error'
                update_db_status(db_id, 'error')
        finally:
            if os.path.exists(input_path): os.remove(input_path)

    if session['cancel_flag']: await session['dashboard_msg'].edit_text("🛑 عملیات لغو شد.")
    else:
        try: await session['dashboard_msg'].delete()
        except: pass
            
    session['queue'], session['is_processing'], session['dashboard_msg'], session['cancel_flag'] = [], False, None, False

# ----------------- پنل مدیریت -----------------
@app.on_message(filters.command("id"))
async def send_user_id(_, message: Message):
    await message.reply_text(f"آیدی شما:\n`{message.from_user.id}`")

def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 وضعیت منابع", callback_data="admin_stats")],
        [InlineKeyboardButton("🔄 ری‌استارت", callback_data="admin_restart"),
         InlineKeyboardButton("🛑 خاموش", callback_data="admin_shutdown")]
    ])

@app.on_message(filters.command("admin"))
async def admin_panel_cmd(_, message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return await message.reply_text("⛔ دسترسی غیرمجاز.")
    await message.reply_text("🎛 **پنل مدیریت سریع**", reply_markup=get_admin_keyboard())

@app.on_callback_query(filters.regex("^admin_"))
async def admin_callbacks(client, callback_query: CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID: return
    action = callback_query.data.split("_")[1]
    
    if action == "stats":
        if HAS_PSUTIL:
            cpu, ram = psutil.cpu_percent(interval=0.5), psutil.virtual_memory().percent
            text = f"📊 **وضعیت سرور**\n\nCPU: {cpu}%\nRAM: {ram}%"
        else: text = "⚠️ ماژول psutil نصب نیست."
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]))
    elif action == "restart":
        await callback_query.message.edit_text("🔄 ری‌استارت شد.")
        os.execl(sys.executable, sys.executable, *sys.argv)
    elif action == "shutdown":
        await callback_query.message.edit_text("🛑 سرور خاموش شد.")
        os._exit(0)
    elif action == "back":
        await callback_query.message.edit_text("🎛 **پنل مدیریت سریع**", reply_markup=get_admin_keyboard())

# ----------------- هندلرهای اصلی -----------------
@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    await message.reply_text("سلام! ویدیو را بفرست تا کاور را روی آن تنظیم کنم 🚀")

@app.on_message(filters.video | filters.document)
async def handle_incoming_video(client: Client, message: Message):
    if message.document and not message.document.mime_type.startswith("video/"): return
    if not os.path.exists(THUMB_FILE):
        return await message.reply_text(f"❌ عکس {THUMB_FILE} روی سرور پیدا نشد!")

    user_id = message.from_user.id
    db_id = add_to_db(user_id, message.chat.id, message.id)
    session = get_session(user_id)
    session['queue'].append({'db_id': db_id, 'message': message, 'status': 'waiting'})
    
    if not session['dashboard_msg']: session['dashboard_msg'] = await message.reply_text("📊 داشبورد فعال شد...")
    else: await update_dashboard(user_id)

    if not session['is_processing']: asyncio.create_task(process_user_queue(client, user_id))

@app.on_callback_query(filters.regex("^cancel_all$"))
async def cancel_callback(client, callback_query: CallbackQuery):
    session = get_session(callback_query.from_user.id)
    if session['is_processing']:
        session['cancel_flag'] = True
        await callback_query.answer("🛑 در حال لغو...", show_alert=True)

async def main():
    init_db()
    await app.start()
    logging.info("🚀 Fast Thumb Bot Started!")
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
