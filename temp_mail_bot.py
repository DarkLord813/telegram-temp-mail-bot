import os
import sys
import requests
import json
import time
import re
import sqlite3
import random
import string
import threading
import base64
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import TelegramError
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------- ENVIRONMENT VARIABLES --------------------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHANNEL_USERNAME = os.environ.get('CHANNEL_USERNAME', '@NCK_Dev')
CHANNEL_LINK = os.environ.get('CHANNEL_LINK', 'https://t.me/NCK_Dev')
API_BASE = os.environ.get('API_BASE', 'https://api.mail.tm')
DB_PATH = os.environ.get('DB_PATH', 'temp_mail.db')

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN environment variable is not set!")
    sys.exit(1)

logger.info("✅ Environment variables loaded")
logger.info(f"📢 Channel: {CHANNEL_USERNAME}")
logger.info(f"📊 Database: {DB_PATH}")


# -------------------- HEALTH SERVER (Star Cards style) --------------------
def start_health_server():
    """Start a minimal HTTP server for Render health checks."""
    port = int(os.getenv("PORT", "8080"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    def serve():
        try:
            HTTPServer(("0.0.0.0", port), H).serve_forever()
        except Exception as e:
            logger.warning(f"health server failed: {e}")

    threading.Thread(target=serve, daemon=True, name="health").start()
    logger.info(f"🏥 health server on :{port}")


# -------------------- KEEP-ALIVE LOOP (Star Cards style) --------------------
def start_keepalive_loop(interval=600):
    """Ping the Render external URL every `interval` seconds."""
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("KEEPALIVE_URL", "")
    if not url:
        logger.info("ℹ️ keepalive disabled (no RENDER_EXTERNAL_URL)")
        return

    ping_url = url.rstrip("/") + "/"

    def loop():
        while True:
            time.sleep(interval)
            try:
                r = requests.get(ping_url, timeout=10)
                logger.info(f"💓 keepalive ping {ping_url} → {r.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ keepalive ping failed: {e}")

    threading.Thread(target=loop, daemon=True, name="keepalive").start()
    logger.info(f"💓 keepalive loop started ({interval}s → {ping_url})")


# -------------------- DATABASE CLASS --------------------
class TempMailDB:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                account_id TEXT NOT NULL,
                token TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id TEXT NOT NULL,
                from_address TEXT,
                subject TEXT,
                body TEXT,
                html_body TEXT,
                received_at TIMESTAMP,
                is_read INTEGER DEFAULT 0,
                has_attachments INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                UNIQUE(user_id, message_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id TEXT,
                attachment_id TEXT,
                filename TEXT,
                mime_type TEXT,
                size INTEGER,
                content BLOB,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')

        conn.commit()
        conn.close()
        logger.info("✅ Database initialized")

    def create_user(self, user_id, email, password, account_id, token):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO users 
                (user_id, email, password, account_id, token, created_at, last_checked)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ''', (user_id, email, password, account_id, token))
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"DB error: {e}")
            return False
        finally:
            conn.close()

    def get_user(self, user_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, email, password, account_id, token, created_at, last_checked
            FROM users
            WHERE user_id = ? AND is_active = 1
        ''', (user_id,))
        result = cursor.fetchone()
        conn.close()
        if result:
            return {
                'user_id': result[0],
                'email': result[1],
                'password': result[2],
                'account_id': result[3],
                'token': result[4],
                'created_at': result[5],
                'last_checked': result[6]
            }
        return None

    def delete_user(self, user_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE users SET is_active = 0 WHERE user_id = ?', (user_id,))
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"DB error: {e}")
            return False
        finally:
            conn.close()

    def update_last_checked(self, user_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE users SET last_checked = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"DB error: {e}")
        finally:
            conn.close()

    def save_messages(self, user_id, messages):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            for msg in messages:
                cursor.execute('''
                    INSERT OR IGNORE INTO messages 
                    (user_id, message_id, from_address, subject, received_at, has_attachments)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    user_id,
                    msg.get('id'),
                    msg.get('from', {}).get('address', 'Unknown'),
                    msg.get('subject', 'No Subject'),
                    msg.get('createdAt', datetime.now().isoformat()),
                    1 if msg.get('attachments', []) else 0
                ))
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"DB error: {e}")
        finally:
            conn.close()

    def get_messages(self, user_id, limit=20):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT message_id, from_address, subject, received_at, is_read, has_attachments
            FROM messages
            WHERE user_id = ?
            ORDER BY received_at DESC
            LIMIT ?
        ''', (user_id, limit))
        results = cursor.fetchall()
        conn.close()
        return [{
            'id': row[0],
            'from': row[1],
            'subject': row[2],
            'received_at': row[3],
            'is_read': row[4],
            'has_attachments': row[5]
        } for row in results]

    def get_message_by_id(self, user_id, message_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT message_id, from_address, subject, body, html_body, received_at, has_attachments
            FROM messages
            WHERE user_id = ? AND message_id = ?
        ''', (user_id, message_id))
        result = cursor.fetchone()
        conn.close()
        if result:
            self.mark_message_read(user_id, message_id)
            return {
                'id': result[0],
                'from': result[1],
                'subject': result[2],
                'body': result[3],
                'html_body': result[4],
                'received_at': result[5],
                'has_attachments': result[6]
            }
        return None

    def mark_message_read(self, user_id, message_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE messages SET is_read = 1
                WHERE user_id = ? AND message_id = ?
            ''', (user_id, message_id))
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"DB error: {e}")
        finally:
            conn.close()

    def update_message_body(self, user_id, message_id, body, html_body=None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE messages 
                SET body = ?, html_body = ?
                WHERE user_id = ? AND message_id = ?
            ''', (body, html_body, user_id, message_id))
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"DB error: {e}")
        finally:
            conn.close()

    def delete_all_messages(self, user_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM messages WHERE user_id = ?', (user_id,))
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"DB error: {e}")
            return False
        finally:
            conn.close()

    def get_unread_count(self, user_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM messages
            WHERE user_id = ? AND is_read = 0
        ''', (user_id,))
        count = cursor.fetchone()[0]
        conn.close()
        return count


db = TempMailDB()


# -------------------- MAIL.TM API FUNCTIONS --------------------
class MailTM:
    def __init__(self):
        self.base_url = API_BASE
        self.session = requests.Session()

    def create_account(self):
        domains = self.get_domains()
        if not domains:
            return None

        domain = domains[0]['domain']
        username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        email = f"{username}@{domain}"
        password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))

        response = self.session.post(
            f"{self.base_url}/accounts",
            json={"address": email, "password": password}
        )

        if response.status_code == 201:
            data = response.json()
            account_id = data['id']
            token_response = self.session.post(
                f"{self.base_url}/token",
                json={"address": email, "password": password}
            )
            if token_response.status_code == 200:
                token_data = token_response.json()
                return {
                    'email': email,
                    'password': password,
                    'account_id': account_id,
                    'token': token_data['token'],
                    'id': token_data['id']
                }
        return None

    def get_domains(self):
        response = self.session.get(f"{self.base_url}/domains")
        if response.status_code == 200:
            data = response.json()
            return data['hydra:member']
        return []

    def get_messages(self, token):
        headers = {'Authorization': f'Bearer {token}'}
        response = self.session.get(f"{self.base_url}/messages", headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data['hydra:member']
        return []

    def get_message(self, token, message_id):
        headers = {'Authorization': f'Bearer {token}'}
        response = self.session.get(f"{self.base_url}/messages/{message_id}", headers=headers)
        if response.status_code == 200:
            return response.json()
        return None

    def delete_message(self, token, message_id):
        headers = {'Authorization': f'Bearer {token}'}
        response = self.session.delete(
            f"{self.base_url}/messages/{message_id}",
            headers=headers
        )
        return response.status_code == 204

    def delete_account(self, token, account_id):
        headers = {'Authorization': f'Bearer {token}'}
        response = self.session.delete(
            f"{self.base_url}/accounts/{account_id}",
            headers=headers
        )
        return response.status_code == 204


mail_api = MailTM()


# -------------------- FORCE JOIN HELPERS --------------------
async def is_user_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    try:
        chat = await context.bot.get_chat(CHANNEL_USERNAME)
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception as e:
        logger.error(f"Membership check error: {e}")
        return False


async def send_force_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>🔒 🔐 CHANNEL ACCESS REQUIRED 🔐 🔒</b>\n\n"
        "<b>🌟 You need to join our official channel to use this bot!</b> 🌟\n\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"📢 <b>Channel:</b> <a href='{CHANNEL_LINK}'>{CHANNEL_USERNAME}</a>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        "<b>✨ Benefits of joining:</b>\n"
        "• 📧 Get temporary email addresses\n"
        "• 📬 Receive and read emails\n"
        "• 🔄 Manage your inbox\n"
        "• 🚀 24/7 available\n\n"
        "<b>👇 Click the button below to join and continue!</b>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 JOIN CHANNEL 📢", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ I'VE JOINED ✅", callback_data="check_join")]
    ])

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if await is_user_member(update, context):
        await show_main_menu(update, context)
    else:
        text = (
            "<b>❌ ❌ NOT A MEMBER YET</b> ❌ ❌\n\n"
            "You are still not a member of the channel.\n\n"
            "1️⃣ Click the <b>JOIN CHANNEL</b> button below\n"
            "2️⃣ Join the channel\n"
            "3️⃣ Click <b>I'VE JOINED</b> again\n\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"📢 <b>Channel:</b> <a href='{CHANNEL_LINK}'>{CHANNEL_USERNAME}</a>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━━</b>"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 JOIN CHANNEL 📢", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ I'VE JOINED ✅", callback_data="check_join")]
        ])

        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


# -------------------- UI HELPERS --------------------
def get_colorful_main_menu(user_exists=False, unread=0):
    keyboard = []

    if user_exists:
        inbox_text = f"📬 CHECK INBOX"
        if unread > 0:
            inbox_text += f" 🔴 +{unread}"
        keyboard.append([InlineKeyboardButton(inbox_text, callback_data="inbox")])
        keyboard.append([
            InlineKeyboardButton("🔄 REFRESH 🔄", callback_data="refresh"),
            InlineKeyboardButton("📝 NEW EMAIL ✨", callback_data="new_email")
        ])
        keyboard.append([InlineKeyboardButton("🗑️ DELETE ACCOUNT ❌", callback_data="delete_account")])
    else:
        keyboard.append([InlineKeyboardButton("📧 CREATE TEMP EMAIL 🚀", callback_data="create_email")])

    keyboard.append([InlineKeyboardButton("ℹ️ HELP 📖", callback_data="help")])
    return InlineKeyboardMarkup(keyboard)


def get_colorful_back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 BACK TO MENU 🏠", callback_data="menu")]
    ])


def get_colorful_inbox_buttons(messages, page=0, total_pages=1):
    keyboard = []

    for i, msg in enumerate(messages[:10], 1):
        if msg['is_read']:
            status = "📖 READ"
            color = "🔵"
        else:
            status = "🆕 NEW"
            color = "🔴"

        attachment = " 📎" if msg['has_attachments'] else ""
        subject = msg['subject'][:20] + "..." if len(msg['subject']) > 20 else msg['subject']

        keyboard.append([
            InlineKeyboardButton(
                f"{color} {i}. {status} {subject}{attachment}",
                callback_data=f"read_{msg['id']}"
            )
        ])

    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ PREV", callback_data=f"inbox_page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("NEXT ▶️", callback_data=f"inbox_page_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)

    keyboard.append([
        InlineKeyboardButton("🔄 REFRESH", callback_data="refresh"),
        InlineKeyboardButton("🗑️ DELETE ALL", callback_data="delete_all_messages")
    ])
    keyboard.append([InlineKeyboardButton("🔙 BACK TO MENU 🏠", callback_data="menu")])

    return InlineKeyboardMarkup(keyboard)


def get_confirmation_buttons(action="delete"):
    if action == "delete":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ YES, DELETE ✅", callback_data="confirm_delete"),
             InlineKeyboardButton("❌ CANCEL ❌", callback_data="menu")]
        ])
    elif action == "new_email":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ YES, CREATE NEW ✅", callback_data="create_email"),
             InlineKeyboardButton("❌ CANCEL ❌", callback_data="menu")]
        ])


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_user(user_id)

    if user:
        unread = db.get_unread_count(user_id)
        text = (
            f"<b>📧 🌟 YOUR TEMP EMAIL</b> 🌟 📧\n\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"📌 <b>Email:</b> <code>{user['email']}</code>\n"
            f"⏰ <b>Created:</b> {user['created_at']}\n"
            f"📬 <b>Unread:</b> {unread}\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"👇 <b>Choose an option below:</b>"
        )
    else:
        text = (
            f"<b>📧 🌟 TEMP MAIL BOT</b> 🌟 📧\n\n"
            f"Create a temporary email address to receive messages anonymously.\n\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"🔹 No registration required\n"
            f"🔹 Valid for 24 hours\n"
            f"🔹 Delete anytime\n"
            f"🔹 100% Free\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"👇 <b>Click the button below to get started:</b>"
        )

    reply_markup = get_colorful_main_menu(
        user_exists=bool(user),
        unread=db.get_unread_count(user_id) if user else 0
    )

    if update.callback_query:
        query = update.callback_query
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


# -------------------- BOT HANDLERS --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return
    await show_main_menu(update, context)


async def create_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if db.get_user(user_id):
        user = db.get_user(user_id)
        mail_api.delete_account(user['token'], user['account_id'])
        db.delete_user(user_id)

    await query.edit_message_text(
        "<b>⏳ ⏳ CREATING YOUR EMAIL...</b> ⏳ ⏳\n\n"
        "Please wait while we generate your temporary email address.",
        parse_mode="HTML"
    )

    account = mail_api.create_account()

    if not account:
        await query.edit_message_text(
            "<b>❌ ❌ FAILED TO CREATE EMAIL</b> ❌ ❌\n\n"
            "Something went wrong. Please try again later.",
            reply_markup=get_colorful_back_menu(),
            parse_mode="HTML"
        )
        return

    db.create_user(
        user_id,
        account['email'],
        account['password'],
        account['account_id'],
        account['token']
    )

    text = (
        f"<b>✅ ✅ TEMPORARY EMAIL CREATED!</b> ✅ ✅\n\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"📌 <b>Email:</b> <code>{account['email']}</code>\n"
        f"🔑 <b>Password:</b> <code>{account['password']}</code>\n"
        f"⏰ <b>Valid for:</b> 24 hours\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        f"<b>⚠️ ⚠️ SAVE YOUR PASSWORD!</b> ⚠️ ⚠️\n"
        f"You'll need it to access your email.\n\n"
        f"📋 Copy this email and use it anywhere.\n"
        f"👇 <b>Check your inbox using the buttons below:</b>"
    )

    reply_markup = get_colorful_main_menu(user_exists=True, unread=0)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def check_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = db.get_user(user_id)

    if not user:
        await query.edit_message_text(
            "<b>❌ ❌ NO ACTIVE SESSION</b> ❌ ❌\n\n"
            "Please create a temporary email first.",
            reply_markup=get_colorful_back_menu(),
            parse_mode="HTML"
        )
        return

    await query.edit_message_text(
        "<b>⏳ ⏳ FETCHING MESSAGES...</b> ⏳ ⏳\n\n"
        "Please wait while we retrieve your emails.",
        parse_mode="HTML"
    )

    messages = mail_api.get_messages(user['token'])

    if messages:
        db.save_messages(user_id, messages)

        for msg in messages[:10]:
            existing = db.get_message_by_id(user_id, msg['id'])
            if not existing or not existing.get('body'):
                full_msg = mail_api.get_message(user['token'], msg['id'])
                if full_msg:
                    text_content = full_msg.get('text', '')
                    html_content = full_msg.get('html', [''])[0] if full_msg.get('html') else None

                    if not text_content and html_content:
                        text_content = re.sub(r'<[^>]+>', ' ', html_content)
                        text_content = ' '.join(text_content.split())

                    db.update_message_body(
                        user_id,
                        msg['id'],
                        text_content or 'No text content available',
                        html_content
                    )

    db.update_last_checked(user_id)
    cached_messages = db.get_messages(user_id)

    if not cached_messages:
        text = (
            f"<b>📭 📭 INBOX EMPTY</b> 📭 📭\n\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"📌 <b>Email:</b> <code>{user['email']}</code>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"No messages yet. Wait for someone to send you an email!"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 REFRESH 🔄", callback_data="refresh")],
            [InlineKeyboardButton("🔙 BACK TO MENU 🏠", callback_data="menu")]
        ])

        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    total_pages = (len(cached_messages) + 9) // 10
    page = context.user_data.get('inbox_page', 0)

    start_idx = page * 10
    end_idx = min(start_idx + 10, len(cached_messages))
    page_messages = cached_messages[start_idx:end_idx]

    text = (
        f"<b>📬 📬 INBOX ({len(cached_messages)} MESSAGES)</b> 📬 📬\n\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"📌 <b>Email:</b> <code>{user['email']}</code>\n"
        f"📄 <b>Page {page + 1}/{total_pages}</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
    )

    for i, msg in enumerate(page_messages, start_idx + 1):
        status = "📖 READ" if msg['is_read'] else "🆕 NEW"
        attachment = " 📎" if msg['has_attachments'] else ""
        text += f"{i}. {status} <b>From:</b> {msg['from']}\n"
        text += f"   <b>Subject:</b> {msg['subject']}{attachment}\n"
        text += f"   <b>Date:</b> {msg['received_at'][:10]}\n\n"

    if len(cached_messages) > 10:
        text += f"\n... and {len(cached_messages) - 10} more messages\n"

    reply_markup = get_colorful_inbox_buttons(page_messages, page=page, total_pages=total_pages)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def inbox_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = int(query.data.replace("inbox_page_", ""))
    context.user_data['inbox_page'] = page
    await check_inbox(update, context)


async def read_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    message_id = query.data.replace("read_", "")

    user = db.get_user(user_id)
    if not user:
        await query.edit_message_text(
            "<b>❌ Session expired.</b> Please create a new email.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📧 CREATE EMAIL 🚀", callback_data="create_email")]
            ]),
            parse_mode="HTML"
        )
        return

    await query.edit_message_text(
        "<b>⏳ FETCHING MESSAGE CONTENT...</b>",
        parse_mode="HTML"
    )

    msg = db.get_message_by_id(user_id, message_id)

    if not msg or not msg.get('body') or msg.get('body') == 'No text content available':
        full_msg = mail_api.get_message(user['token'], message_id)
        if full_msg:
            text_content = full_msg.get('text', '')
            html_content = full_msg.get('html', [''])[0] if full_msg.get('html') else None

            if not text_content and html_content:
                text_content = re.sub(r'<[^>]+>', ' ', html_content)
                text_content = ' '.join(text_content.split())

            db.update_message_body(
                user_id,
                message_id,
                text_content or 'No text content available',
                html_content
            )
            msg = db.get_message_by_id(user_id, message_id)

    if not msg:
        await query.edit_message_text(
            "<b>❌ Message not found.</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 REFRESH", callback_data="refresh")],
                [InlineKeyboardButton("🔙 BACK TO INBOX", callback_data="inbox")]
            ]),
            parse_mode="HTML"
        )
        return

    text = (
        f"<b>📧 📧 MESSAGE DETAILS</b> 📧 📧\n\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"📌 <b>From:</b> {msg['from']}\n"
        f"📝 <b>Subject:</b> {msg['subject']}\n"
        f"📅 <b>Date:</b> {msg['received_at']}\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
    )

    if msg.get('body') and msg['body'] != 'No text content available':
        body = msg['body'][:3000]
        body = body.replace('\r\n', '\n').replace('\r', '\n')
        body = '\n'.join(line.strip() for line in body.split('\n') if line.strip())

        if body:
            text += f"<b>📝 Message:</b>\n<pre>{body}</pre>"
            if len(msg['body']) > 3000:
                text += "\n\n... (message truncated)"
        else:
            text += "<b>📝 Message:</b> (Empty message)"
    else:
        text += "<b>📝 Message:</b> (No text content available)"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 REFRESH", callback_data="refresh")],
        [InlineKeyboardButton("🗑️ DELETE", callback_data=f"delete_msg_{message_id}")],
        [InlineKeyboardButton("🔙 BACK TO INBOX", callback_data="inbox")],
        [InlineKeyboardButton("🔙 BACK TO MENU 🏠", callback_data="menu")]
    ])

    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    message_id = query.data.replace("delete_msg_", "")

    user = db.get_user(user_id)
    if user:
        mail_api.delete_message(user['token'], message_id)
        db.delete_all_messages(user_id)

    await query.edit_message_text(
        "<b>✅ ✅ MESSAGE DELETED!</b> ✅ ✅\n\n"
        "The message has been removed successfully.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📬 CHECK INBOX", callback_data="inbox")],
            [InlineKeyboardButton("🔙 BACK TO MENU 🏠", callback_data="menu")]
        ]),
        parse_mode="HTML"
    )


async def delete_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    db.delete_all_messages(user_id)

    await query.edit_message_text(
        "<b>✅ ✅ ALL MESSAGES DELETED!</b> ✅ ✅\n\n"
        "Your inbox has been cleared.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📬 CHECK INBOX", callback_data="inbox")],
            [InlineKeyboardButton("🔙 BACK TO MENU 🏠", callback_data="menu")]
        ]),
        parse_mode="HTML"
    )


async def refresh_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()
    await check_inbox(update, context)


async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    text = (
        "<b>⚠️ ⚠️ CREATE NEW EMAIL</b> ⚠️ ⚠️\n\n"
        "This will delete your current email and create a new one.\n"
        "All messages will be lost.\n\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        "❓ <b>Are you sure you want to continue?</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━</b>"
    )

    await query.edit_message_text(text, reply_markup=get_confirmation_buttons("new_email"), parse_mode="HTML")


async def delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    text = (
        "<b>⚠️ ⚠️ DELETE ACCOUNT</b> ⚠️ ⚠️\n\n"
        "This will permanently delete your temporary email.\n"
        "All messages will be lost.\n\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        "❓ <b>Are you sure you want to continue?</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━</b>"
    )

    await query.edit_message_text(text, reply_markup=get_confirmation_buttons("delete"), parse_mode="HTML")


async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = db.get_user(user_id)

    if user:
        mail_api.delete_account(user['token'], user['account_id'])
        db.delete_user(user_id)

    await query.edit_message_text(
        "<b>🗑️ 🗑️ ACCOUNT DELETED</b> 🗑️ 🗑️\n\n"
        "Your temporary email has been deleted.\n\n"
        "You can create a new one anytime with /start",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📧 CREATE NEW 🚀", callback_data="create_email")],
            [InlineKeyboardButton("🔙 BACK TO MENU 🏠", callback_data="menu")]
        ]),
        parse_mode="HTML"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()

    text = (
        "<b>📖 📖 HOW TO USE TEMP MAIL BOT</b> 📖 📖\n\n"
        "<b>1️⃣ Create Email</b>\n"
        "   Click 'CREATE TEMP EMAIL' to get a new address\n\n"
        "<b>2️⃣ Check Inbox</b>\n"
        "   Click 'CHECK INBOX' to see your messages\n\n"
        "<b>3️⃣ Read Messages</b>\n"
        "   Click 'READ' on any message to view it\n\n"
        "<b>4️⃣ Delete Messages</b>\n"
        "   Delete individual messages or all at once\n\n"
        "<b>5️⃣ New Email</b>\n"
        "   Create a new email address (old one is deleted)\n\n"
        "<b>6️⃣ Delete Account</b>\n"
        "   Permanently delete your email account\n\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        "⏰ <b>Email expires after 24 hours</b>\n"
        "🔒 <b>All data is stored securely in SQLite database</b>\n"
        f"📢 <b>Join our channel:</b> <a href='{CHANNEL_LINK}'>{CHANNEL_USERNAME}</a>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        "💡 <b>Tip:</b> Use this for temporary registrations!"
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 BACK TO MENU 🏠", callback_data="menu")]
        ]),
        parse_mode="HTML"
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    query = update.callback_query
    await query.answer()
    await show_main_menu(update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_member(update, context):
        await send_force_join_message(update, context)
        return

    await update.message.reply_text(
        "<b>Use the buttons below to interact with the bot.</b>\n\n"
        "Type /start to see the menu.",
        parse_mode="HTML"
    )


# -------------------- MAIN --------------------
def main():
    # Star Cards style: start health server + keepalive FIRST
    start_health_server()
    start_keepalive_loop(interval=600)

    try:
        application = Application.builder().token(BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
        application.add_handler(CallbackQueryHandler(create_email, pattern="^create_email$"))
        application.add_handler(CallbackQueryHandler(check_inbox, pattern="^inbox$"))
        application.add_handler(CallbackQueryHandler(refresh_inbox, pattern="^refresh$"))
        application.add_handler(CallbackQueryHandler(inbox_page, pattern="^inbox_page_"))
        application.add_handler(CallbackQueryHandler(read_message, pattern="^read_"))
        application.add_handler(CallbackQueryHandler(delete_message, pattern="^delete_msg_"))
        application.add_handler(CallbackQueryHandler(delete_all_messages, pattern="^delete_all_messages$"))
        application.add_handler(CallbackQueryHandler(new_email, pattern="^new_email$"))
        application.add_handler(CallbackQueryHandler(delete_account, pattern="^delete_account$"))
        application.add_handler(CallbackQueryHandler(confirm_delete, pattern="^confirm_delete$"))
        application.add_handler(CallbackQueryHandler(help_command, pattern="^help$"))
        application.add_handler(CallbackQueryHandler(menu, pattern="^menu$"))

        logger.info("🤖 Temp Mail Bot starting...")
        logger.info(f"📊 Database: {DB_PATH}")
        logger.info(f"📢 Force join: {CHANNEL_USERNAME}")
        logger.info("✅ Powered by mail.tm API")

        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()