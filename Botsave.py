import os
import re
import time
import socket
import string
import random
import threading
import base64
import urllib.parse
import io
from datetime import datetime
from threading import Thread

import telebot
from telebot import types
import psycopg2
import requests
from bs4 import BeautifulSoup
from flask import Flask

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '8176196456'))
DATABASE_URL = os.getenv('DATABASE_URL')
CHANNEL_ID = -1003668283208
CHANNEL_LINK = 'https://t.me/ciorsa'
SUPPORT = '@mel1ste'

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)

check_results = {}
search_cache = {}
decrypt_results = {}

KEY_TEMPLATE = """\
#profile-title: 🌐 Потужно VPN Free
#profile-update-interval: 1
#support-url: https://t.me/mel1ste
#announce: 📡 Сервера LTE использовать только при белых списках. Без торрентов. 🕐 Поддержка с 10 до 22, ответят в ближайшее время.
#channel: 📢 https://t.me/ciorsa
#subscription-userinfo: upload=0; download=0; total=10995116277760000; expire={expire}
{keys}"""

DEFAULT_KEYS = [
    'vless://00000000-0000-0000-0000-000000000001@1.1.1.1:443?type=tcp&security=tls#Demo-Key-1',
]

VPN_KEY_PATTERN = r'(?:vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic|naive\+https?|wg|wireguard|juicity|brook|shadowtls)://[^\s\r\n<>"\'`]+'

# ==================== DATABASE ====================

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            subscription_end BIGINT,
            notified_3days INTEGER DEFAULT 0,
            last_activity BIGINT DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            token TEXT UNIQUE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            added_by BIGINT,
            added_at BIGINT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id BIGINT,
            referred_id BIGINT,
            reward_date BIGINT,
            rewarded INTEGER DEFAULT 0,
            PRIMARY KEY (referrer_id, referred_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_setting(key, default='0'):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result[0] if result else default
    except:
        return default

def set_setting(key, value):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = %s",
        (key, value, value)
    )
    conn.commit()
    cur.close()
    conn.close()

def get_keys_from_db():
    val = get_setting('vless_keys', '')
    if not val:
        return []
    return [k for k in val.split('|||') if k]

def save_keys_to_db(keys):
    set_setting('vless_keys', '|||'.join(keys))

def generate_subscription_token():
    chars = string.ascii_letters + string.digits
    while True:
        token = ''.join(random.choices(chars, k=12))
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE token = %s", (token,))
        exists = cur.fetchone()
        cur.close()
        conn.close()
        if not exists:
            return token

# ==================== KEY PARSING UTILS ====================

def _dedup(lst):
    seen = set()
    out = []
    for x in lst:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _extract_vpn_keys(text):
    keys = re.findall(VPN_KEY_PATTERN, text, re.IGNORECASE)
    return [k.strip().rstrip('.,;"\')>') for k in keys]

def _try_b64(data):
    data = data.strip().replace('\n', '').replace('\r', '').replace(' ', '')
    if len(data) < 16:
        return None
    for variant in [data, data.replace('-', '+').replace('_', '/')]:
        for pad in ['', '=', '==']:
            try:
                decoded = base64.b64decode(variant + pad).decode('utf-8', errors='ignore')
                if len(decoded) > 20:
                    return decoded
            except:
                pass
    return None

def _resolve_url(raw_url):
    raw_url = raw_url.strip()
    # Схемы приложений: incy://, happ://, v2rayng:// и т.д.
    app_scheme = re.match(
        r'^(?:incy|happ|v2ray|v2rayng|v2box|clash|sing-box|quantumult|surge|loon|shadowrocket|stash|nekoray|hiddify|streisand|karing|mihomo|flclash)://(?:add|sub|crypt|import|install|update)/(.+)',
        raw_url, re.IGNORECASE
    )
    if app_scheme:
        payload = urllib.parse.unquote(app_scheme.group(1).strip())
        if re.match(r'https?://', payload, re.IGNORECASE):
            return payload
        return payload  # Base64 или прямые ключи

    # Формат прокси-обёртки: https://proxy.domain/https://real.url
    proxy_wrap = re.match(r'https?://[^/]+/(https?://.+)', raw_url, re.IGNORECASE)
    if proxy_wrap:
        return proxy_wrap.group(1)

    return raw_url

def _parse_keys_from_content(content):
    all_keys = []
    # 1. Прямые ключи
    all_keys.extend(_extract_vpn_keys(content))
    # 2. Весь контент как Base64
    decoded = _try_b64(content.strip())
    if decoded:
        all_keys.extend(_extract_vpn_keys(decoded))
    # 3. Построчный Base64
    for line in content.splitlines():
        line = line.strip()
        if len(line) < 20:
            continue
        d = _try_b64(line)
        if d:
            all_keys.extend(_extract_vpn_keys(d))
    # 4. HTML парсинг
    if '<' in content:
        try:
            soup = BeautifulSoup(content, 'html5lib')
            for elem in soup.find_all(string=True):
                all_keys.extend(_extract_vpn_keys(str(elem)))
        except:
            pass
    # 5. JSON поиск
    try:
        json_matches = re.findall(
            r'"([^"]*(?:vless|vmess|trojan|ss|ssr|hysteria)[^"]*)"', content, re.IGNORECASE
        )
        for m in json_matches:
            all_keys.extend(_extract_vpn_keys(m))
    except:
        pass
    return _dedup(all_keys)

def load_keys_from_url(raw_url):
    url = _resolve_url(raw_url)
    # Если после resolve не HTTP URL — Base64 или прямые ключи
    if not re.match(r'https?://', url, re.IGNORECASE):
        decoded = _try_b64(url)
        if decoded:
            keys = _extract_vpn_keys(decoded)
            if keys:
                return _dedup(keys)
        return _dedup(_extract_vpn_keys(url))

    headers = {'User-Agent': 'v2rayNG/1.8.7', 'Accept': '*/*'}
    content = None
    for verify in [True, False]:
        try:
            resp = requests.get(url, headers=headers, timeout=30, verify=verify)
            content = resp.text
            break
        except:
            continue
    if not content:
        return []
    return _parse_keys_from_content(content)

def load_keys_from_text(text):
    return _parse_keys_from_content(text)

def _finish_update_keys(message, keys, source_label):
    if not keys:
        bot.reply_to(
            message,
            "❌ Не найдено ни одного VPN ключа.\n\n"
            "Поддерживается:\n"
            "• vless:// vmess:// trojan:// ss:// и др.\n"
            "• Base64 подписка\n"
            "• URL подписки (включая happ.dska.su и подобные)\n"
            "• HTML страница с ключами"
        )
        return
    save_keys_to_db(keys)
    proto_stats = {}
    for k in keys:
        m = re.match(r'([a-z0-9+]+)://', k, re.IGNORECASE)
        if m:
            p = m.group(1).lower()
            proto_stats[p] = proto_stats.get(p, 0) + 1
    stats = '\n'.join(f"  • {p}:// — {c}" for p, c in sorted(proto_stats.items(), key=lambda x: -x[1]))
    bot.reply_to(
        message,
        f"✅ Ключи обновлены!\n\n"
        f"📊 Загружено ключей: {len(keys)}\n"
        f"📋 По протоколам:\n{stats}\n"
        f"🔗 Источник: {source_label}"
    )

# ==================== CHECKS ====================

def is_admin(user_id):
    if user_id == ADMIN_ID:
        return True
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM admins WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result is not None
    except:
        return False

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def get_subscription_link(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT token FROM users WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    if result and result[0]:
        token = result[0]
        cur.close()
        conn.close()
        return f"https://potyjnovpnbot.onrender.com/sub/{token}"
    token = generate_subscription_token()
    cur.execute("UPDATE users SET token = %s WHERE user_id = %s", (token, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return f"https://potyjnovpnbot.onrender.com/sub/{token}"

def is_blocked(user_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT is_blocked FROM users WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result[0] == 1 if result else False
    except:
        return False

def can_add_referral(referrer_id):
    today_start = int(time.time()) - 24 * 60 * 60
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id = %s AND reward_date > %s",
        (referrer_id, today_start)
    )
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count < 10

def get_user_display_name(user_id):
    try:
        chat = bot.get_chat(user_id)
        name = chat.first_name or ''
        if chat.last_name:
            name += ' ' + chat.last_name
        return name.strip() or str(user_id)
    except:
        return str(user_id)

# ==================== KEYBOARDS ====================

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(
        types.KeyboardButton("👤 Личный кабинет"),
        types.KeyboardButton("📡 Моя подписка")
    )
    kb.add(
        types.KeyboardButton("👥 Рефералы"),
        types.KeyboardButton("🏆 Топ рефералов")
    )
    kb.add(
        types.KeyboardButton("🔍 Проверка ключей"),
        types.KeyboardButton("🔓 Расшифровать подписку")
    )
    kb.add(
        types.KeyboardButton("❓ Поддержка")
    )
    return kb

def subscribe_button():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 ПОДПИСАТЬСЯ", url=CHANNEL_LINK))
    kb.add(types.InlineKeyboardButton("✅ Я подписался", callback_data="check_sub"))
    return kb

def blocked_message():
    return f"🚫 Вы заблокированы администратором. Обратитесь в поддержку: {SUPPORT}"

# ==================== /start ====================

@bot.message_handler(commands=['start'])
def cmd_start(message):
    if message.chat.type != 'private':
        bot.reply_to(message, "⚠️ Бот работает только в личных сообщениях.")
        return

    user_id = message.from_user.id
    current_time = int(time.time())

    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return

    referrer_id = None
    if message.text and 'start=ref_' in message.text:
        parts = message.text.split('start=ref_')
        if len(parts) > 1:
            try:
                referrer_id = int(parts[1].strip())
            except:
                referrer_id = None

    if referrer_id and referrer_id != user_id:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM referrals WHERE referrer_id = %s AND referred_id = %s",
            (referrer_id, user_id)
        )
        already_ref = cur.fetchone()
        cur.close()
        conn.close()

        if not already_ref:
            if can_add_referral(referrer_id):
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO referrals (referrer_id, referred_id, reward_date, rewarded) VALUES (%s, %s, %s, 0)",
                    (referrer_id, user_id, current_time)
                )
                conn.commit()
                cur.close()
                conn.close()
                name = message.from_user.first_name or str(user_id)
                try:
                    bot.send_message(referrer_id, f"🔔 Новый реферал! Пользователь {name} присоединился по вашей реферальной ссылке.")
                except:
                    pass
                if get_setting('referral_enabled') == '1' and is_subscribed(referrer_id):
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (referrer_id,))
                    ref_result = cur.fetchone()
                    if ref_result:
                        new_end = ref_result[0] + 3 * 24 * 60 * 60
                        cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (new_end, referrer_id))
                        cur.execute(
                            "UPDATE referrals SET rewarded = 1 WHERE referrer_id = %s AND referred_id = %s",
                            (referrer_id, user_id)
                        )
                        conn.commit()
                        try:
                            bot.send_message(referrer_id, "🎉 Вам начислено +3 дня!")
                        except:
                            pass
                    cur.close()
                    conn.close()
            else:
                try:
                    bot.send_message(referrer_id, "⚠️ Вы достигли лимита рефералов (10 в день). Попробуйте завтра.")
                except:
                    pass

    if not is_subscribed(user_id):
        bot.reply_to(message, "⚠️ Подпишитесь на канал, чтобы пользоваться ботом.", reply_markup=subscribe_button())
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, last_activity FROM users WHERE user_id = %s", (user_id,))
    user = cur.fetchone()

    if not user:
        token = generate_subscription_token()
        sub_end = current_time + 7 * 24 * 60 * 60
        cur.execute(
            "INSERT INTO users (user_id, subscription_end, last_activity, is_blocked, token) VALUES (%s, %s, %s, 0, %s)",
            (user_id, sub_end, current_time, token)
        )
        conn.commit()
        cur.close()
        conn.close()
        bot.reply_to(message, "🎉 Добро пожаловать! Вам выдана подписка на 7 дней.")
    else:
        last_activity = user[1] or 0
        days_since_last = (current_time - last_activity) // (24 * 60 * 60)
        welcome_text = "👋 С возвращением!" if days_since_last >= 3 else "👋 Добро пожаловать!"
        cur.execute("UPDATE users SET last_activity = %s WHERE user_id = %s", (current_time, user_id))
        conn.commit()
        cur.close()
        conn.close()
        bot.reply_to(message, welcome_text)

    bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())

# ==================== ЛИЧНЫЙ КАБИНЕТ ====================

@bot.message_handler(func=lambda m: m.text == "👤 Личный кабинет")
def cabinet(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    current_time = int(time.time())
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT subscription_end, token FROM users WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    if not result:
        bot.reply_to(message, "❌ Вы не зарегистрированы. Используйте /start")
        return
    subscription_end = result[0]
    if subscription_end and subscription_end > current_time:
        status = "✅ Активна"
        days_left = (subscription_end - current_time) // (24 * 60 * 60)
        hours_left = ((subscription_end - current_time) // 3600) % 24
        time_left = f"{days_left} дн {hours_left} ч"
        expire_date = datetime.fromtimestamp(subscription_end).strftime("%d.%m.%Y в %H:%M")
        link = get_subscription_link(user_id)
    else:
        status = "❌ Не активна"
        time_left = "Закончилась"
        expire_date = "Закончилась"
        link = "❌ Нет активной подписки"
    text = (
        f"👤 Личный кабинет\n\n"
        f"🆔 ID: {user_id}\n"
        f"📅 Подписка до: {expire_date}\n"
        f"⏳ Осталось: {time_left}\n"
        f"📊 Статус: {status}\n"
        f"🔗 Ссылка: {link}"
    )
    bot.reply_to(message, text)

# ==================== МОЯ ПОДПИСКА ====================

@bot.message_handler(func=lambda m: m.text == "📡 Моя подписка")
def my_subscription(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    current_time = int(time.time())
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    if not is_subscribed(user_id):
        bot.reply_to(message, "⚠️ Подпишитесь на канал, чтобы пользоваться ботом.", reply_markup=subscribe_button())
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    if not result:
        bot.reply_to(message, "❌ Вы не зарегистрированы. Используйте /start")
        return
    subscription_end = result[0]
    if subscription_end and subscription_end > current_time:
        link = get_subscription_link(user_id)
        bot.reply_to(
            message,
            f"🔗 Ваша ссылка для импорта в VPN-клиент:\n\n{link}\n\nСкопируйте её и вставьте в приложение (V2Ray, Hiddify, Nekobox и др.)"
        )
    else:
        bot.reply_to(
            message,
            f"❌ Ваша подписка неактивна или истекла.\n\nДля продления обратитесь к администратору:\n{SUPPORT}"
        )

# ==================== РЕФЕРАЛЫ ====================

@bot.message_handler(func=lambda m: m.text == "👥 Рефералы")
def referrals(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        bot.reply_to(message, "❌ Вы не зарегистрированы. Используйте /start")
        return
    cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = %s", (user_id,))
    total = cur.fetchone()[0]
    today_start = int(time.time()) - 24 * 60 * 60
    cur.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id = %s AND reward_date > %s",
        (user_id, today_start)
    )
    today = cur.fetchone()[0]
    cur.close()
    conn.close()
    bot_username = bot.get_me().username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    text = (
        f"👥 *Ваши рефералы*\n\n"
        f"📊 Всего: {total}\n"
        f"📅 Сегодня: {today} / 10\n\n"
        f"🔗 *Ваша реферальная ссылка:*\n"
        f"`{ref_link}`\n\n"
        f"📌 *Как это работает:*\n"
        f"• Приглашенные друзья засчитываются вам на баланс\n"
        f"• За каждого друга вы получаете +3 дня подписки\n"
        f"• Лимит: 10 рефералов в день\n\n"
        f"💬 *Вопросы?* Пишите в поддержку: {SUPPORT}"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

# ==================== ТОП РЕФЕРАЛОВ ====================

@bot.message_handler(func=lambda m: m.text == "🏆 Топ рефералов")
def top_referrals(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT referrer_id, COUNT(*) as count FROM referrals GROUP BY referrer_id ORDER BY count DESC LIMIT 10"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        bot.reply_to(message, "📭 Пока нет рефералов.")
        return
    text = "🏆 *Топ рефералов:*\n\n"
    medals = ['🥇', '🥈', '🥉']
    for i, (ref_id, count) in enumerate(rows):
        name = get_user_display_name(ref_id)
        icon = medals[i] if i < 3 else f"{i+1}."
        text += f"{icon} {name} — {count} реф.\n"
    bot.reply_to(message, text, parse_mode="Markdown")

# ==================== ПРОВЕРКА КЛЮЧЕЙ ====================

@bot.message_handler(func=lambda m: m.text == "🔍 Проверка ключей")
def check_keys_start(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    check_results[user_id] = {'waiting': True}
    bot.reply_to(
        message,
        "📡 Отправьте файл или текст с ключами (в формате vless://...)\n\n"
        "Я проверю их на доступность (пинг).\n"
        "Ключи должны быть в формате:\nvless://...\nvless://...\n\n"
        "⏳ Проверка может занять до 30 секунд."
    )

def ping_key(key):
    match = re.search(r'@([\d\.]+):(\d+)', key)
    if not match:
        return False
    ip = match.group(1)
    port = int(match.group(2))
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except:
        return False

def check_keys_async(chat_id, keys, user_id, message_id):
    results = []
    working = 0
    not_working = 0
    for i, key in enumerate(keys):
        if i % 3 == 0:
            try:
                bot.edit_message_text(
                    f"🔍 Проверяю ключи...\n⏳ Прогресс: {i}/{len(keys)}",
                    chat_id, message_id
                )
            except:
                pass
        status = ping_key(key)
        results.append((key, status))
        if status:
            working += 1
        else:
            not_working += 1
    report = (
        f"📊 *Результаты проверки*\n\n"
        f"✅ Работает: {working}\n"
        f"❌ Не работает: {not_working}\n"
        f"📡 Всего проверено: {len(keys)}\n\n"
    )
    if not_working > 0:
        report += "*❌ Не работающие ключи:*\n"
        for key, status in results:
            if not status:
                short_key = key[:60] + '...' if len(key) > 60 else key
                report += f"└ `{short_key}`\n"
    else:
        report += "🎉 *Все ключи работают!*"
    try:
        bot.send_message(chat_id, report, parse_mode="Markdown")
    except:
        bot.send_message(chat_id, report)
    if user_id in check_results:
        del check_results[user_id]

# ==================== РАСШИФРОВКА ПОДПИСКИ ====================

@bot.message_handler(func=lambda m: m.text == "🔓 Расшифровать подписку")
def decrypt_subscription_start(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    decrypt_results[user_id] = {'waiting': True}
    bot.reply_to(
        message,
        "🔓 *Расшифровка VPN подписки*\n\n"
        "Отправьте ссылку, текст или файл подписки.\n\n"
        "Поддерживаю:\n"
        "• `https://example.com/sub` — URL подписки\n"
        "• `https://happ.dska.su/https://...` — прокси-обёртка\n"
        "• `incy://add/https://...` — схема Hiddify\n"
        "• `incy://add/BASE64==` — Hiddify с Base64\n"
        "• `happ://add/...` `shadowrocket://...` и др.\n"
        "• `vless://` `vmess://` `trojan://` `ss://` `ssr://`\n"
        "• `hysteria2://` `tuic://` `wg://` и др.\n"
        "• Чистый Base64 текст подписки\n"
        "• .txt файл с ключами\n\n"
        "📄 Получите `.txt` файл со всеми ключами.",
        parse_mode="Markdown"
    )

def _do_decrypt(message, user_id, text=None, file_bytes=None, file_name=None):
    if user_id in decrypt_results:
        del decrypt_results[user_id]

    wait_msg = bot.reply_to(message, "⏳ Обрабатываю подписку...")

    def process():
        try:
            if file_bytes is not None:
                raw = file_bytes.decode('utf-8', errors='ignore')
                keys, steps = _parse_subscription_any(raw, source_label=file_name or 'файл')
            else:
                keys, steps = _parse_subscription_any(text)
        except Exception as e:
            keys, steps = [], [f"❌ Ошибка: {e}"]

        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except:
            pass

        if not keys:
            info = '\n'.join(steps) if steps else '—'
            bot.reply_to(
                message,
                f"❌ *Не удалось найти VPN ключи*\n\n"
                f"🔍 Шаги:\n{info}\n\n"
                f"Убедитесь что ссылка рабочая и содержит VPN ключи.",
                parse_mode="Markdown"
            )
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        src = text[:80] if text else (file_name or 'файл')
        file_content = (
            f"# VPN подписка расшифрована\n"
            f"# Дата: {now}\n"
            f"# Ключей: {len(keys)}\n"
            f"# Источник: {src}\n"
            f"# {'='*48}\n\n"
        )
        file_content += '\n'.join(keys) + '\n'

        proto_stats = {}
        for k in keys:
            m = re.match(r'([a-z0-9+]+)://', k, re.IGNORECASE)
            if m:
                p = m.group(1).lower()
                proto_stats[p] = proto_stats.get(p, 0) + 1
        stats_text = '\n'.join(f"  • {p}:// — {c}" for p, c in sorted(proto_stats.items(), key=lambda x: -x[1]))
        steps_text = '\n'.join(steps) if steps else '—'

        caption = (
            f"✅ *Расшифровка завершена!*\n\n"
            f"📊 Найдено ключей: *{len(keys)}*\n\n"
            f"📋 По протоколам:\n{stats_text}\n\n"
            f"🔍 Шаги:\n{steps_text}"
        )

        buf = io.BytesIO(file_content.encode('utf-8'))
        buf.name = f"vpn_keys_{len(keys)}.txt"
        try:
            bot.send_document(
                message.chat.id, buf,
                caption=caption, parse_mode="Markdown",
                visible_file_name=f"vpn_keys_{len(keys)}.txt"
            )
        except:
            buf.seek(0)
            bot.send_document(
                message.chat.id, buf,
                caption=f"✅ Найдено ключей: {len(keys)}",
                visible_file_name=f"vpn_keys_{len(keys)}.txt"
            )

    t = threading.Thread(target=process)
    t.daemon = True
    t.start()

def _parse_subscription_any(raw, source_label=None):
    steps = []
    text = raw.strip()
    label = source_label or text[:60]

    # Шаг 1: Схема приложения или прокси-обёртка
    payload = None
    app_m = re.match(
        r'^(?:incy|happ|v2ray|v2rayng|v2box|clash|sing-box|quantumult|surge|loon|shadowrocket|stash|nekoray|hiddify|streisand|karing|mihomo|flclash)://(?:add|sub|crypt|import|install|update)/(.+)',
        text, re.IGNORECASE
    )
    if app_m:
        payload = urllib.parse.unquote(app_m.group(1).strip())
        steps.append(f"📱 Схема приложения → {payload[:50]}")
        text = payload

    proxy_m = re.match(r'https?://[^/]+/(https?://.+)', text, re.IGNORECASE)
    if proxy_m:
        real_url = proxy_m.group(1)
        steps.append(f"🔄 Прокси-обёртка → {real_url[:50]}")
        text = real_url

    # Шаг 2: URL — скачать
    if re.match(r'https?://', text, re.IGNORECASE):
        steps.append(f"🌐 Загружаю: {text[:60]}")
        headers = {'User-Agent': 'v2rayNG/1.8.7', 'Accept': '*/*'}
        content = None
        for verify in [True, False]:
            try:
                resp = requests.get(text, headers=headers, timeout=20, verify=verify)
                content = resp.text
                steps.append(f"✅ Получено {len(content)} байт")
                break
            except Exception as e:
                steps.append(f"⚠️ Попытка: {e}")
        if not content:
            return [], steps + ["❌ Не удалось загрузить URL"]
        text = content

    # Шаг 3: Парсинг ключей
    keys = _parse_keys_from_content(text)
    if keys:
        steps.append(f"🔑 Извлечено ключей: {len(keys)}")
    else:
        steps.append("❌ Ключи не найдены")

    return keys, steps

# ==================== ПОДДЕРЖКА ====================

@bot.message_handler(func=lambda m: m.text == "❓ Поддержка")
def support(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    bot.reply_to(message, f"📞 По всем вопросам пишите:\n{SUPPORT}")

# ==================== CHECK SUB CALLBACK ====================

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def callback_check_sub(call):
    if call.message.chat.type != 'private':
        bot.answer_callback_query(call.id, "⚠️ Работает только в личных сообщениях.")
        return
    user_id = call.from_user.id
    current_time = int(time.time())
    if is_blocked(user_id):
        bot.answer_callback_query(call.id, "🚫 Вы заблокированы.")
        return
    if is_subscribed(user_id):
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = %s AND rewarded = 0",
            (user_id,)
        )
        pending = cur.fetchone()
        if pending and get_setting('referral_enabled') == '1':
            referrer_id = pending[0]
            cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (referrer_id,))
            ref_result = cur.fetchone()
            if ref_result:
                new_end = ref_result[0] + 3 * 24 * 60 * 60
                cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (new_end, referrer_id))
                cur.execute("UPDATE referrals SET rewarded = 1 WHERE referred_id = %s", (user_id,))
                conn.commit()
                try:
                    bot.send_message(referrer_id, "🎉 Ваш реферал подтвердил подписку! Вам начислено +3 дня.")
                except:
                    pass
        cur.execute("SELECT user_id, token FROM users WHERE user_id = %s", (user_id,))
        user = cur.fetchone()
        if not user:
            token = generate_subscription_token()
            sub_end = current_time + 7 * 24 * 60 * 60
            cur.execute(
                "INSERT INTO users (user_id, subscription_end, last_activity, is_blocked, token) VALUES (%s, %s, %s, 0, %s)",
                (user_id, sub_end, current_time, token)
            )
            conn.commit()
            bot.send_message(user_id, "🎉 Добро пожаловать! Вам выдана подписка на 7 дней.")
        else:
            bot.send_message(user_id, "👋 Добро пожаловать!")
        cur.close()
        conn.close()
        bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())
    else:
        bot.answer_callback_query(call.id, "❌ Вы ещё не подписались на канал!")

# ==================== ADMIN COMMANDS ====================

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if not is_admin(message.from_user.id):
        return
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM users WHERE subscription_end > {current_time}")
    active = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM users WHERE subscription_end < {current_time}")
    expired = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 1")
    blocked_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM referrals")
    refs = cur.fetchone()[0]
    cur.close()
    conn.close()
    bot.reply_to(message,
        f"📊 Статистика:\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"✅ Активных: {active}\n"
        f"❌ Истекших: {expired}\n"
        f"🚫 Заблокированных: {blocked_count}\n"
        f"🔗 Всего рефералов: {refs}"
    )

@bot.message_handler(commands=['check'])
def cmd_check(message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Использование: /check [ID]")
        return
    try:
        target_id = int(args[1])
    except:
        bot.reply_to(message, "❌ Неверный ID.")
        return
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT subscription_end, is_blocked, token FROM users WHERE user_id = %s", (target_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    if not result:
        bot.reply_to(message, f"❌ Пользователь {target_id} не найден.")
        return
    subscription_end, blk, token = result
    if blk:
        status = "🚫 Заблокирован"
    elif subscription_end and subscription_end > current_time:
        days_left = (subscription_end - current_time) // (24 * 60 * 60)
        status = f"✅ Активна (осталось {days_left} дн)"
    else:
        status = "❌ Истекла"
    bot.reply_to(message, f"👤 Пользователь {target_id}\n📊 Статус: {status}\n🔗 Токен: {token}")

@bot.message_handler(commands=['prolong'])
def cmd_prolong(message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "❌ Использование: /prolong [ID] [дни]")
        return
    try:
        target_id = int(args[1])
        days = int(args[2])
    except:
        bot.reply_to(message, "❌ Неверные аргументы.")
        return
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (target_id,))
    result = cur.fetchone()
    if not result:
        cur.close()
        conn.close()
        bot.reply_to(message, f"❌ Пользователь {target_id} не найден.")
        return
    current_end = result[0] if (result[0] and result[0] > current_time) else current_time
    new_end = current_end + days * 24 * 60 * 60
    cur.execute("UPDATE users SET subscription_end = %s, notified_3days = 0 WHERE user_id = %s", (new_end, target_id))
    conn.commit()
    cur.close()
    conn.close()
    bot.reply_to(message, f"✅ Пользователю {target_id} продлена подписка на {days} дней.")
    try:
        expire_date = datetime.fromtimestamp(new_end).strftime("%d.%m.%Y в %H:%M")
        bot.send_message(target_id, f"🎉 Ваша подписка продлена на {days} дней!\n📅 Действует до: {expire_date}")
    except:
        pass

@bot.message_handler(commands=['remove'])
def cmd_remove(message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Использование: /remove [ID]")
        return
    try:
        target_id = int(args[1])
    except:
        bot.reply_to(message, "❌ Неверный ID.")
        return
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (current_time - 1, target_id))
    conn.commit()
    cur.close()
    conn.close()
    bot.reply_to(message, f"✅ Подписка пользователя {target_id} удалена.")
    try:
        bot.send_message(target_id, "❌ Ваша подписка была удалена администратором.")
    except:
        pass

@bot.message_handler(commands=['add_admin'])
def cmd_add_admin(message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Использование: /add_admin [ID]")
        return
    try:
        target_id = int(args[1])
    except:
        bot.reply_to(message, "❌ Неверный ID.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = %s", (target_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        bot.reply_to(message, f"❌ Пользователь {target_id} не найден в базе.")
        return
    cur.execute(
        "INSERT INTO admins (user_id, added_by, added_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (target_id, ADMIN_ID, int(time.time()))
    )
    conn.commit()
    cur.close()
    conn.close()
    bot.reply_to(message, f"✅ Пользователю {target_id} выдан админ-доступ.")
    try:
        bot.send_message(target_id, "👑 Вам выдан доступ администратора!")
    except:
        pass

@bot.message_handler(commands=['remove_admin'])
def cmd_remove_admin(message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Использование: /remove_admin [ID]")
        return
    try:
        target_id = int(args[1])
    except:
        bot.reply_to(message, "❌ Неверный ID.")
        return
    if target_id == ADMIN_ID:
        bot.reply_to(message, "❌ Нельзя удалить создателя.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE user_id = %s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    bot.reply_to(message, f"✅ Админ-доступ у пользователя {target_id} отозван.")
    try:
        bot.send_message(target_id, "❌ Ваш доступ администратора был отозван.")
    except:
        pass

@bot.message_handler(commands=['admins_list'])
def cmd_admins_list(message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, added_at FROM admins")
    admins = cur.fetchall()
    cur.close()
    conn.close()
    text = f"👑 Список администраторов:\n\n👤 Создатель: {ADMIN_ID}\n\n"
    if not admins:
        text += "📋 Дополнительных администраторов нет."
    else:
        for admin_id, added_at in admins:
            name = get_user_display_name(admin_id)
            text += f"└ {name} (ID: {admin_id})\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=['block'])
def cmd_block(message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Использование: /block [ID]")
        return
    try:
        target_id = int(args[1])
    except:
        bot.reply_to(message, "❌ Неверный ID.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_blocked = 1 WHERE user_id = %s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    bot.reply_to(message, f"✅ Пользователь {target_id} заблокирован.")
    try:
        bot.send_message(target_id, f"🚫 Вы заблокированы администратором.\n\nДля выяснения причин обратитесь в поддержку: {SUPPORT}")
    except:
        pass

@bot.message_handler(commands=['unblock'])
def cmd_unblock(message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Использование: /unblock [ID]")
        return
    try:
        target_id = int(args[1])
    except:
        bot.reply_to(message, "❌ Неверный ID.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_blocked = 0 WHERE user_id = %s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    bot.reply_to(message, f"✅ Пользователь {target_id} разблокирован.")
    try:
        bot.send_message(target_id, "✅ Вы разблокированы! Теперь вы можете пользоваться ботом.")
    except:
        pass

@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.replace('/broadcast', '').strip()
    if not text and not message.reply_to_message:
        bot.reply_to(message,
            "📢 Использование:\n"
            "1. /broadcast Текст сообщения\n"
            "2. Ответьте на фото командой /broadcast Подпись"
        )
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE is_blocked = 0")
    users = cur.fetchall()
    cur.close()
    conn.close()
    success = 0
    fail = 0
    for user in users:
        try:
            if message.reply_to_message and message.reply_to_message.photo:
                photo = message.reply_to_message.photo[-1].file_id
                bot.send_photo(user[0], photo, caption=text)
            else:
                bot.send_message(user[0], text)
            success += 1
            time.sleep(0.05)
        except:
            fail += 1
    bot.reply_to(message, f"✅ Рассылка завершена.\n\n📤 Отправлено: {success}\n❌ Не доставлено: {fail}")

# ==================== UPDATE KEYS ====================

@bot.message_handler(commands=['update_keys'])
def cmd_update_keys(message):
    if not is_admin(message.from_user.id):
        return

    # Режим 1: ответ на документ или текст
    if message.reply_to_message:
        reply = message.reply_to_message
        if reply.document:
            try:
                file_info = bot.get_file(reply.document.file_id)
                downloaded = bot.download_file(file_info.file_path)
                text = downloaded.decode('utf-8', errors='ignore')
                bot.reply_to(message, "⏳ Читаю файл...")
                keys = load_keys_from_text(text)
                _finish_update_keys(message, keys, f"файл: {reply.document.file_name}")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка чтения файла: {e}")
            return
        if reply.text:
            t = reply.text.strip()
            if re.match(r'https?://', t) or re.match(
                r'(?:incy|happ|v2rayng|shadowrocket|clash|sing-box)://', t, re.IGNORECASE
            ):
                bot.reply_to(message, "⏳ Загружаю ключи...")
                keys = load_keys_from_url(t)
                _finish_update_keys(message, keys, t[:60])
            else:
                bot.reply_to(message, "⏳ Читаю ключи из текста...")
                keys = load_keys_from_text(t)
                _finish_update_keys(message, keys, "текстовое сообщение")
            return

    # Режим 2: URL в аргументе
    args = message.text.split(maxsplit=1)
    if len(args) >= 2:
        url = args[1].strip()
        bot.reply_to(message, "⏳ Загружаю ключи...")
        keys = load_keys_from_url(url)
        _finish_update_keys(message, keys, url[:60])
        return

    # Режим 3: ждём следующего сообщения
    search_cache[message.from_user.id] = 'waiting_for_keys'
    bot.reply_to(
        message,
        "📥 Отправьте ключи одним из способов:\n\n"
        "• Ссылка: `https://...` или `https://happ.dska.su/https://...`\n"
        "• Схема: `incy://add/...` `happ://add/...`\n"
        "• .txt файл с ключами\n"
        "• Текст с ключами vless:// vmess:// и др.",
        parse_mode="Markdown"
    )

# ==================== KEY PARSING FUNCS ====================

def load_keys_from_url(raw_url):
    url = _resolve_url(raw_url)
    if not re.match(r'https?://', url, re.IGNORECASE):
        decoded = _try_b64(url)
        if decoded:
            keys = _extract_vpn_keys(decoded)
            if keys:
                return _dedup(keys)
        return _dedup(_extract_vpn_keys(url))
    headers = {'User-Agent': 'v2rayNG/1.8.7', 'Accept': '*/*'}
    content = None
    for verify in [True, False]:
        try:
            resp = requests.get(url, headers=headers, timeout=30, verify=verify)
            content = resp.text
            break
        except:
            continue
    if not content:
        return []
    return _parse_keys_from_content(content)

def load_keys_from_text(text):
    return _parse_keys_from_content(text)

def _resolve_url(raw_url):
    raw_url = raw_url.strip()
    app_scheme = re.match(
        r'^(?:incy|happ|v2ray|v2rayng|v2box|clash|sing-box|quantumult|surge|loon|shadowrocket|stash|nekoray|hiddify|streisand|karing|mihomo|flclash)://(?:add|sub|crypt|import|install|update)/(.+)',
        raw_url, re.IGNORECASE
    )
    if app_scheme:
        payload = urllib.parse.unquote(app_scheme.group(1).strip())
        if re.match(r'https?://', payload, re.IGNORECASE):
            return payload
        return payload
    proxy_wrap = re.match(r'https?://[^/]+/(https?://.+)', raw_url, re.IGNORECASE)
    if proxy_wrap:
        return proxy_wrap.group(1)
    return raw_url

def _parse_keys_from_content(content):
    all_keys = []
    all_keys.extend(_extract_vpn_keys(content))
    decoded = _try_b64(content.strip())
    if decoded:
        all_keys.extend(_extract_vpn_keys(decoded))
    for line in content.splitlines():
        line = line.strip()
        if len(line) < 20:
            continue
        d = _try_b64(line)
        if d:
            all_keys.extend(_extract_vpn_keys(d))
    if '<' in content:
        try:
            soup = BeautifulSoup(content, 'html5lib')
            for elem in soup.find_all(string=True):
                all_keys.extend(_extract_vpn_keys(str(elem)))
        except:
            pass
    try:
        json_matches = re.findall(
            r'"([^"]*(?:vless|vmess|trojan|ss|ssr|hysteria)[^"]*)"', content, re.IGNORECASE
        )
        for m in json_matches:
            all_keys.extend(_extract_vpn_keys(m))
    except:
        pass
    return _dedup(all_keys)

def _finish_update_keys(message, keys, source_label):
    if not keys:
        bot.reply_to(
            message,
            "❌ Не найдено ни одного VPN ключа.\n\n"
            "Поддерживается:\n"
            "• vless:// vmess:// trojan:// ss:// и др.\n"
            "• Base64 подписка\n"
            "• URL подписки (включая happ.dska.su и подобные)\n"
            "• HTML страница с ключами"
        )
        return
    save_keys_to_db(keys)
    proto_stats = {}
    for k in keys:
        m = re.match(r'([a-z0-9+]+)://', k, re.IGNORECASE)
        if m:
            p = m.group(1).lower()
            proto_stats[p] = proto_stats.get(p, 0) + 1
    stats = '\n'.join(f"  • {p}:// — {c}" for p, c in sorted(proto_stats.items(), key=lambda x: -x[1]))
    bot.reply_to(
        message,
        f"✅ Ключи обновлены!\n\n"
        f"📊 Загружено ключей: {len(keys)}\n"
        f"📋 По протоколам:\n{stats}\n"
        f"🔗 Источник: {source_label}"
    )

# ==================== REF COMMANDS ====================

@bot.message_handler(commands=['ref_on'])
def cmd_ref_on(message):
    if message.from_user.id != ADMIN_ID:
        return
    set_setting('referral_enabled', '1')
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT referrer_id FROM referrals WHERE rewarded = 0")
    referrers = cur.fetchall()
    total_rewarded = 0
    for (referrer_id,) in referrers:
        cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (referrer_id,))
        ref_result = cur.fetchone()
        if not ref_result:
            continue
        cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = %s AND rewarded = 0", (referrer_id,))
        count = cur.fetchone()[0]
        new_end = ref_result[0] + count * 3 * 24 * 60 * 60
        cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (new_end, referrer_id))
        cur.execute("UPDATE referrals SET rewarded = 1 WHERE referrer_id = %s AND rewarded = 0", (referrer_id,))
        total_rewarded += count
        try:
            bot.send_message(referrer_id,
                f"🎉 Реферальная система включена!\n\n"
                f"За {count} приглашённых вами рефералов начислено {count*3} дней.\n"
                f"Спасибо, что приглашаете друзей! 🌟"
            )
        except:
            pass
    conn.commit()
    cur.close()
    conn.close()
    bot.reply_to(message,
        f"✅ Реферальная система ВКЛЮЧЕНА.\n"
        f"Начислено {total_rewarded*3} дней {total_rewarded} рефералам."
    )

@bot.message_handler(commands=['ref_off'])
def cmd_ref_off(message):
    if message.from_user.id != ADMIN_ID:
        return
    set_setting('referral_enabled', '0')
    bot.reply_to(message, "❌ Реферальная система ВЫКЛЮЧЕНА. Новые рефералы сохраняются, но дни не начисляются.")

@bot.message_handler(commands=['ref_status'])
def cmd_ref_status(message):
    if not is_admin(message.from_user.id):
        return
    status = "ВКЛЮЧЕНА ✅" if get_setting('referral_enabled') == '1' else "ВЫКЛЮЧЕНА ❌"
    bot.reply_to(message, f"📊 Реферальная система: {status}")

# ==================== /manage ====================

manage_cache = {}

def build_user_list_keyboard(users, page, filter_type='all'):
    kb = types.InlineKeyboardMarkup(row_width=2)
    per_page = 5
    start = page * per_page
    end = start + per_page
    page_users = users[start:end]
    current_time = int(time.time())
    for uid in page_users:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT subscription_end, is_blocked FROM users WHERE user_id = %s", (uid,))
        udata = cur.fetchone()
        cur.close()
        conn.close()
        if udata:
            sub_end, blk = udata
            if blk:
                status_icon = "🚫"
            elif sub_end and sub_end > current_time:
                status_icon = "🟢"
            else:
                status_icon = "🔴"
        else:
            status_icon = "❓"
        admin_icon = "👑 " if is_admin(uid) else ""
        name = get_user_display_name(uid)
        display = f"{status_icon} {admin_icon}{name}"[:40]
        kb.add(types.InlineKeyboardButton(display, callback_data=f"user_{uid}"))
    nav_row = []
    if page > 0:
        nav_row.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page-1}_{filter_type}"))
    if end < len(users):
        nav_row.append(types.InlineKeyboardButton("Вперед ▶️", callback_data=f"page_{page+1}_{filter_type}"))
    if nav_row:
        kb.row(*nav_row)
    kb.row(
        types.InlineKeyboardButton("🟢 Активные", callback_data="filter_active"),
        types.InlineKeyboardButton("🔴 Неактивные", callback_data="filter_inactive")
    )
    kb.row(
        types.InlineKeyboardButton("👑 Админы", callback_data="filter_admins"),
        types.InlineKeyboardButton("📋 Все приглашавшие", callback_data="filter_referrers")
    )
    kb.row(
        types.InlineKeyboardButton("🏆 Топ рефералов", callback_data="top_refs_admin"),
        types.InlineKeyboardButton("🔄 Обновить", callback_data="filter_all")
    )
    kb.row(types.InlineKeyboardButton("❌ Закрыть", callback_data="close_manage"))
    return kb

@bot.message_handler(commands=['manage'])
def cmd_manage(message):
    if not is_admin(message.from_user.id):
        return
    admin_id = message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE is_blocked = 0 ORDER BY user_id")
    users = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    if not users:
        bot.reply_to(message, "📭 Нет активных пользователей.")
        return
    manage_cache[admin_id] = {'users': users, 'filter': 'all'}
    kb = build_user_list_keyboard(users, 0, 'all')
    bot.reply_to(message, f"👥 Пользователи ({len(users)}):", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith('filter_') or call.data.startswith('page_') or call.data in ['close_manage', 'back_to_list', 'top_refs_admin'])
def callback_manage_filters(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Нет доступа.")
        return
    admin_id = call.from_user.id
    current_time = int(time.time())
    data = call.data
    if data == 'close_manage':
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        return
    if data == 'top_refs_admin':
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT referrer_id, COUNT(*) FROM referrals GROUP BY referrer_id ORDER BY COUNT(*) DESC LIMIT 10")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        text = "🏆 *Топ рефералов:*\n\n"
        for i, (ref_id, count) in enumerate(rows):
            name = get_user_display_name(ref_id)
            text += f"{i+1}. {name} (ID: {ref_id}) — {count} реф.\n"
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        except:
            bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
        return
    if data == 'back_to_list':
        cached = manage_cache.get(admin_id, {})
        users = cached.get('users', [])
        filter_type = cached.get('filter', 'all')
        if not users:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users ORDER BY user_id")
            users = [row[0] for row in cur.fetchall()]
            cur.close()
            conn.close()
        kb = build_user_list_keyboard(users, 0, filter_type)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(f"👥 Пользователи ({len(users)}):", call.message.chat.id, call.message.message_id, reply_markup=kb)
        except:
            pass
        return
    if data.startswith('page_'):
        parts = data.split('_')
        page = int(parts[1])
        filter_type = parts[2] if len(parts) > 2 else 'all'
        cached = manage_cache.get(admin_id, {})
        users = cached.get('users', [])
        kb = build_user_list_keyboard(users, page, filter_type)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(f"👥 Пользователи ({len(users)}):", call.message.chat.id, call.message.message_id, reply_markup=kb)
        except:
            pass
        return
    conn = get_db_connection()
    cur = conn.cursor()
    if data == 'filter_active':
        cur.execute(f"SELECT user_id FROM users WHERE is_blocked = 0 AND subscription_end > {current_time} ORDER BY user_id")
        filter_type = 'active'
    elif data == 'filter_inactive':
        cur.execute(f"SELECT user_id FROM users WHERE is_blocked = 0 AND subscription_end < {current_time} ORDER BY user_id")
        filter_type = 'inactive'
    elif data == 'filter_admins':
        cur.execute("SELECT user_id FROM admins ORDER BY user_id")
        filter_type = 'admins'
    elif data == 'filter_referrers':
        cur.execute("SELECT DISTINCT referrer_id FROM referrals ORDER BY referrer_id")
        filter_type = 'referrers'
    else:
        cur.execute("SELECT user_id FROM users ORDER BY user_id")
        filter_type = 'all'
    users = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    manage_cache[admin_id] = {'users': users, 'filter': filter_type}
    kb = build_user_list_keyboard(users, 0, filter_type)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(f"👥 Пользователи ({len(users)}):", call.message.chat.id, call.message.message_id, reply_markup=kb)
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('user_') and len(call.data.split('_')) == 2)
def callback_user_detail(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Нет доступа.")
        return
    try:
        target_id = int(call.data.split('_')[1])
    except:
        return
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT subscription_end, is_blocked FROM users WHERE user_id = %s", (target_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    if not result:
        bot.answer_callback_query(call.id, "❌ Пользователь не найден.")
        return
    subscription_end, blk = result
    if blk:
        status = "🚫 Заблокирован"
    elif subscription_end and subscription_end > current_time:
        days_left = (subscription_end - current_time) // (24 * 60 * 60)
        status = f"🟢 Активна (осталось {days_left} дн)"
    else:
        status = "🔴 Неактивна"
    is_admin_user = is_admin(target_id)
    admin_text = "✅ Да" if is_admin_user else "❌ Нет"
    name = get_user_display_name(target_id)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📅 Продлить (+30 дн)", callback_data=f"prolong_{target_id}_30"),
        types.InlineKeyboardButton("🗑️ Удалить подписку", callback_data=f"remove_sub_{target_id}")
    )
    if blk:
        kb.add(types.InlineKeyboardButton("🔓 Разблокировать", callback_data=f"unblock_{target_id}"))
    else:
        kb.add(types.InlineKeyboardButton("🔒 Заблокировать", callback_data=f"block_{target_id}"))
    if is_admin_user:
        kb.add(types.InlineKeyboardButton("👑 Забрать админку", callback_data=f"remove_admin_{target_id}"))
    else:
        kb.add(types.InlineKeyboardButton("👑 Выдать админку", callback_data=f"add_admin_{target_id}"))
    kb.row(
        types.InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list"),
        types.InlineKeyboardButton("❌ Закрыть", callback_data="close_manage")
    )
    text = (
        f"👤 *{name}*\n"
        f"🆔 ID: `{target_id}`\n"
        f"📊 Статус: {status}\n"
        f"👑 Админ: {admin_text}"
    )
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('prolong_'))
def callback_prolong(call):
    if not is_admin(call.from_user.id):
        return
    parts = call.data.split('_')
    target_id = int(parts[1])
    days = int(parts[2])
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (target_id,))
    result = cur.fetchone()
    if not result:
        bot.answer_callback_query(call.id, "❌ Пользователь не найден.")
        cur.close()
        conn.close()
        return
    current_end = result[0] if (result[0] and result[0] > current_time) else current_time
    new_end = current_end + days * 24 * 60 * 60
    cur.execute("UPDATE users SET subscription_end = %s, notified_3days = 0 WHERE user_id = %s", (new_end, target_id))
    conn.commit()
    cur.close()
    conn.close()
    bot.answer_callback_query(call.id, "✅ Подписка продлена!")
    try:
        bot.send_message(target_id, f"🎉 Ваша подписка продлена на {days} дней!")
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_sub_'))
def callback_remove_sub(call):
    if not is_admin(call.from_user.id):
        return
    target_id = int(call.data.split('_')[2])
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (current_time - 1, target_id))
    conn.commit()
    cur.close()
    conn.close()
    bot.answer_callback_query(call.id, "✅ Подписка удалена!")
    try:
        bot.send_message(target_id, "❌ Ваша подписка была удалена администратором.")
    except:
        pass

@bot.callback_query_handler(func=lambda call: re.match(r'^block_\d+$', call.data) is not None)
def callback_block(call):
    if not is_admin(call.from_user.id):
        return
    target_id = int(call.data.split('_')[1])
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_blocked = 1 WHERE user_id = %s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    bot.answer_callback_query(call.id, "✅ Пользователь заблокирован!")
    try:
        bot.send_message(target_id, f"🚫 Вы заблокированы администратором.\n\nОбратитесь в поддержку: {SUPPORT}")
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('unblock_'))
def callback_unblock(call):
    if not is_admin(call.from_user.id):
        return
    target_id = int(call.data.split('_')[1])
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_blocked = 0 WHERE user_id = %s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    bot.answer_callback_query(call.id, "✅ Пользователь разблокирован!")
    try:
        bot.send_message(target_id, "✅ Вы разблокированы! Теперь вы можете пользоваться ботом.")
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('add_admin_'))
def callback_add_admin(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Только создатель может выдавать админку.")
        return
    target_id = int(call.data.split('_')[2])
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO admins (user_id, added_by, added_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (target_id, ADMIN_ID, int(time.time()))
    )
    conn.commit()
    cur.close()
    conn.close()
    bot.answer_callback_query(call.id, "✅ Админ-доступ выдан!")
    try:
        bot.send_message(target_id, "👑 Вам выдан доступ администратора!")
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_admin_'))
def callback_remove_admin_cb(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Только создатель может отзывать админку.")
        return
    target_id = int(call.data.split('_')[2])
    if target_id == ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Нельзя удалить создателя.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE user_id = %s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    bot.answer_callback_query(call.id, "✅ Админ-доступ отозван!")
    try:
        bot.send_message(target_id, "❌ Ваш доступ администратора был отозван.")
    except:
        pass

# ==================== DOCUMENT HANDLER ====================

@bot.message_handler(content_types=['document'])
def handle_document(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id

    # Режим расшифровки
    if user_id in decrypt_results and decrypt_results[user_id].get('waiting'):
        if is_blocked(user_id):
            bot.reply_to(message, "🚫 Вы заблокированы.")
            del decrypt_results[user_id]
            return
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            _do_decrypt(message, user_id, file_bytes=downloaded, file_name=message.document.file_name)
        except Exception as e:
            bot.reply_to(message, f"❌ Не удалось прочитать файл: {e}")
        return

    # Режим ожидания ключей от админа
    if search_cache.get(user_id) == 'waiting_for_keys' and is_admin(user_id):
        del search_cache[user_id]
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            text = downloaded.decode('utf-8', errors='ignore')
            bot.reply_to(message, "⏳ Читаю файл...")
            keys = load_keys_from_text(text)
            _finish_update_keys(message, keys, f"файл: {message.document.file_name}")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    # Режим проверки ключей
    if user_id in check_results and check_results[user_id].get('waiting'):
        if is_blocked(user_id):
            bot.reply_to(message, "🚫 Вы заблокированы.")
            return
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            text = downloaded_file.decode('utf-8')
            keys = re.findall(r'vless://[^\s<>"\']+', text)
        except:
            bot.reply_to(message, "❌ Не удалось прочитать файл.")
            return
        _process_keys(message, keys, user_id)

# ==================== TEXT HANDLER ====================

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id

    if message.text and message.text.startswith('/'):
        return

    # Режим расшифровки подписки
    if user_id in decrypt_results and decrypt_results[user_id].get('waiting'):
        if is_blocked(user_id):
            bot.reply_to(message, "🚫 Вы заблокированы.")
            del decrypt_results[user_id]
            return
        _do_decrypt(message, user_id, text=message.text or '')
        return

    # Режим ожидания ключей от админа (waiting_for_keys)
    if search_cache.get(user_id) == 'waiting_for_keys' and is_admin(user_id):
        del search_cache[user_id]
        t = (message.text or '').strip()
        if re.match(r'https?://', t) or re.match(
            r'(?:incy|happ|v2rayng|shadowrocket|clash|sing-box)://', t, re.IGNORECASE
        ):
            bot.reply_to(message, "⏳ Загружаю ключи...")
            keys = load_keys_from_url(t)
            _finish_update_keys(message, keys, t[:60])
        else:
            bot.reply_to(message, "⏳ Читаю ключи из текста...")
            keys = load_keys_from_text(t)
            _finish_update_keys(message, keys, "текстовое сообщение")
        return

    # Режим проверки ключей
    if user_id in check_results and check_results[user_id].get('waiting'):
        if is_blocked(user_id):
            bot.reply_to(message, "🚫 Вы заблокированы.")
            return
        keys = re.findall(r'vless://[^\s<>"\']+', message.text or '')
        _process_keys(message, keys, user_id)
        return

    # Режим поиска (manage)
    if search_cache.get(user_id) == 'waiting_for_search':
        del search_cache[user_id]
        query = (message.text or '').strip()
        if query.startswith('@'):
            try:
                chat = bot.get_chat(query)
                target_id = chat.id
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT subscription_end, is_blocked FROM users WHERE user_id = %s", (target_id,))
                result = cur.fetchone()
                cur.close()
                conn.close()
                if result:
                    bot.reply_to(message, f"✅ Найден: {query} (ID: {target_id})\nПодписка до: {result[0]}, Блок: {result[1]}")
                else:
                    bot.reply_to(message, "❌ Пользователь не найден в базе.")
            except:
                bot.reply_to(message, "❌ Пользователь не найден.")
        else:
            try:
                target_id = int(query)
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT subscription_end, is_blocked FROM users WHERE user_id = %s", (target_id,))
                result = cur.fetchone()
                cur.close()
                conn.close()
                if result:
                    name = get_user_display_name(target_id)
                    bot.reply_to(message, f"✅ Найден: {name} (ID: {target_id})\nПодписка до: {result[0]}, Блок: {result[1]}")
                else:
                    bot.reply_to(message, "❌ Пользователь не найден.")
            except:
                bot.reply_to(message, "❌ Неверный запрос.")
        return

    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return

def _process_keys(message, keys, user_id):
    if not keys:
        bot.reply_to(message, "❌ Не найдено ключей в формате vless://")
        return
    keys = list(dict.fromkeys(keys))
    msg = bot.reply_to(
        message,
        f"🔍 Найдено ключей: {len(keys)}\n⏳ Начинаю проверку...\nЭто может занять до 30 секунд."
    )
    t = threading.Thread(target=check_keys_async, args=(message.chat.id, keys, user_id, msg.message_id))
    t.daemon = True
    t.start()

# ==================== FLASK ROUTES ====================

@app.route('/ping')
def ping():
    return "ok", 200

@app.route('/')
def index():
    return "PotyjnoVPN Bot is running", 200

@app.route('/sub/<token>')
def sub(token):
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, subscription_end, is_blocked FROM users WHERE token = %s", (token,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    if not result:
        return "Подписка не найдена", 404
    user_id, subscription_end, blk = result
    if (not subscription_end or subscription_end < current_time) or blk == 1:
        return "Подписка истекла или пользователь заблокирован", 403
    keys = get_keys_from_db()
    if not keys:
        keys = DEFAULT_KEYS
    content = KEY_TEMPLATE.format(expire=subscription_end, keys='\n'.join(keys))
    return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}

# ==================== MAIN ====================

def run_bot():
    bot.infinity_polling(skip_pending=True)

if __name__ == '__main__':
    init_db()
    if not get_keys_from_db():
        save_keys_to_db(DEFAULT_KEYS)
    thread = Thread(target=run_bot)
    thread.daemon = True
    thread.start()
    from waitress import serve
    serve(app, host='0.0.0.0', port=10000)
