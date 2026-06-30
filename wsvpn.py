import os
import sys
import re
import time
import socket
import string
import random
import threading
import traceback
import base64
import urllib.parse
import io
import json
import warnings
import urllib3
from datetime import datetime, timedelta
from threading import Thread, Lock, Event
from waitress import serve
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from functools import wraps
from collections import defaultdict
import uuid

import telebot
from telebot import types
import psycopg2
from psycopg2 import pool
import requests
from bs4 import BeautifulSoup
from flask import Flask, request

# ==================== КОНСТАНТЫ ====================

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

try:
    import socks
    SOCKS_AVAILABLE = True
except ImportError:
    SOCKS_AVAILABLE = False

_cache_lock = Lock()
_subscribe_monitor_lock = Lock()
_captcha_lock = Lock()
_keys_lock = Lock()
_user_name_cache_lock = Lock()
_last_activity_lock = Lock()
_rate_limit_lock = Lock()

MENU_BUTTONS = {
    "👤 Личный кабинет", "📡 Моя подписка",
    "👥 Рефералы", "🏆 Топ рефералов",
    "ℹ️ Стаж бота", "📋 Правила",
    "❓ Поддержка"
}

def keep_alive_ping():
    url = os.getenv('RENDER_EXTERNAL_URL', '')
    if not url:
        url = os.getenv('PUBLIC_URL', '')
    if not url:
        url = 'https://wsvpn-bobot.onrender.com'
    url = url.rstrip('/')
    print(f"[keep_alive] Запущен пинг-механизм для {url}")
    ping_count = 0
    while True:
        try:
            response = requests.get(f"{url}/ping", timeout=10)
            ping_count += 1
            if ping_count > 1000000:
                ping_count = 0
            print(f"[keep_alive] Пинг #{ping_count} в {datetime.now().strftime('%H:%M:%S')}: {response.status_code}")
            requests.get(f"{url}/health", timeout=10)
        except SystemExit:
            break
        except Exception as e:
            print(f"[keep_alive] Ошибка пинга: {e}")
        time.sleep(240)

def auto_restart_monitor():
    max_idle_time = 600
    print(f"[auto_restart] Запущен монитор перезапуска")
    while True:
        try:
            current_time = time.time()
            with _last_activity_lock:
                idle_time = current_time - last_activity_time
            if idle_time > max_idle_time:
                print(f"[auto_restart] Длительное бездействие, выполняем мягкий перезапуск...")
                try:
                    url = os.getenv('RENDER_EXTERNAL_URL', 'https://wsvpn-bobot.onrender.com')
                    for _ in range(3):
                        requests.get(f"{url}/ping", timeout=5)
                        time.sleep(1)
                except:
                    pass
            time.sleep(30)
        except:
            time.sleep(60)

last_activity_time = time.time()

def update_activity():
    global last_activity_time
    with _last_activity_lock:
        last_activity_time = time.time()

BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '8176196456'))
DATABASE_URL = os.getenv('DATABASE_URL')
CHANNEL_ID = -1003848589461
CHANNEL_LINK = 'https://t.me/WS_JuJuB01_vpn_keys'
SUPPORT = '@WS_JuJuB01'

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)
app = Flask(__name__)

def get_bot_base_url():
    base_url = os.getenv('RENDER_EXTERNAL_URL', '')
    if not base_url:
        base_url = os.getenv('PUBLIC_URL', 'https://wsvpn-bobot.onrender.com')
    base_url = base_url.rstrip('/')
    if not base_url.startswith(('http://', 'https://')):
        base_url = 'https://' + base_url
    return base_url

db_pool = None

def init_db_pool():
    global db_pool
    try:
        db_pool = pool.SimpleConnectionPool(1, 20, DATABASE_URL)
        print("[db_pool] ✅ Пул соединений инициализирован (min=1, max=20)")
    except Exception as e:
        print(f"[db_pool] ❌ Ошибка инициализации пула: {e}")
        db_pool = None

def get_db_connection():
    if db_pool:
        return db_pool.getconn()
    return psycopg2.connect(DATABASE_URL)

def return_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)
    elif conn:
        conn.close()

search_cache = {}
announce_data = {}
manage_cache = {}
captcha_sessions = {}
keys_loading = {}

_user_name_cache = {}
USER_NAME_CACHE_TTL = 3600

_keys_cache = None
_keys_cache_time = 0
KEYS_CACHE_TTL = 60

_bot_username = None
_bot_username_lock = Lock()

_user_blocked_cache = {}
_user_blocked_cache_lock = Lock()
USER_BLOCKED_CACHE_TTL = 3600

def safe_get_cache(cache_dict, key, default=None):
    with _cache_lock:
        return cache_dict.get(key, default)

def safe_set_cache(cache_dict, key, value):
    with _cache_lock:
        cache_dict[key] = value

def safe_del_cache(cache_dict, key):
    with _cache_lock:
        if key in cache_dict:
            del cache_dict[key]

def safe_cache_keys(cache_dict):
    with _cache_lock:
        return list(cache_dict.keys())

SESSION_TIMEOUT = 3600

def cleanup_expired_sessions():
    current_time = int(time.time())
    
    with _cache_lock:
        to_remove = [uid for uid, session in captcha_sessions.items() 
                     if current_time - session.get('timestamp', 0) > SESSION_TIMEOUT]
        for uid in to_remove:
            del captcha_sessions[uid]
        
        to_remove = [uid for uid, cache in search_cache.items() 
                     if current_time - cache.get('timestamp', 0) > SESSION_TIMEOUT]
        for uid in to_remove:
            del search_cache[uid]
        
        to_remove = [uid for uid, data in announce_data.items() 
                     if current_time - data.get('timestamp', 0) > SESSION_TIMEOUT]
        for uid in to_remove:
            del announce_data[uid]
        
        to_remove = [uid for uid, data in keys_loading.items() 
                     if current_time - data.get('timestamp', 0) > SESSION_TIMEOUT]
        for uid in to_remove:
            del keys_loading[uid]
        
        to_remove = [uid for uid, data in manage_cache.items() 
                     if current_time - data.get('timestamp', 0) > SESSION_TIMEOUT]
        for uid in to_remove:
            del manage_cache[uid]
    
    with _user_name_cache_lock:
        to_remove = [
            uid for uid, data in _user_name_cache.items()
            if current_time - data.get('timestamp', 0) > USER_NAME_CACHE_TTL
        ]
        for uid in to_remove:
            del _user_name_cache[uid]
    
    with _user_blocked_cache_lock:
        to_remove = [
            uid for uid, data in _user_blocked_cache.items()
            if current_time - data.get('timestamp', 0) > USER_BLOCKED_CACHE_TTL * 2
        ]
        for uid in to_remove:
            del _user_blocked_cache[uid]

def cleanup_sessions_scheduler():
    print("[cleanup] Запущен планировщик очистки сессий")
    last_notify = 0
    last_expired_notify = 0
    while True:
        try:
            cleanup_expired_sessions()
            current_time = int(time.time())
            
            if current_time - last_notify >= 3600:
                _notify_expiring_subscriptions()
                last_notify = current_time
            if current_time - last_expired_notify >= 6 * 3600:
                _notify_expired_subscriptions()
                last_expired_notify = current_time
            time.sleep(300)
        except Exception as e:
            print(f"[cleanup] Ошибка: {e}")
            time.sleep(60)

def _notify_expiring_subscriptions():
    current_time = int(time.time())
    threshold = current_time + 3 * 24 * 60 * 60
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, subscription_end FROM users
            WHERE is_blocked = 0
              AND notified_3days = 0
              AND subscription_end > %s
              AND subscription_end <= %s
        """, (current_time, threshold))
        rows = cur.fetchall()
        for user_id, sub_end in rows:
            days_left = (sub_end - current_time) // (24 * 60 * 60)
            try:
                bot.send_message(
                    user_id,
                    f"⚠️ *Подписка заканчивается через {days_left} дн.*\n\n"
                    f"Для продления обратитесь в поддержку: {SUPPORT}",
                    parse_mode="Markdown"
                )
                cur.execute(
                    "UPDATE users SET notified_3days = 1 WHERE user_id = %s",
                    (user_id,)
                )
                conn.commit()
            except Exception as e:
                print(f"[notify] Ошибка отправки {user_id}: {e}")
                continue
    except Exception as e:
        print(f"[notify] Ошибка: {e}")
        try:
            conn.rollback()
        except:
            pass
    finally:
        if cur:
            try:
                cur.close()
            except:
                pass
        return_db_connection(conn)

def _notify_expired_subscriptions():
    current_time = int(time.time())
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id FROM users
            WHERE is_blocked = 0
              AND is_frozen = 0
              AND notified_expired = 0
              AND subscription_end > 0
              AND subscription_end < %s
        """, (current_time,))
        rows = cur.fetchall()
        for (user_id,) in rows:
            try:
                bot.send_message(
                    user_id,
                    f"❌ *Ваша подписка истекла*\n\n"
                    f"Для продления обратитесь в поддержку: {SUPPORT}",
                    parse_mode="Markdown"
                )
                cur.execute(
                    "UPDATE users SET notified_expired = 1 WHERE user_id = %s",
                    (user_id,)
                )
                conn.commit()
            except Exception as e:
                print(f"[notify_expired] Ошибка отправки {user_id}: {e}")
                continue
    except Exception as e:
        print(f"[notify_expired] Ошибка: {e}")
        try:
            conn.rollback()
        except:
            pass
    finally:
        if cur:
            try:
                cur.close()
            except:
                pass
        return_db_connection(conn)

KEY_TEMPLATE = """\
#profile-title: WSVPN🐈‍⬛
#profile-update-interval: 1
#support-url: https://t.me/WS_JuJuB01
#announce: 📡 Полностью бесплатный | без скрытых условий/подписок | без логов
#channel: 📢 https://t.me/WS_JuJuB01_vpn_keys
#subscription-userinfo: upload=0; download=0; total=10995116277760000; expire={expire}
{keys}"""

DEFAULT_KEYS = [
    'vless://00000000-0000-0000-0000-000000000001@1.1.1.1:443?type=tcp&security=tls#Demo-Key-1',
]

def get_keys_from_db():
    global _keys_cache, _keys_cache_time
    current_time = time.time()
    
    with _keys_lock:
        if _keys_cache is not None and current_time - _keys_cache_time < KEYS_CACHE_TTL:
            return _keys_cache.copy()
        
        val = get_setting('vless_keys', '')
        if not val:
            keys = []
        else:
            keys = [k for k in val.split('|||') if k]
        
        _keys_cache = keys.copy()
        _keys_cache_time = current_time
        return keys

def save_keys_to_db(keys):
    global _keys_cache, _keys_cache_time
    cleaned = []
    seen = set()
    for k in keys:
        if k and k not in seen:
            cleaned.append(k)
            seen.add(k)
    
    with _keys_lock:
        set_setting('vless_keys', '|||'.join(cleaned))
        _keys_cache = cleaned.copy()
        _keys_cache_time = time.time()

def get_subscription_keys_from_db():
    val = get_setting('subscription_keys', '')
    if not val:
        return []
    return [k for k in val.split('|||') if k]

def save_subscription_keys_to_db(keys):
    cleaned = list(dict.fromkeys(k for k in keys if k))
    set_setting('subscription_keys', '|||'.join(cleaned))

def generate_subscription_token():
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=12))

def ensure_bot_start_time():
    existing = get_setting('bot_start_time', '')
    if not existing:
        set_setting('bot_start_time', str(int(time.time())))

PERMISSIONS = {
    'check_user': 'Проверка пользователя (/check)',
    'user_info': 'Информация о пользователе (/user)',
    'add_days': 'Выдача дней (/add_days)',
    'remove_days': 'Забирание дней (/remove_days)',
    'block_user': 'Блокировка (/block)',
    'unblock_user': 'Разблокировка (/unblock)',
    'announce': 'Рассылка',
    'manage_keys': 'Управление ключами',
    'manage_users': 'Управление пользователями',
    'admin_stats': 'Статистика бота',
    'admin_panel': 'Доступ к админ-панели',
    'view_logs': 'Просмотр логов',
    'manage_admins': 'Управление админами',
}

ROLE_PRESETS = {
    'owner': {
        'name': '👑 Владелец',
        'permissions': {p: True for p in PERMISSIONS}
    },
    'senior': {
        'name': '⭐ Старший админ',
        'permissions': {
            'check_user': True, 'user_info': True, 'add_days': True, 'remove_days': True,
            'block_user': True, 'unblock_user': True, 'announce': True, 'manage_keys': True,
            'manage_admins': False, 'manage_users': True, 'admin_stats': True,
            'admin_panel': True, 'view_logs': True,
        }
    },
    'junior': {
        'name': '🔹 Младший админ',
        'permissions': {
            'check_user': True, 'user_info': True, 'add_days': True, 'remove_days': True,
            'block_user': True, 'unblock_user': True, 'announce': False, 'manage_keys': False,
            'manage_admins': False, 'manage_users': False, 'admin_stats': False,
            'admin_panel': True, 'view_logs': False,
        }
    },
    'support': {
        'name': '🟢 Поддержка',
        'permissions': {
            'check_user': True, 'user_info': True, 'add_days': False, 'remove_days': False,
            'block_user': False, 'unblock_user': False, 'announce': False, 'manage_keys': False,
            'manage_admins': False, 'manage_users': False, 'admin_stats': False,
            'admin_panel': False, 'view_logs': False,
        }
    }
}

def log_admin_action(admin_id, action, target_id=None, details=None, target_name=None, ip_address=None):
    try:
        admin_name = get_user_display_name(admin_id)
        if target_id:
            target_name = target_name or get_user_display_name(target_id)
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO admin_logs 
                (admin_id, admin_name, action, target_id, target_name, details, ip_address, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                admin_id,
                admin_name,
                action,
                target_id,
                target_name,
                details,
                ip_address,
                int(time.time())
            ))
            conn.commit()
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
    except Exception as e:
        print(f"[log_admin_action] Ошибка: {e}")

def get_user_display_name_cached(user_id):
    current_time = int(time.time())
    
    with _user_name_cache_lock:
        cached = _user_name_cache.get(user_id, {})
        if cached.get('timestamp', 0) > current_time - USER_NAME_CACHE_TTL:
            return cached.get('name', str(user_id))
    
    try:
        chat = bot.get_chat(user_id)
        if chat.username:
            name = f"@{chat.username}"
        else:
            name = chat.first_name or ''
            if chat.last_name:
                name += ' ' + chat.last_name
            name = name.strip() or str(user_id)
    except Exception as e:
        print(f"[get_user_display_name_cached] Ошибка для {user_id}: {e}")
        name = str(user_id)
    
    with _user_name_cache_lock:
        _user_name_cache[user_id] = {
            'name': name,
            'timestamp': current_time
        }
    
    return name

def get_bot_username():
    global _bot_username
    with _bot_username_lock:
        if not _bot_username:
            try:
                _bot_username = bot.get_me().username
            except Exception as e:
                print(f"[get_bot_username] Ошибка: {e}")
                return "WSVPN_Bobot"
        return _bot_username

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                subscription_end BIGINT,
                notified_3days INTEGER DEFAULT 0,
                last_activity BIGINT DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                token TEXT UNIQUE,
                username TEXT,
                telegram_id BIGINT,
                notified_expired INTEGER DEFAULT 0,
                is_frozen INTEGER DEFAULT 0,
                frozen_days_left INTEGER DEFAULT 0,
                frozen_at BIGINT DEFAULT 0
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_subscription_end ON users(subscription_end)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_notified_3days ON users(notified_3days)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")
        conn.commit()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY
            )
        """)
        conn.commit()
        
        cur.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'junior'")
        cur.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS permissions TEXT")
        cur.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS added_by BIGINT")
        cur.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS added_at BIGINT")
        conn.commit()
        
        try:
            cur.execute("""
                INSERT INTO admins (user_id, role, permissions, added_by, added_at) 
                VALUES (%s, %s, %s, %s, %s) 
                ON CONFLICT (user_id) DO UPDATE SET role = %s, permissions = %s
            """, (ADMIN_ID, 'owner', json.dumps({p: True for p in PERMISSIONS}), ADMIN_ID, int(time.time()), 'owner', json.dumps({p: True for p in PERMISSIONS})))
            conn.commit()
            print(f"[init] ✅ Создатель {ADMIN_ID} добавлен с ролью Владелец")
        except Exception as e:
            print(f"[init] Ошибка добавления создателя: {e}")
            conn.rollback()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT,
                referred_id BIGINT,
                reward_date BIGINT,
                rewarded INTEGER DEFAULT 0,
                referrer_subscribed INTEGER DEFAULT 0,
                referred_subscribed INTEGER DEFAULT 0,
                UNIQUE(referrer_id, referred_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                admin_name TEXT,
                action TEXT NOT NULL,
                target_id BIGINT,
                target_name TEXT,
                details TEXT,
                ip_address TEXT,
                created_at BIGINT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_logs_admin_id ON admin_logs(admin_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_logs_created_at ON admin_logs(created_at DESC)")
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_channels (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL UNIQUE,
                channel_name TEXT,
                enabled BOOLEAN DEFAULT TRUE,
                added_by BIGINT,
                added_at BIGINT
            )
        """)
        conn.commit()
    except Exception as e:
        print(f"[init_db] Критическая ошибка: {e}")
        try:
            conn.rollback()
        except:
            pass
        raise
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    init_channel_settings()

def get_setting(key, default='0'):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        result = cur.fetchone()
        return result[0] if result else default
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def set_setting(key, value):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = %s",
            (key, value, value)
        )
        conn.commit()
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def increment_setting(key, by=1):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE
            SET value = (COALESCE(settings.value, '0')::bigint + %s)::text
            RETURNING value
        """, (key, str(by), by))
        new_value = cur.fetchone()[0]
        conn.commit()
        return int(new_value)
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def init_channel_settings():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_channels (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL UNIQUE,
                channel_name TEXT,
                enabled BOOLEAN DEFAULT TRUE,
                added_by BIGINT,
                added_at BIGINT
            )
        """)
        conn.commit()
        print("[init] ✅ Настройки каналов рассылки инициализированы")
    except Exception as e:
        print(f"[init_channel_settings] Ошибка: {e}")
        conn.rollback()
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def get_broadcast_channels():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT channel_id, channel_name FROM broadcast_channels WHERE enabled = TRUE")
        return cur.fetchall()
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def add_broadcast_channel(channel_id, channel_name, added_by):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO broadcast_channels (channel_id, channel_name, enabled, added_by, added_at)
            VALUES (%s, %s, TRUE, %s, %s)
            ON CONFLICT (channel_id) DO UPDATE SET channel_name = %s, enabled = TRUE
        """, (channel_id, channel_name, added_by, int(time.time()), channel_name))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"[add_broadcast_channel] Ошибка: {e}")
        return False
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def get_subscription_link(user_id):
    if is_blocked(user_id):
        return None
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT token, is_frozen FROM users WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        if not result:
            return None
        
        token, is_frozen = result
        
        if is_frozen:
            return None
        
        if token:
            base_url = get_bot_base_url()
            return f"{base_url}/sub/{token}"
        
        token = generate_subscription_token()
        cur.execute("""
            UPDATE users SET token = %s 
            WHERE user_id = %s AND token IS NULL
            RETURNING token
        """, (token, user_id))
        result = cur.fetchone()
        if not result:
            cur.execute("SELECT token FROM users WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            token = result[0] if result else token
        conn.commit()
        base_url = get_bot_base_url()
        return f"{base_url}/sub/{token}"
    except Exception as e:
        print(f"[get_subscription_link] Ошибка: {e}")
        return None
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def is_blocked(user_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT is_blocked FROM users WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            return result[0] == 1 if result else False
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
    except:
        return False

def get_user_display_name(user_id):
    return get_user_display_name_cached(user_id)

def update_user_username(user_id, username):
    if not username:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE users 
                SET username = %s, telegram_id = %s 
                WHERE user_id = %s
            """, (username, user_id, user_id))
            conn.commit()
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
    except Exception as e:
        print(f"[update_user_username] Ошибка: {e}")

def _find_user_by_username_in_db(username):
    try:
        username_lower = username.lower().lstrip('@')
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM users WHERE LOWER(username) = %s", (username_lower,))
            result = cur.fetchone()
            if result:
                return result[0]
            cur.execute("SELECT user_id FROM users WHERE telegram_id = %s", (username_lower,))
            result = cur.fetchone()
            if result:
                return result[0]
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
    except Exception as e:
        print(f"[_find_user_by_username_in_db] Ошибка: {e}")
    return None

def get_user_id_from_input(user_input):
    user_input = user_input.strip()
    
    tg_match = re.search(r'tg://user\?id=(\d+)', user_input)
    if tg_match:
        try:
            user_id = int(tg_match.group(1))
            if user_id <= 0:
                return None
            return user_id
        except:
            return None
    
    tme_match = re.search(r't\.me/([a-zA-Z0-9_]+)', user_input)
    if tme_match:
        username = tme_match.group(1)
        uid = _find_user_by_username_in_db(username)
        if uid:
            return uid
        try:
            chat = bot.get_chat(f"@{username}")
            return chat.id
        except:
            return None
    
    if user_input.startswith('@'):
        username = user_input.lstrip('@')
        uid = _find_user_by_username_in_db(username)
        if uid:
            return uid
        try:
            chat = bot.get_chat(user_input)
            return chat.id
        except:
            return None
    
    try:
        user_id = int(user_input)
        if user_id <= 0:
            return None
        return user_id
    except:
        return None

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        types.KeyboardButton("👤 Личный кабинет"),
        types.KeyboardButton("📡 Моя подписка")
    )
    kb.row(
        types.KeyboardButton("👥 Рефералы"),
        types.KeyboardButton("🏆 Топ рефералов")
    )
    kb.row(
        types.KeyboardButton("ℹ️ Стаж бота"),
        types.KeyboardButton("📋 Правила")
    )
    kb.row(
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

def _format_duration(seconds):
    seconds = max(0, int(seconds))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours or days:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return ' '.join(parts)

def get_bot_stats():
    ensure_bot_start_time()
    start_time = int(get_setting('bot_start_time', str(int(time.time()))))
    uptime_seconds = int(time.time()) - start_time
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    return {
        'uptime_text': _format_duration(uptime_seconds),
        'total_users': total_users,
        'current_keys': len(get_keys_from_db()),
    }

CAPTCHA_TIMEOUT = 300
SUBSCRIBE_MONITOR = {'timestamps': [], 'blocked_until': 0}
SUBSCRIBE_LIMIT = 100
SUBSCRIBE_BAN_TIME = 3600

def check_subscribe_rate():
    with _subscribe_monitor_lock:
        current_time = int(time.time())
        SUBSCRIBE_MONITOR['timestamps'] = [t for t in SUBSCRIBE_MONITOR['timestamps'] if current_time - t < 60]
        count = len(SUBSCRIBE_MONITOR['timestamps'])
        if current_time < SUBSCRIBE_MONITOR['blocked_until']:
            remaining = SUBSCRIBE_MONITOR['blocked_until'] - current_time
            return False, f"⏳ Подписки заблокированы. Осталось {remaining//60} мин."
        if count > SUBSCRIBE_LIMIT:
            SUBSCRIBE_MONITOR['blocked_until'] = current_time + SUBSCRIBE_BAN_TIME
            return False, "⚠️ Слишком много подписок. Попробуйте через час."
        return True, "OK"

def add_subscribe_record(user_id):
    with _subscribe_monitor_lock:
        SUBSCRIBE_MONITOR['timestamps'].append(int(time.time()))

def process_referral(referrer_id, referred_id):
    if referrer_id == referred_id:
        return False, "Нельзя пригласить самого себя"
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM users WHERE user_id = %s FOR UPDATE", (referrer_id,))
        referrer_exists = cur.fetchone()
        if not referrer_exists:
            return False, "Реферер не найден"
        
        cur.execute("SELECT user_id, is_blocked FROM users WHERE user_id = %s FOR UPDATE", (referred_id,))
        referred = cur.fetchone()
        if not referred:
            return False, "Реферал не зарегистрирован в боте"
        if referred[1] == 1:
            return False, "Реферал заблокирован"
        
        referrer_subscribed = is_subscribed(referrer_id)
        referred_subscribed = is_subscribed(referred_id)
        
        if not referred_subscribed:
            return False, "Реферал не подписан на канал"
        
        today_start = int(time.time()) - 24 * 60 * 60
        cur.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = %s AND reward_date > %s",
            (referrer_id, today_start)
        )
        count = cur.fetchone()[0]
        if count >= 10:
            return False, "Лимит рефералов (10 в день) превышен"
        
        current_time = int(time.time())
        try:
            cur.execute("""
                INSERT INTO referrals (referrer_id, referred_id, reward_date, rewarded, referrer_subscribed, referred_subscribed) 
                VALUES (%s, %s, %s, 0, %s, %s)
            """, (referrer_id, referred_id, current_time, 1 if referrer_subscribed else 0, 1))
            conn.commit()
        except Exception as e:
            conn.rollback()
            if 'unique' in str(e).lower():
                return False, "Этот пользователь уже был приглашен"
            raise
        
        if referrer_subscribed:
            cur.execute("SELECT subscription_end FROM users WHERE user_id = %s FOR UPDATE", (referrer_id,))
            ref_result = cur.fetchone()
            if ref_result:
                new_end = ref_result[0] + 3 * 24 * 60 * 60
                cur.execute("UPDATE users SET subscription_end = %s, notified_3days = 0 WHERE user_id = %s", 
                           (new_end, referrer_id))
                cur.execute(
                    "UPDATE referrals SET rewarded = 1 WHERE referrer_id = %s AND referred_id = %s",
                    (referrer_id, referred_id)
                )
                conn.commit()
                try:
                    bot.send_message(referrer_id, "🎉 Вам начислено +3 дня за нового реферала!")
                except:
                    pass
                return True, "Реферал добавлен, начислено +3 дня"
        
        conn.commit()
        return True, "Реферал сохранен"
    except Exception as e:
        print(f"[process_referral] Ошибка: {e}")
        try:
            conn.rollback()
        except:
            pass
        return False, f"Ошибка: {e}"
    finally:
        try:
            if cur:
                cur.close()
        except:
            pass
        if conn:
            return_db_connection(conn)

def get_admin_permissions(user_id):
    if user_id == ADMIN_ID:
        return ROLE_PRESETS['owner']['permissions'].copy()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT permissions FROM admins WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        if result and result[0]:
            try:
                return json.loads(result[0])
            except:
                pass
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    return {p: False for p in PERMISSIONS}

def has_permission(user_id, permission):
    if user_id == ADMIN_ID:
        return True
    perms = get_admin_permissions(user_id)
    return perms.get(permission, False)

def get_admin_role_name(user_id):
    if user_id == ADMIN_ID:
        return "👑 Владелец"
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT role FROM admins WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        if result:
            role = result[0]
            if role == 'owner': return "👑 Владелец"
            elif role == 'senior': return "⭐ Старший админ"
            elif role == 'junior': return "🔹 Младший админ"
            elif role == 'support': return "🟢 Поддержка"
        return "❌ Не админ"
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def is_admin(user_id):
    if user_id == ADMIN_ID:
        return True
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM admins WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            return result is not None
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
    except:
        return False

def build_user_list_keyboard(users, page, filter_type='all'):
    kb = types.InlineKeyboardMarkup(row_width=2)
    per_page = 5
    start = page * per_page
    end = start + per_page
    current_time = int(time.time())

    page_users = users[start:end]
    user_data = {}
    if page_users:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            placeholders = ','.join(['%s'] * len(page_users))
            query = """
                SELECT user_id, COALESCE(subscription_end, 0), COALESCE(is_blocked, 0) 
                FROM users WHERE user_id IN ({})
            """.format(placeholders)
            cur.execute(query, tuple(page_users))
            user_data = {row[0]: row for row in cur.fetchall()}
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)

    for uid in page_users:
        row = user_data.get(uid)
        if row:
            _, sub_end, blk = row
            if blk == 1:
                icon = "🚫"
            elif sub_end > 0 and sub_end > current_time:
                icon = "🟢"
            else:
                icon = "🔴"
        else:
            icon = "❓"
        admin_icon = "👑 " if is_admin(uid) else ""
        name = get_user_display_name_cached(uid)
        display = f"{icon} {admin_icon}{name}"[:40]
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
        types.InlineKeyboardButton("📋 Все", callback_data="filter_all")
    )
    kb.row(
        types.InlineKeyboardButton("🔙 Назад в админ-панель", callback_data="admin_back_panel"),
        types.InlineKeyboardButton("❌ Закрыть", callback_data="close_manage")
    )
    return kb

def admin_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_announce"),
        types.InlineKeyboardButton("👥 Управление пользователями", callback_data="admin_manage_users")
    )
    kb.add(
        types.InlineKeyboardButton("🔑 Управление ключами", callback_data="admin_keys")
    )
    kb.add(
        types.InlineKeyboardButton("📋 Логи админов", callback_data="admin_view_logs")
    )
    kb.add(
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="admin_back")
    )
    return kb

def show_keys_menu(user_id, chat_id, message_id):
    keys = get_keys_from_db()
    sub_keys = get_subscription_keys_from_db()
    total_issued = int(get_setting('total_keys_issued', '0'))
    
    text = (
        f"🔑 *Управление ключами*\n\n"
        f"📋 *Подписка /sub:* {len(sub_keys)} ключей\n"
        f"🗑️ Выдано ключей: {total_issued}\n\n"
        f"Выберите действие:"
    )
    
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📥 Ключи подписки", callback_data="admin_sub_keys_load"),
        types.InlineKeyboardButton("🧹 Очистить нерабочие", callback_data="admin_keys_clean_dead")
    )
    kb.add(
        types.InlineKeyboardButton("🗑️ Очистить ВСЕ", callback_data="admin_keys_clear_all")
    )
    kb.add(
        types.InlineKeyboardButton("🔙 Назад", callback_data="admin_back_panel")
    )
    
    sent = False
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="Markdown", reply_markup=kb)
            sent = True
        except Exception as e:
            print(f"[show_keys_menu] edit failed: {e}")
    if not sent:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)

def _show_admin_logs(call):
    user_id = call.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT admin_name, action, target_name, details, created_at
            FROM admin_logs
            ORDER BY created_at DESC
            LIMIT 20
        """)
        logs = cur.fetchall()
    except Exception as e:
        print(f"[logs] Ошибка: {e}")
        bot.send_message(user_id, "❌ Ошибка получения логов")
        return
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    if not logs:
        text = "📋 *Логи админов*\n\nПусто"
    else:
        text = "📋 *Последние 20 действий:*\n\n"
        for admin_name, action, target_name, details, created_at in logs:
            time_str = datetime.fromtimestamp(created_at).strftime("%d.%m %H:%M")
            target = f" → {target_name}" if target_name else ""
            text += f"🕐 {time_str} | *{admin_name}* {action}{target}\n"
            if details:
                text += f"  📎 {details}\n"
            text += "\n"
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔄 Обновить", callback_data="admin_view_logs"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="admin_back_panel")
    )
    
    try:
        if len(text) > 4000:
            text = text[:3950] + "\n…"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)
    except:
        bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=kb)

def admin_announce_text(message):
    user_id = message.from_user.id
    with _cache_lock:
        if user_id not in announce_data:
            return
        data = announce_data.pop(user_id, {})
    
    announce_type = data.get('type', 'dm')
    text = message.text
    caption = message.caption or ''
    
    if announce_type == 'dm':
        if not text and not message.photo and not message.video and not message.document:
            bot.reply_to(message, "❌ Отправьте текст или медиа.")
            return
        
        def do_announce():
            conn = get_db_connection()
            cur = conn.cursor()
            try:
                cur.execute("SELECT user_id FROM users")
                users = cur.fetchall()
            finally:
                try:
                    cur.close()
                except:
                    pass
                return_db_connection(conn)
            sent = 0
            for (uid,) in users:
                try:
                    if is_blocked(uid):
                        continue
                    if message.photo:
                        bot.send_photo(uid, message.photo[-1].file_id, caption=caption)
                    elif message.video:
                        bot.send_video(uid, message.video.file_id, caption=caption)
                    elif message.document:
                        bot.send_document(uid, message.document.file_id, caption=caption)
                    else:
                        bot.send_message(uid, text)
                    sent += 1
                    time.sleep(0.05)
                except:
                    pass
            log_admin_action(user_id, f"Сделал рассылку в ЛС", details=f"Отправлено: {sent} пользователей")
            try:
                bot.send_message(user_id, f"✅ Отправлено {sent} пользователям")
            except:
                pass
        
        bot.reply_to(message, "⏳ Рассылка запущена в фоне...")
        t = Thread(target=do_announce, daemon=True)
        t.start()
        
    elif announce_type == 'channel':
        channel_id = data.get('channel_id')
        try:
            if message.photo:
                bot.send_photo(channel_id, message.photo[-1].file_id, caption=caption)
            elif message.video:
                bot.send_video(channel_id, message.video.file_id, caption=caption)
            elif message.document:
                bot.send_document(channel_id, message.document.file_id, caption=caption)
            else:
                bot.send_message(channel_id, text)
            log_admin_action(user_id, f"Отправил объявление в канал {channel_id}")
            bot.reply_to(message, "✅ Отправлено")
            try:
                chat_info = bot.get_chat(channel_id)
                ch_name = chat_info.title or str(channel_id)
                add_broadcast_channel(channel_id, ch_name, user_id)
            except:
                pass
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
            
    elif announce_type == 'all_channels':
        broadcast = get_broadcast_channels()
        all_targets = [(ch_id,) for ch_id, _ in broadcast]
        
        if not all_targets:
            bot.reply_to(message, "❌ Нет каналов для рассылки.")
            return
        sent = 0
        for (ch_id,) in all_targets:
            try:
                if message.photo:
                    bot.send_photo(ch_id, message.photo[-1].file_id, caption=caption)
                elif message.video:
                    bot.send_video(ch_id, message.video.file_id, caption=caption)
                elif message.document:
                    bot.send_document(ch_id, message.document.file_id, caption=caption)
                else:
                    bot.send_message(ch_id, text)
                sent += 1
                time.sleep(0.3)
            except Exception as e:
                print(f"[announce_all] Ошибка отправки в {ch_id}: {e}")
        log_admin_action(user_id, "Рассылка во все каналы", details=f"Отправлено: {sent}")
        bot.reply_to(message, f"✅ Отправлено в {sent} каналов")

def is_user_blocked_bot(user_id):
    current_time = int(time.time())
    
    with _user_blocked_cache_lock:
        cached = _user_blocked_cache.get(user_id, {})
        if cached.get('timestamp', 0) > current_time - USER_BLOCKED_CACHE_TTL:
            return cached.get('blocked', False)
    
    try:
        bot.get_chat(user_id)
        blocked = False
    except Exception as e:
        if 'blocked' in str(e).lower() or 'deactivated' in str(e).lower() or 'user not found' in str(e).lower():
            blocked = True
        else:
            blocked = False
    
    with _user_blocked_cache_lock:
        _user_blocked_cache[user_id] = {
            'blocked': blocked,
            'timestamp': current_time
        }
    
    return blocked

def clear_user_cache(user_id):
    """Очищает кеш пользователя после изменений"""
    with _user_name_cache_lock:
        if user_id in _user_name_cache:
            del _user_name_cache[user_id]
    with _user_blocked_cache_lock:
        if user_id in _user_blocked_cache:
            del _user_blocked_cache[user_id]

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ МЕНЮ ====================

@bot.message_handler(func=lambda m: m.text == "👤 Личный кабинет")
def cabinet(message):
    update_activity()
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    current_time = int(time.time())
    
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT COALESCE(subscription_end, 0), 
                   COALESCE(is_frozen, 0), 
                   COALESCE(frozen_days_left, 0)
            FROM users WHERE user_id = %s
        """, (user_id,))
        result = cur.fetchone()
        if not result:
            bot.reply_to(message, "❌ Используйте /start")
            return
        
        subscription_end, is_frozen, frozen_days_left = result
        
        if is_frozen == 1:
            status = "❄️ Заморожена"
            days_left = frozen_days_left
            time_left = f"{days_left} дн"
            expire_date = "Заморожена"
        elif subscription_end > 0 and subscription_end > current_time:
            status = "✅ Активна"
            days_left = (subscription_end - current_time) // (24 * 60 * 60)
            hours_left = ((subscription_end - current_time) // 3600) % 24
            time_left = f"{days_left} дн {hours_left} ч"
            expire_date = datetime.fromtimestamp(subscription_end).strftime("%d.%m.%Y в %H:%M")
        else:
            status = "❌ Не активна"
            time_left = "Закончилась"
            expire_date = "Закончилась"
        
        text = (
            f"👤 *Личный кабинет*\n\n"
            f"🆔 ID: `{user_id}`\n"
            f"📊 Статус: {status}\n"
            f"📅 Подписка до: `{expire_date}`\n"
            f"⏳ Осталось: `{time_left}`"
        )

        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="refresh_cabinet"))
        
        bot.reply_to(message, text, parse_mode="Markdown", reply_markup=kb)
        
    except Exception as e:
        print(f"[cabinet] Ошибка: {e}")
        traceback.print_exc()
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

@bot.callback_query_handler(func=lambda call: call.data == "refresh_cabinet")
def callback_refresh_cabinet(call):
    user_id = call.from_user.id
    current_time = int(time.time())
    
    clear_user_cache(user_id)
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT COALESCE(subscription_end, 0), 
                   COALESCE(is_frozen, 0), 
                   COALESCE(frozen_days_left, 0)
            FROM users WHERE user_id = %s
        """, (user_id,))
        result = cur.fetchone()
        if not result:
            bot.answer_callback_query(call.id, "❌ Ошибка")
            return
        
        subscription_end, is_frozen, frozen_days_left = result
        
        if is_frozen == 1:
            status = "❄️ Заморожена"
            days_left = frozen_days_left
            time_left = f"{days_left} дн"
            expire_date = "Заморожена"
        elif subscription_end > 0 and subscription_end > current_time:
            status = "✅ Активна"
            days_left = (subscription_end - current_time) // (24 * 60 * 60)
            hours_left = ((subscription_end - current_time) // 3600) % 24
            time_left = f"{days_left} дн {hours_left} ч"
            expire_date = datetime.fromtimestamp(subscription_end).strftime("%d.%m.%Y в %H:%M")
        else:
            status = "❌ Не активна"
            time_left = "Закончилась"
            expire_date = "Закончилась"
        
        text = (
            f"👤 *Личный кабинет*\n\n"
            f"🆔 ID: `{user_id}`\n"
            f"📊 Статус: {status}\n"
            f"📅 Подписка до: `{expire_date}`\n"
            f"⏳ Осталось: `{time_left}`"
        )

        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="refresh_cabinet"))
        
        try:
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=kb
            )
        except:
            bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=kb)
        
        bot.answer_callback_query(call.id, "✅ Обновлено!")
        
    except Exception as e:
        print(f"[refresh_cabinet] Ошибка: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

@bot.message_handler(func=lambda m: m.text == "📡 Моя подписка")
def my_subscription(message):
    update_activity()
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    current_time = int(time.time())
    
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    if not is_subscribed(user_id):
        bot.reply_to(message, "⚠️ Подпишитесь на канал.", reply_markup=subscribe_button())
        return
    
    clear_user_cache(user_id)
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT COALESCE(subscription_end, 0), 
                   COALESCE(is_frozen, 0), 
                   COALESCE(frozen_days_left, 0)
            FROM users WHERE user_id = %s
        """, (user_id,))
        result = cur.fetchone()
        if not result:
            bot.reply_to(message, "❌ Не зарегистрированы. /start")
            return
        
        subscription_end, is_frozen, frozen_days_left = result
        
        if is_frozen == 1:
            text = (
                f"📡 *Моя подписка*\n\n"
                f"❄️ *Подписка заморожена*\n\n"
                f"⏳ Сохранено дней: `{frozen_days_left}`\n\n"
                f"Нажмите кнопку ниже чтобы разморозить.\n"
                f"Будет сгенерирован новый токен подписки.\n\n"
                f"💬 Поддержка: {SUPPORT}"
            )
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(
                "🔥 Разморозить подписку",
                callback_data="unfreeze_sub"
            ))
            bot.reply_to(message, text, parse_mode="Markdown", reply_markup=kb)
            return
        
        link = get_subscription_link(user_id) if subscription_end > 0 and subscription_end > current_time else None
        days_left = (subscription_end - current_time) // (24 * 60 * 60) if subscription_end > 0 and subscription_end > current_time else 0

        if subscription_end > 0 and subscription_end > current_time:
            status_text = f"✅ Активна\n⏳ Осталось: `{days_left}` дн."
        else:
            status_text = "❌ Не активна\n\nДля продления обратитесь к администратору:"

        text = (
            f"📡 *Моя подписка*\n\n"
            f"📊 Статус: {status_text}\n\n"
        )
        
        if link:
            text += f"🔗 *Ссылка для импорта:*\n`{link}`\n\n"
        
        text += f"💬 Поддержка: {SUPPORT}"

        kb = types.InlineKeyboardMarkup(row_width=2)
        
        if link:
            kb.add(types.InlineKeyboardButton("📋 Копировать ссылку", callback_data=f"copy_link_{user_id}"))
            kb.row(
                types.InlineKeyboardButton("🍎 Incy iOS", url="https://apps.apple.com/ru/app/incy/id6756943388"),
                types.InlineKeyboardButton("🤖 Incy Android", url="https://play.google.com/store/apps/details?id=llc.itdev.incy")
            )
            if days_left > 0:
                kb.add(types.InlineKeyboardButton(
                    f"❄️ Заморозить ({days_left} дн.)",
                    callback_data="freeze_sub"
                ))
        else:
            kb.add(types.InlineKeyboardButton(
                "💬 Связаться с поддержкой",
                url=f"https://t.me/{SUPPORT.lstrip('@')}"
            ))
            kb.add(types.InlineKeyboardButton(
                "🔄 Обновить статус",
                callback_data="refresh_cabinet"
            ))
        
        bot.reply_to(message, text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        print(f"[my_subscription] Ошибка: {e}")
        traceback.print_exc()
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

@bot.callback_query_handler(func=lambda call: call.data == "freeze_sub")
def callback_freeze_sub(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT subscription_end FROM users WHERE user_id = %s",
            (user_id,)
        )
        result = cur.fetchone()
        if not result:
            return
        
        current_time = int(time.time())
        sub_end = result[0]
        days_left = max(0, (sub_end - current_time) // (24 * 60 * 60))
        
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    text = (
        f"❄️ *Заморозка подписки*\n\n"
        f"⚠️ *Внимание!*\n\n"
        f"• Текущий токен подписки будет *удалён*\n"
        f"• Сохранится: `{days_left}` дней\n"
        f"• При разморозке генерируется *новый токен*\n"
        f"• Старая ссылка перестанет работать\n\n"
        f"Вы уверены?"
    )
    
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Да, заморозить", callback_data="freeze_confirm"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="freeze_cancel")
    )
    
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            parse_mode="Markdown", reply_markup=kb
        )
    except:
        bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "freeze_confirm")
def callback_freeze_confirm(call):
    user_id = call.from_user.id
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT subscription_end FROM users WHERE user_id = %s",
            (user_id,)
        )
        result = cur.fetchone()
        if not result:
            bot.answer_callback_query(call.id, "❌ Ошибка")
            return
        
        current_time = int(time.time())
        sub_end = result[0]
        days_left = max(0, (sub_end - current_time) // (24 * 60 * 60))
        
        cur.execute("""
            UPDATE users SET 
                is_frozen = 1,
                frozen_days_left = %s,
                frozen_at = %s,
                token = NULL,
                subscription_end = 0
            WHERE user_id = %s
        """, (days_left, int(time.time()), user_id))
        conn.commit()
        
        clear_user_cache(user_id)
        
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    bot.answer_callback_query(call.id, "❄️ Подписка заморожена!")
    
    try:
        bot.edit_message_text(
            f"❄️ *Подписка заморожена*\n\n⏳ Сохранено: `{days_left}` дней\n\nДля разморозки нажмите кнопку в разделе 📡 *Моя подписка*",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown"
        )
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "freeze_cancel")
def callback_freeze_cancel(call):
    bot.answer_callback_query(call.id, "❌ Отменено")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "unfreeze_sub")
def callback_unfreeze_sub(call):
    user_id = call.from_user.id
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT frozen_days_left FROM users WHERE user_id = %s",
            (user_id,)
        )
        result = cur.fetchone()
        if not result:
            bot.answer_callback_query(call.id, "❌ Ошибка")
            return
        
        frozen_days = result[0] or 0
        current_time = int(time.time())
        new_sub_end = current_time + frozen_days * 24 * 60 * 60
        new_token = generate_subscription_token()
        
        cur.execute("""
            UPDATE users SET
                is_frozen = 0,
                frozen_days_left = 0,
                frozen_at = 0,
                subscription_end = %s,
                token = %s,
                notified_3days = 0
            WHERE user_id = %s
        """, (new_sub_end, new_token, user_id))
        conn.commit()
        
        clear_user_cache(user_id)
        
        new_link = f"{get_bot_base_url()}/sub/{new_token}"
        
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    bot.answer_callback_query(call.id, "🔥 Подписка разморожена!")
    
    text = (
        f"🔥 *Подписка разморожена!*\n\n"
        f"✅ Активна ещё: `{frozen_days}` дней\n"
        f"🔗 Новая ссылка:\n"
        f"`{new_link}`\n\n"
        f"⚠️ Старая ссылка больше не работает!\n"
        f"Обновите подписку в клиенте."
    )
    
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            parse_mode="Markdown"
        )
    except:
        bot.send_message(user_id, text, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('copy_link_'))
def callback_copy_link(call):
    user_id = call.from_user.id
    target_id = int(call.data.split('_')[2])

    if user_id != target_id:
        bot.answer_callback_query(call.id, "❌ Это не ваша ссылка.")
        return

    link = get_subscription_link(user_id)
    if not link:
        bot.answer_callback_query(call.id, "❌ Подписка заморожена или недоступна.")
        return

    bot.send_message(
        user_id,
        f"📋 *Ссылка для импорта:*\n\n`{link}`\n\nНажмите на сообщение и скопируйте текст.",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, "✅ Ссылка отправлена!")

@bot.message_handler(func=lambda m: m.text == "👥 Рефералы")
def referrals(message):
    update_activity()
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        if not cur.fetchone():
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
        bot_username = get_bot_username()
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        text = f"👥 *Рефералы*\n\n📊 Всего: {total}\n📅 Сегодня: {today} / 10\n\n🔗 Ссылка: `{ref_link}`\n\n📌 За каждого друга +3 дня."
        bot.reply_to(message, text, parse_mode="Markdown")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

@bot.message_handler(func=lambda m: m.text == "🏆 Топ рефералов")
def top_referrals(message):
    update_activity()
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT referrer_id, COUNT(*) FROM referrals GROUP BY referrer_id ORDER BY COUNT(*) DESC LIMIT 10")
        rows = cur.fetchall()
        if not rows:
            bot.reply_to(message, "📭 Нет рефералов.")
            return
        text = "🏆 *Топ рефералов:*\n\n"
        medals = ['🥇', '🥈', '🥉']
        for i, (ref_id, count) in enumerate(rows):
            name = get_user_display_name_cached(ref_id)
            icon = medals[i] if i < 3 else f"{i+1}."
            text += f"{icon} {name} — {count} реф.\n"
        bot.reply_to(message, text, parse_mode="Markdown")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

@bot.message_handler(func=lambda m: m.text == "ℹ️ Стаж бота")
def bot_stats_command(message):
    update_activity()
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    stats = get_bot_stats()
    text = (
        f"📊 *Статистика*\n\n"
        f"⏳ Стаж: {stats['uptime_text']}\n"
        f"👥 Пользователей: {stats['total_users']}\n"
        f"📦 Ключей: {stats['current_keys']}"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "📋 Правила")
def rules(message):
    update_activity()
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    
    text = (
        "⚠️ *Правила сети:*\n\n"
        "🛑 *Запрещено:*\n"
        "• Использовать торрент и P2P\n"
        "От этого могут умереть сервера из-за перегрузки процессора.\n\n"
        "🏦 *Ограничения:*\n"
        "• Не использовать для банков\n"
        "Запрещено для доступа к банковским приложениям и финансовым операциям.\n\n"
        f"💬 Поддержка: {SUPPORT}"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "❓ Поддержка")
def support(message):
    bot.reply_to(message, f"💬 Поддержка: {SUPPORT}")

@bot.message_handler(commands=['start'])
def cmd_start(message):
    update_activity()
    if message.chat.type != 'private':
        bot.reply_to(message, "⚠️ Бот работает только в личных сообщениях.")
        return

    user_id = message.from_user.id
    current_time = int(time.time())

    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        existing_user = cur.fetchone()
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

    if existing_user:
        if not is_subscribed(user_id):
            bot.reply_to(message, "⚠️ Подпишитесь на канал, чтобы пользоваться ботом.", reply_markup=subscribe_button())
            return
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT last_activity FROM users WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            if result:
                last_activity = result[0] or 0
                days_since_last = (current_time - last_activity) // (24 * 60 * 60)
                welcome_text = "👋 С возвращением!" if days_since_last >= 3 else "👋 Добро пожаловать!"
                cur.execute("UPDATE users SET last_activity = %s WHERE user_id = %s", (current_time, user_id))
                conn.commit()
                bot.reply_to(message, welcome_text)
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
        bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())
        return

    with _captcha_lock:
        if user_id in captcha_sessions:
            session = captcha_sessions[user_id]
            if int(time.time()) - session['timestamp'] < CAPTCHA_TIMEOUT:
                bot.reply_to(
                    message,
                    "⏳ Вы уже проходите капчу. Нажмите кнопку ниже.",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("✅ Я НЕ РОБОТ", callback_data=f"captcha_verify_{user_id}")
                    )
                )
                return
            else:
                del captcha_sessions[user_id]

    ok, msg = check_subscribe_rate()
    if not ok:
        bot.reply_to(message, f"⚠️ {msg}")
        return

    add_subscribe_record(user_id)

    referrer_id = None
    if message.text:
        parts = message.text.strip().split()
        if len(parts) > 1:
            for part in parts:
                if part.startswith('ref_'):
                    try:
                        ref = int(part[4:])
                        if ref != user_id:
                            referrer_id = ref
                        break
                    except ValueError:
                        continue

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Я НЕ РОБОТ", callback_data=f"captcha_verify_{user_id}"))

    msg = bot.reply_to(
        message,
        "🤖 *Пожалуйста, подтвердите, что вы не робот*\n\n"
        "Нажмите кнопку ниже для проверки.\n"
        f"⏱ У вас {CAPTCHA_TIMEOUT//60} минут.",
        parse_mode="Markdown",
        reply_markup=kb
    )

    with _captcha_lock:
        captcha_sessions[user_id] = {
            'timestamp': int(time.time()),
            'message_id': msg.message_id,
            'referrer_id': referrer_id,
            'waiting_for_sub': False
        }

@bot.callback_query_handler(func=lambda call: call.data.startswith('captcha_verify_'))
def callback_captcha_verify(call):
    user_id = int(call.data.split('_')[2])
    if call.from_user.id != user_id:
        bot.answer_callback_query(call.id, "❌ Это не ваша капча.")
        return
    
    with _captcha_lock:
        if user_id not in captcha_sessions:
            bot.answer_callback_query(call.id, "❌ Сессия истекла. Нажмите /start")
            return
        session = captcha_sessions[user_id]
        current_time = int(time.time())
        if current_time - session['timestamp'] > CAPTCHA_TIMEOUT:
            del captcha_sessions[user_id]
            bot.answer_callback_query(call.id, "⏰ Время вышло. Нажмите /start")
            return
    
    try:
        bot.delete_message(call.message.chat.id, session['message_id'])
    except:
        pass
    bot.answer_callback_query(call.id, "✅ Капча пройдена!")

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        already_registered = cur.fetchone()
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

    if already_registered:
        with _captcha_lock:
            if user_id in captcha_sessions:
                del captcha_sessions[user_id]
        bot.send_message(user_id, "👋 Вы уже зарегистрированы!")
        bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())
        return

    if is_subscribed(user_id):
        bot.send_message(user_id, "✅ Подписка подтверждена! Регистрируем вас...")
        with _captcha_lock:
            referrer_id = captcha_sessions.get(user_id, {}).get('referrer_id')
            if user_id in captcha_sessions:
                del captcha_sessions[user_id]
        _register_user(user_id, referrer_id)
    else:
        bot.send_message(
            user_id,
            "⚠️ Подпишитесь на канал, чтобы завершить регистрацию.\n\n"
            "После подписки нажмите кнопку ниже.",
            reply_markup=subscribe_button()
        )
        with _captcha_lock:
            if user_id in captcha_sessions:
                captcha_sessions[user_id]['waiting_for_sub'] = True

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def callback_check_sub(call):
    update_activity()
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
        
        with _captcha_lock:
            if user_id in captcha_sessions and captcha_sessions[user_id].get('waiting_for_sub'):
                conn = get_db_connection()
                cur = conn.cursor()
                try:
                    cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
                    already_registered = cur.fetchone()
                finally:
                    try:
                        cur.close()
                    except:
                        pass
                    return_db_connection(conn)
                
                if already_registered:
                    del captcha_sessions[user_id]
                    bot.send_message(user_id, "👋 Вы уже зарегистрированы!")
                    bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())
                    return
                
                session = captcha_sessions[user_id]
                del captcha_sessions[user_id]
                bot.send_message(user_id, "✅ Подписка подтверждена! Регистрируем вас...")
                _register_user(user_id, session.get('referrer_id'))
                return
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT referrer_id FROM referrals WHERE referred_id = %s AND rewarded = 0",
                (user_id,)
            )
            pending = cur.fetchone()
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
        if pending:
            referrer_id = pending[0]
            if is_subscribed(referrer_id):
                conn = get_db_connection()
                cur = conn.cursor()
                try:
                    cur.execute("SELECT subscription_end FROM users WHERE user_id = %s FOR UPDATE", (referrer_id,))
                    ref_result = cur.fetchone()
                    if ref_result:
                        new_end = ref_result[0] + 3 * 24 * 60 * 60
                        cur.execute("UPDATE users SET subscription_end = %s, notified_3days = 0 WHERE user_id = %s", 
                                   (new_end, referrer_id))
                        cur.execute("UPDATE referrals SET rewarded = 1 WHERE referred_id = %s", (user_id,))
                        conn.commit()
                        try:
                            bot.send_message(referrer_id, "🎉 Ваш реферал подтвердил подписку! Вам начислено +3 дня.")
                        except:
                            pass
                finally:
                    try:
                        cur.close()
                    except:
                        pass
                    return_db_connection(conn)
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
            user_exists = cur.fetchone()
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
        if not user_exists:
            _register_user(user_id, None)
        else:
            bot.send_message(user_id, "👋 Добро пожаловать!")
            bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())
    else:
        bot.answer_callback_query(call.id, "❌ Вы ещё не подписались на канал!")

def _register_user(user_id, referrer_id=None):
    current_time = int(time.time())
    registered = False
    conn = None
    cur = None
    
    for attempt in range(5):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            existing = cur.fetchone()
            
            if existing:
                registered = True
                break
            
            token = generate_subscription_token()
            sub_end = current_time + 7 * 24 * 60 * 60
            
            username = None
            try:
                chat = bot.get_chat(user_id)
                username = chat.username
            except:
                pass
            
            cur.execute("""
                INSERT INTO users (user_id, subscription_end, last_activity, is_blocked, token, username, telegram_id) 
                VALUES (%s, %s, %s, 0, %s, %s, %s)
            """, (user_id, sub_end, current_time, token, username, user_id))
            conn.commit()
            registered = True
            break
        except Exception as e:
            conn.rollback()
            if 'unique' in str(e).lower() and 'token' in str(e).lower():
                print(f"[_register_user] Конфликт токена, попытка {attempt+1}")
                continue
            print(f"[_register_user] Ошибка: {e}")
            break
        finally:
            try:
                if cur:
                    cur.close()
            except:
                pass
            if conn:
                return_db_connection(conn)
    
    if not registered:
        print(f"[_register_user] Не удалось зарегистрировать {user_id}")
        return
    
    if referrer_id:
        success, msg = process_referral(referrer_id, user_id)
        if success:
            try:
                bot.send_message(referrer_id, f"🔔 Новый реферал! Пользователь {get_user_display_name_cached(user_id)} зарегистрировался по вашей ссылке.")
            except:
                pass
    
    try:
        bot.send_message(user_id, "🎉 Добро пожаловать! Вам выдана подписка на 7 дней.")
        bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())
    except Exception as e:
        print(f"[_register_user] Ошибка отправки приветствия: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('filter_') or 
                             call.data.startswith('page_') or
                             call.data in ('back_to_list', 'close_manage'))
def callback_user_list_nav(call):
    user_id = call.from_user.id
    if not is_admin(user_id) or not has_permission(user_id, 'manage_users'):
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    
    data = call.data
    
    if data == 'close_manage':
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        return
    
    if data == 'back_to_list':
        bot.answer_callback_query(call.id)
        with _cache_lock:
            cached = manage_cache.get(user_id, {})
            users = cached.get('users', [])
            filter_type = cached.get('filter', 'all')
        if not users:
            conn = get_db_connection()
            cur = conn.cursor()
            try:
                cur.execute("SELECT user_id FROM users ORDER BY user_id")
                users = [row[0] for row in cur.fetchall()]
            finally:
                try:
                    cur.close()
                except:
                    pass
                return_db_connection(conn)
            with _cache_lock:
                manage_cache[user_id] = {
                    'users': users,
                    'filter': 'all',
                    'timestamp': int(time.time())
                }
        kb = build_user_list_keyboard(users, 0, filter_type)
        try:
            bot.edit_message_text(
                f"👥 Пользователи ({len(users)}):",
                call.message.chat.id, call.message.message_id,
                reply_markup=kb
            )
        except:
            pass
        return
    
    if data.startswith('page_'):
        parts = data.split('_')
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Ошибка формата")
            return
        try:
            page = int(parts[1])
        except ValueError:
            bot.answer_callback_query(call.id, "❌ Ошибка формата")
            return
        filter_type = parts[2] if len(parts) > 2 else 'all'
        with _cache_lock:
            cached = manage_cache.get(user_id, {})
            users = cached.get('users', [])
        if not users:
            bot.answer_callback_query(call.id, "❌ Список устарел")
            return
        kb = build_user_list_keyboard(users, page, filter_type)
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id,
                reply_markup=kb
            )
        except:
            pass
        bot.answer_callback_query(call.id)
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    current_time = int(time.time())
    try:
        if data == 'filter_active':
            cur.execute("""
                SELECT user_id FROM users 
                WHERE is_blocked = 0 AND subscription_end > %s 
                ORDER BY user_id
            """, (current_time,))
            filter_type = 'active'
        elif data == 'filter_inactive':
            cur.execute("""
                SELECT user_id FROM users 
                WHERE is_blocked = 0 AND (subscription_end IS NULL OR subscription_end <= %s)
                ORDER BY user_id
            """, (current_time,))
            filter_type = 'inactive'
        elif data == 'filter_admins':
            cur.execute("""
                SELECT u.user_id FROM users u
                INNER JOIN admins a ON u.user_id = a.user_id
                ORDER BY u.user_id
            """)
            filter_type = 'admins'
        else:
            cur.execute("SELECT user_id FROM users ORDER BY user_id")
            filter_type = 'all'
        
        users = [row[0] for row in cur.fetchall()]
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    with _cache_lock:
        manage_cache[user_id] = {
            'users': users,
            'filter': filter_type,
            'timestamp': int(time.time())
        }
    
    kb = build_user_list_keyboard(users, 0, filter_type)
    try:
        bot.edit_message_text(
            f"👥 Пользователи ({len(users)}):",
            call.message.chat.id, call.message.message_id,
            reply_markup=kb
        )
    except:
        pass
    bot.answer_callback_query(call.id)

def _refresh_user_card(call, target_id, admin_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT 
                    COALESCE(subscription_end, 0) as subscription_end,
                    COALESCE(is_blocked, 0) as is_blocked
                FROM users WHERE user_id = %s
            """, (target_id,))
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)

        if not row:
            bot.answer_callback_query(call.id, "❌ Пользователь не найден")
            return

        subscription_end, blk = row
        current_time = int(time.time())
        
        if blk == 1:
            status = "🚫 Заблокирован"
        elif subscription_end > 0 and subscription_end > current_time:
            days_left = (subscription_end - current_time) // 86400
            status = f"🟢 Активен ({days_left} дн)"
        else:
            status = "🔴 Неактивен"

        is_admin_user = is_admin(target_id)
        admin_text = "✅ Да" if is_admin_user else "❌ Нет"
        name = get_user_display_name_cached(target_id)
        
        try:
            chat = bot.get_chat(target_id)
            username = f"@{chat.username}" if chat.username else "❌ Нет юзернейма"
        except:
            username = "❌ Не найден"

        text = f"""👤 *{name}*

🆔 ID: `{target_id}`
👤 Юзернейм: {username}
📊 Статус: {status}
👑 Админ: {admin_text}"""

        kb = types.InlineKeyboardMarkup(row_width=2)
        
        if has_permission(admin_id, 'add_days') or admin_id == ADMIN_ID:
            kb.add(types.InlineKeyboardButton("✅ Выдать подписку", callback_data=f"give_sub_{target_id}"))
            kb.add(types.InlineKeyboardButton("📅 +30 дн", callback_data=f"prolong_{target_id}_30"))
        
        if has_permission(admin_id, 'remove_days') or admin_id == ADMIN_ID:
            kb.add(types.InlineKeyboardButton("📅 -30 дн", callback_data=f"remove_days_{target_id}_30"))
        
        if (has_permission(admin_id, 'add_days') or has_permission(admin_id, 'remove_days') or admin_id == ADMIN_ID):
            kb.add(types.InlineKeyboardButton("🗑️ Удалить подписку", callback_data=f"remove_sub_{target_id}"))
        
        if (has_permission(admin_id, 'block_user') or admin_id == ADMIN_ID):
            if blk == 1:
                kb.add(types.InlineKeyboardButton("🔓 Разблокировать", callback_data=f"unblock_{target_id}"))
            else:
                kb.add(types.InlineKeyboardButton("🔒 Заблокировать", callback_data=f"block_{target_id}"))
        
        if admin_id == ADMIN_ID:
            if target_id != ADMIN_ID:
                if is_admin_user:
                    kb.add(types.InlineKeyboardButton("👑 Забрать админку", callback_data=f"remove_admin_{target_id}"))
                else:
                    kb.add(types.InlineKeyboardButton("👑 Выдать админку", callback_data=f"grant_admin_{target_id}"))
        
        kb.row(
            types.InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list"),
            types.InlineKeyboardButton("❌ Закрыть", callback_data="close_manage")
        )

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=kb
        )
    except Exception as e:
        print(f"[refresh_card] Ошибка: {e}")
        traceback.print_exc()
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('user_') and len(call.data.split('_')) == 2)
def callback_user_detail(call):
    user_id = call.from_user.id
    if not is_admin(user_id) and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Нет доступа.")
        return
    
    target_id = int(call.data.split('_')[1])
    
    if not has_permission(user_id, 'manage_users') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ У вас нет прав на управление пользователями.")
        return
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (target_id,))
            exists = cur.fetchone()
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
        
        if not exists:
            bot.answer_callback_query(call.id, "❌ Пользователь не найден в базе")
            return
        
        _refresh_user_card(call, target_id, user_id)
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"[callback_user_detail] Ошибка: {e}")
        traceback.print_exc()
        bot.answer_callback_query(call.id, "❌ Ошибка открытия карточки")

@bot.callback_query_handler(func=lambda call: call.data.startswith('give_sub_'))
def callback_give_sub(call):
    if not is_admin(call.from_user.id) and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    user_id = call.from_user.id
    if not has_permission(user_id, 'add_days') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ У вас нет прав на выдачу подписки.")
        return
    target_id = int(call.data.split('_')[2])
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM users WHERE user_id = %s", (target_id,))
        result = cur.fetchone()
        if not result:
            bot.answer_callback_query(call.id, "❌ Пользователь не найден.")
            return
        current_time = int(time.time())
        new_end = current_time + 30 * 24 * 60 * 60
        cur.execute("""
            UPDATE users SET 
                subscription_end = %s, 
                notified_3days = 0, 
                notified_expired = 0 
            WHERE user_id = %s
        """, (new_end, target_id))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    log_admin_action(user_id, f"Выдал подписку {target_id}", target_id=target_id, details="30 дней")
    bot.answer_callback_query(call.id, "✅ Выдана подписка на 30 дней!")
    try:
        if target_id != user_id:
            bot.send_message(target_id, f"🎉 Администратор выдал вам подписку на 30 дней!")
    except:
        pass
    
    _refresh_user_card(call, target_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('prolong_'))
def callback_prolong(call):
    if not is_admin(call.from_user.id) and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    user_id = call.from_user.id
    if not has_permission(user_id, 'add_days') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ У вас нет прав на выдачу дней.")
        return
    parts = call.data.split('_')
    target_id = int(parts[1])
    days = int(parts[2])
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (target_id,))
        result = cur.fetchone()
        if not result:
            bot.answer_callback_query(call.id, "❌ Пользователь не найден.")
            return
        current_time = int(time.time())
        current_end = result[0] if (result[0] and result[0] > current_time) else current_time
        new_end = current_end + days * 24 * 60 * 60
        cur.execute("""
            UPDATE users SET 
                subscription_end = %s, 
                notified_3days = 0, 
                notified_expired = 0 
            WHERE user_id = %s
        """, (new_end, target_id))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
    except Exception as e:
        print(f"[prolong] Ошибка: {e}")
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")
        return
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    log_admin_action(user_id, f"Продлил подписку {target_id}", target_id=target_id, details=f"+{days} дней")
    bot.answer_callback_query(call.id, f"✅ Продлено на {days} дней!")
    
    try:
        if target_id != user_id:
            bot.send_message(target_id, f"🎉 Ваша подписка продлена на {days} дней администратором!")
    except:
        pass
    
    _refresh_user_card(call, target_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_days_'))
def callback_remove_days(call):
    if not is_admin(call.from_user.id) and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    user_id = call.from_user.id
    if not has_permission(user_id, 'remove_days') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ У вас нет прав на забирание дней.")
        return
    parts = call.data.split('_')
    target_id = int(parts[2])
    days = int(parts[3])
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (target_id,))
        result = cur.fetchone()
        if not result:
            bot.answer_callback_query(call.id, "❌ Пользователь не найден.")
            return
        current_time = int(time.time())
        current_end = result[0] if (result[0] and result[0] > current_time) else current_time
        new_end = current_end - days * 24 * 60 * 60
        if new_end < current_time:
            new_end = current_time - 1
        cur.execute("UPDATE users SET subscription_end = %s, notified_3days = 0 WHERE user_id = %s", (new_end, target_id))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    log_admin_action(user_id, f"Забрал дни у {target_id}", target_id=target_id, details=f"-{days} дней")
    bot.answer_callback_query(call.id, f"✅ Убавлено {days} дней!")
    try:
        if target_id != user_id:
            bot.send_message(target_id, f"⚠️ Администратор забрал {days} дней подписки!")
    except:
        pass
    
    _refresh_user_card(call, target_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_sub_'))
def callback_remove_sub(call):
    if not is_admin(call.from_user.id) and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    user_id = call.from_user.id
    if not has_permission(user_id, 'add_days') and not has_permission(user_id, 'remove_days') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ У вас нет прав на удаление подписки.")
        return
    target_id = int(call.data.split('_')[2])
    current_time = int(time.time())
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (current_time - 1, target_id))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    log_admin_action(user_id, f"Удалил подписку у {target_id}", target_id=target_id)
    bot.answer_callback_query(call.id, "✅ Подписка удалена!")
    try:
        if target_id != user_id:
            bot.send_message(target_id, "❌ Ваша подписка была удалена администратором.")
    except:
        pass
    
    _refresh_user_card(call, target_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('block_'))
def callback_block(call):
    if not is_admin(call.from_user.id) and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    user_id = call.from_user.id
    if not has_permission(user_id, 'block_user') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ У вас нет прав на блокировку.")
        return
    target_id = int(call.data.split('_')[1])
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET is_blocked = 1 WHERE user_id = %s", (target_id,))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    log_admin_action(user_id, f"Заблокировал {target_id}", target_id=target_id)
    bot.answer_callback_query(call.id, "✅ Пользователь заблокирован!")
    try:
        if target_id != user_id:
            bot.send_message(target_id, f"🚫 Вы заблокированы администратором.\n\nОбратитесь в поддержку: {SUPPORT}")
    except:
        pass
    
    with _user_blocked_cache_lock:
        if target_id in _user_blocked_cache:
            del _user_blocked_cache[target_id]
    
    _refresh_user_card(call, target_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('unblock_'))
def callback_unblock(call):
    if not is_admin(call.from_user.id) and call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    user_id = call.from_user.id
    if not has_permission(user_id, 'unblock_user') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ У вас нет прав на разблокировку.")
        return
    target_id = int(call.data.split('_')[1])
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET is_blocked = 0 WHERE user_id = %s", (target_id,))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    log_admin_action(user_id, f"Разблокировал {target_id}", target_id=target_id)
    bot.answer_callback_query(call.id, "✅ Пользователь разблокирован!")
    try:
        if target_id != user_id:
            bot.send_message(target_id, "✅ Вы разблокированы! Теперь вы можете пользоваться ботом.")
    except:
        pass
    
    with _user_blocked_cache_lock:
        if target_id in _user_blocked_cache:
            del _user_blocked_cache[target_id]
    
    _refresh_user_card(call, target_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('grant_admin_'))
def callback_grant_admin(call):
    user_id = call.from_user.id
    
    if user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Только владелец может выдавать админку.")
        return
    
    target_id = int(call.data.split('_')[2])
    
    if target_id == ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Это владелец бота.")
        return
    
    if is_admin(target_id):
        bot.answer_callback_query(call.id, "❌ Пользователь уже является админом.")
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id FROM users WHERE user_id = %s", (target_id,))
        user_exists = cur.fetchone()
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    if not user_exists:
        bot.answer_callback_query(call.id, "❌ Пользователь не зарегистрирован в боте.")
        return
    
    role = 'junior'
    perms = ROLE_PRESETS[role]['permissions'].copy()
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO admins (user_id, role, permissions, added_by, added_at) 
            VALUES (%s, %s, %s, %s, %s)
        """, (target_id, role, json.dumps(perms), user_id, int(time.time())))
        conn.commit()
    except Exception as e:
        conn.rollback()
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")
        return
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    name = get_user_display_name_cached(target_id)
    log_admin_action(user_id, f"Назначил админом {target_id}", target_id=target_id, details=f"Роль: {role}")
    bot.answer_callback_query(call.id, f"✅ {name} назначен админом!")
    
    try:
        bot.send_message(target_id, "👑 Вам назначена роль администратора!\n\nТеперь вы имеете доступ к админ-панели (/admin)")
    except:
        pass
    
    _refresh_user_card(call, target_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_admin_'))
def callback_remove_admin(call):
    user_id = call.from_user.id
    
    if user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Только владелец может забирать админку.")
        return
    
    target_id = int(call.data.split('_')[2])
    
    if target_id == ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Нельзя удалить владельца.")
        return
    
    if not is_admin(target_id):
        bot.answer_callback_query(call.id, "❌ Пользователь не является админом.")
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM admins WHERE user_id = %s", (target_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")
        return
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    name = get_user_display_name_cached(target_id)
    log_admin_action(user_id, f"Удалил админа {target_id}", target_id=target_id)
    bot.answer_callback_query(call.id, f"✅ У {name} отозваны права администратора!")
    
    try:
        bot.send_message(target_id, "❌ Ваши права администратора были отозваны.")
    except:
        pass
    
    _refresh_user_card(call, target_id, user_id)

# ==================== ADMIN CALLBACK ====================

@bot.callback_query_handler(func=lambda call: (
    call.data.startswith('admin_') or 
    call.data.startswith('announce_') or
    call.data.startswith('broadcast_') or
    call.data.startswith('user_') or
    call.data.startswith('filter_') or
    call.data.startswith('page_') or
    call.data.startswith('give_sub_') or
    call.data.startswith('prolong_') or
    call.data.startswith('remove_days_') or
    call.data.startswith('remove_sub_') or
    call.data.startswith('block_') or
    call.data.startswith('unblock_') or
    call.data.startswith('copy_link_') or
    call.data.startswith('grant_admin_') or
    call.data.startswith('remove_admin_') or
    call.data == 'admin_back_panel' or
    call.data == 'admin_back' or
    call.data == 'back_to_list' or
    call.data == 'close_manage'
))
def admin_callback(call):
    user_id = call.from_user.id
    data = call.data
    
    if not is_admin(user_id) and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return

    if data.startswith("user_") and len(data.split('_')) == 2:
        callback_user_detail(call)
        return

    if data.startswith('grant_admin_'):
        callback_grant_admin(call)
        return
    
    if data.startswith('remove_admin_'):
        callback_remove_admin(call)
        return

    if data.startswith("filter_") or data.startswith("page_") or data in ('back_to_list', 'close_manage'):
        callback_user_list_nav(call)
        return

    if data.startswith(('give_sub_', 'prolong_', 'remove_days_', 'remove_sub_', 'block_', 'unblock_')):
        if data.startswith('give_sub_'):
            callback_give_sub(call)
        elif data.startswith('prolong_'):
            callback_prolong(call)
        elif data.startswith('remove_days_'):
            callback_remove_days(call)
        elif data.startswith('remove_sub_'):
            callback_remove_sub(call)
        elif data.startswith('block_'):
            callback_block(call)
        elif data.startswith('unblock_'):
            callback_unblock(call)
        return

    if data.startswith('copy_link_'):
        callback_copy_link(call)
        return

    if data == "admin_announce":
        if not has_permission(user_id, 'announce') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("📨 В ЛС", callback_data="announce_dm"),
            types.InlineKeyboardButton("📢 В каналы", callback_data="announce_channels"),
            types.InlineKeyboardButton("🔙 Назад", callback_data="admin_back_panel")
        )
        try:
            bot.edit_message_text(
                "📢 *Рассылка*\n\nВыберите куда:",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=kb
            )
        except:
            bot.send_message(user_id, "📢 *Рассылка*\n\nВыберите куда:",
                           parse_mode="Markdown", reply_markup=kb)
        return

    if data == "announce_dm":
        if not has_permission(user_id, 'announce') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        bot.answer_callback_query(call.id, "📝 Отправьте текст/медиа")
        bot.send_message(user_id, "📨 *Рассылка в ЛС*\n\nОтправьте текст или медиа.", parse_mode="Markdown")
        with _cache_lock:
            announce_data[user_id] = {'type': 'dm', 'waiting': True, 'timestamp': int(time.time())}
        return

    if data == "announce_channels":
        if not has_permission(user_id, 'announce') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        bot.answer_callback_query(call.id)
        channels = get_broadcast_channels()
        if not channels:
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                types.InlineKeyboardButton("➕ Добавить канал рассылки", callback_data="broadcast_add_channel"),
                types.InlineKeyboardButton("🔙 Назад", callback_data="admin_back_panel")
            )
            bot.send_message(user_id, 
                "❌ Нет каналов для рассылки.\n\nДобавьте каналы через кнопку ниже.", 
                reply_markup=kb)
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for ch_id, ch_name in channels:
            kb.add(types.InlineKeyboardButton(f"📢 {ch_name or ch_id}", callback_data=f"announce_to_channel_{ch_id}"))
        kb.add(types.InlineKeyboardButton("📢 Во все каналы рассылки", callback_data="announce_all_channels"))
        kb.add(types.InlineKeyboardButton("➕ Добавить канал", callback_data="broadcast_add_channel"))
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_back_panel"))
        bot.send_message(user_id, "📢 *Выберите канал для рассылки:*", parse_mode="Markdown", reply_markup=kb)
        return

    if data.startswith("announce_to_channel_"):
        if not has_permission(user_id, 'announce') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        channel_id = int(data.split('_')[3])
        bot.answer_callback_query(call.id, "📝 Отправьте текст/медиа")
        bot.send_message(user_id, f"📢 *Объявление в канал*\n\nID: {channel_id}\n\nОтправьте текст или медиа.", parse_mode="Markdown")
        with _cache_lock:
            announce_data[user_id] = {'type': 'channel', 'channel_id': channel_id, 'waiting': True, 'timestamp': int(time.time())}
        return

    if data == "announce_all_channels":
        if not has_permission(user_id, 'announce') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        bot.answer_callback_query(call.id, "📝 Отправьте текст/медиа")
        bot.send_message(user_id, "📢 *Объявление во все каналы*\n\nОтправьте текст или медиа.", parse_mode="Markdown")
        with _cache_lock:
            announce_data[user_id] = {'type': 'all_channels', 'waiting': True, 'timestamp': int(time.time())}
        return

    if data == "broadcast_add_channel":
        if not has_permission(user_id, 'announce') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, 
            "📢 Отправьте ID канала или чата для рассылки.\n\n"
            "Пример: `-1001234567890`\n\n"
            "Бот должен быть добавлен в канал/чат как администратор.")
        with _cache_lock:
            search_cache[user_id] = {'action': 'add_broadcast_channel', 'timestamp': int(time.time())}
        return

    if data == "admin_back":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        bot.send_message(user_id, "🏠 Главное меню", reply_markup=main_menu())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_back_panel":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        role_name = get_admin_role_name(user_id)
        bot.send_message(
            user_id,
            f"🏛️ Админ панель\n\n👤 Ваша роль: {role_name}",
            reply_markup=admin_menu()
        )
        bot.answer_callback_query(call.id)
        return

    if data == "admin_sub_keys_load":
        if not has_permission(user_id, 'manage_keys') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Завершить", callback_data="admin_sub_keys_finish"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="admin_keys_back")
        )
        msg = bot.send_message(user_id,
            "📥 *Загрузка ключей подписки*\n\n"
            "Эти ключи будут выдаваться пользователям через /sub ссылку.\n\n"
            "Отправляйте ключи, затем нажмите ✅ Завершить",
            parse_mode="Markdown", reply_markup=kb)
        with _cache_lock:
            keys_loading[user_id] = {
                'keys': [], 'mode': 'subscription',
                'message_id': msg.message_id,
                'timestamp': int(time.time())
            }
        return

    if data == "admin_sub_keys_finish":
        if not has_permission(user_id, 'manage_keys') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        with _cache_lock:
            if user_id not in keys_loading:
                bot.answer_callback_query(call.id, "❌ Нет активной загрузки")
                return
            session = keys_loading[user_id]
            keys = session['keys']
            mode = session.get('mode', 'subscription')
            del keys_loading[user_id]
        if not keys:
            bot.answer_callback_query(call.id, "❌ Нет ключей")
            return
        if mode == 'subscription':
            save_subscription_keys_to_db(keys)
        else:
            save_keys_to_db(keys)
        bot.answer_callback_query(call.id, f"✅ Загружено {len(keys)} ключей!")
        show_keys_menu(user_id, call.message.chat.id, call.message.message_id)
        return

    if data in ("admin_keys", "admin_sub_keys_load", "admin_sub_keys_finish") or data.startswith("admin_keys_"):
        if not has_permission(user_id, 'manage_keys') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        bot.answer_callback_query(call.id)
        
        if data == "admin_keys":
            show_keys_menu(user_id, call.message.chat.id, call.message.message_id)
        elif data == "admin_keys_load":
            callback_admin_keys_load(call)
        elif data == "admin_keys_load_finish":
            callback_admin_keys_load_finish(call)
        elif data == "admin_keys_load_cancel":
            callback_admin_keys_load_cancel(call)
        elif data == "admin_keys_clean_dead":
            callback_admin_keys_clean_dead(call)
        elif data == "admin_keys_clear_all":
            callback_admin_keys_clear_all(call)
        elif data == "admin_keys_clear_confirm":
            callback_admin_keys_clear_confirm(call)
        elif data == "admin_keys_back":
            callback_admin_keys_back(call)
        return

    if data == "admin_manage_users":
        if not has_permission(user_id, 'manage_users') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав")
            return
        bot.answer_callback_query(call.id)
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT user_id FROM users ORDER BY user_id")
            users = [row[0] for row in cur.fetchall()]
        finally:
            try:
                cur.close()
            except:
                pass
            return_db_connection(conn)
        if not users:
            try:
                bot.edit_message_text("📭 Нет пользователей.",
                                     call.message.chat.id, call.message.message_id)
            except:
                bot.send_message(user_id, "📭 Нет пользователей.")
            return
        with _cache_lock:
            manage_cache[user_id] = {
                'users': users, 
                'filter': 'all',
                'timestamp': int(time.time())
            }
        kb = build_user_list_keyboard(users, 0, 'all')
        try:
            bot.edit_message_text(
                f"👥 Пользователи ({len(users)}):",
                call.message.chat.id, call.message.message_id,
                reply_markup=kb
            )
        except:
            bot.send_message(user_id, f"👥 Пользователи ({len(users)}):", reply_markup=kb)
        return

    if data == "admin_view_logs":
        if not has_permission(user_id, 'view_logs') and user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нет прав на просмотр логов")
            return
        bot.answer_callback_query(call.id)
        _show_admin_logs(call)
        return

    bot.answer_callback_query(call.id)

# ==================== CALLBACKS ДЛЯ КЛЮЧЕЙ ====================

def callback_admin_keys_load(call):
    user_id = call.from_user.id
    if not has_permission(user_id, 'manage_keys') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    bot.answer_callback_query(call.id, "📥 Отправьте ключи")
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Завершить", callback_data="admin_keys_load_finish"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="admin_keys_load_cancel")
    )
    msg = bot.send_message(
        user_id,
        "📥 *Загрузка ключей подписки*\n\n"
        "Отправляйте ключи, затем нажмите ✅ Завершить",
        parse_mode="Markdown",
        reply_markup=kb
    )
    with _cache_lock:
        keys_loading[user_id] = {
            'keys': [], 'mode': 'subscription',
            'message_id': msg.message_id,
            'timestamp': int(time.time())
        }

def callback_admin_keys_load_finish(call):
    user_id = call.from_user.id
    if not has_permission(user_id, 'manage_keys') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    with _cache_lock:
        if user_id not in keys_loading:
            bot.answer_callback_query(call.id, "❌ Нет активной загрузки")
            return
        session = keys_loading[user_id]
        keys = session['keys']
        mode = session.get('mode', 'subscription')
        del keys_loading[user_id]
    if not keys:
        bot.answer_callback_query(call.id, "❌ Нет загруженных ключей")
        return
    
    if mode == 'subscription':
        save_subscription_keys_to_db(keys)
    else:
        save_keys_to_db(keys)
    
    log_admin_action(user_id, f"Загрузил {len(keys)} ключей ({mode})", details=f"Ключей: {len(keys)}")
    proto_stats = {}
    for k in keys:
        m = re.match(r'([a-z0-9+]+)://', k, re.IGNORECASE)
        if m:
            p = m.group(1).lower()
            proto_stats[p] = proto_stats.get(p, 0) + 1
    stats = '\n'.join(f"  • {p}:// — {c}" for p, c in sorted(proto_stats.items(), key=lambda x: -x[1]))
    bot.answer_callback_query(call.id, f"✅ Загружено {len(keys)} ключей!")
    total_in_db = len(get_keys_from_db())
    try:
        bot.edit_message_text(
            f"✅ *Ключи загружены!*\n\n"
            f"📊 Загружено ключей: {len(keys)}\n"
            f"📋 По протоколам:\n{stats}\n"
            f"📦 Всего в базе: {total_in_db}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )
    except:
        bot.send_message(user_id, 
            f"✅ *Ключи загружены!*\n\n"
            f"📊 Загружено ключей: {len(keys)}\n"
            f"📋 По протоколам:\n{stats}\n"
            f"📦 Всего в базе: {total_in_db}",
            parse_mode="Markdown"
        )

def callback_admin_keys_load_cancel(call):
    user_id = call.from_user.id
    with _cache_lock:
        if user_id in keys_loading:
            del keys_loading[user_id]
    bot.answer_callback_query(call.id, "❌ Отменено")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    show_keys_menu(user_id, call.message.chat.id, call.message.message_id)

def callback_admin_keys_clean_dead(call):
    user_id = call.from_user.id
    if not has_permission(user_id, 'manage_keys') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    keys = get_keys_from_db()
    if not keys:
        bot.answer_callback_query(call.id, "❌ Нет ключей для проверки")
        return
    bot.answer_callback_query(call.id, "⏳ Проверяю ключи...")
    alive_keys = []
    dead_keys = []
    for key in keys:
        match = re.search(r'@([\d\.]+):(\d+)', key)
        if match:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex((match.group(1), int(match.group(2))))
                sock.close()
                if result == 0:
                    alive_keys.append(key)
                else:
                    dead_keys.append(key)
            except:
                dead_keys.append(key)
        else:
            dead_keys.append(key)
    save_keys_to_db(alive_keys)
    log_admin_action(user_id, f"Очистил нерабочие ключи", details=f"Удалено: {len(dead_keys)}, осталось: {len(alive_keys)}")
    text = (
        f"🧹 *Очистка нерабочих ключей завершена!*\n\n"
        f"✅ Оставлено живых: {len(alive_keys)}\n"
        f"🗑️ Удалено нерабочих: {len(dead_keys)}\n"
        f"📦 Всего в базе: {len(alive_keys)}"
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown")
    except:
        bot.send_message(user_id, text, parse_mode="Markdown")
    time.sleep(2)
    show_keys_menu(user_id, call.message.chat.id, call.message.message_id)

def callback_admin_keys_clear_all(call):
    user_id = call.from_user.id
    if not has_permission(user_id, 'manage_keys') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Да, удалить все", callback_data="admin_keys_clear_confirm"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="admin_keys_back")
    )
    try:
        bot.edit_message_text(
            "⚠️ *ВНИМАНИЕ!*\n\n"
            "Вы уверены, что хотите удалить ВСЕ ключи?\n"
            "Это действие НЕЛЬЗЯ будет отменить!",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=kb
        )
    except:
        bot.send_message(user_id, "⚠️ Подтвердите удаление всех ключей.", reply_markup=kb)

def callback_admin_keys_clear_confirm(call):
    user_id = call.from_user.id
    if not has_permission(user_id, 'manage_keys') and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет прав")
        return
    sub_count = len(get_subscription_keys_from_db())
    save_keys_to_db([])
    save_subscription_keys_to_db([])
    set_setting('total_keys_issued', '0')
    log_admin_action(user_id, f"Удалил все ключи", details=f"Подписка: {sub_count}")
    bot.answer_callback_query(call.id, f"🗑️ Удалено {sub_count} ключей!")
    show_keys_menu(user_id, call.message.chat.id, call.message.message_id)

def callback_admin_keys_back(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    show_keys_menu(user_id, call.message.chat.id, call.message.message_id)

# ==================== PRIVATE MESSAGES ====================

@bot.message_handler(func=lambda m: m.chat.type == 'private' and not (m.text or '').startswith('/'))
def handle_private_messages(message):
    user_id = message.from_user.id
    text = message.text or ''

    if message.from_user.username:
        update_user_username(user_id, message.from_user.username)

    if text in MENU_BUTTONS:
        return

    with _cache_lock:
        in_announce = user_id in announce_data
        in_keys_loading = user_id in keys_loading
        in_search = user_id in search_cache

    if in_announce:
        admin_announce_text(message)
        return

    if in_keys_loading:
        raw = text or message.caption or ''
        found_keys = []
        if raw:
            for line in raw.splitlines():
                line = line.strip()
                if line and ('://' in line):
                    found_keys.append(line)
        if not found_keys and message.document:
            try:
                file = bot.get_file(message.document.file_id)
                data_bytes = bot.download_file(file.file_path)
                content = data_bytes.decode('utf-8', errors='ignore')
                for line in content.splitlines():
                    line = line.strip()
                    if line and ('://' in line):
                        found_keys.append(line)
            except:
                pass
        if found_keys:
            with _cache_lock:
                if user_id in keys_loading:
                    keys_loading[user_id]['keys'].extend(found_keys)
                    keys_loading[user_id]['keys'] = list(dict.fromkeys(keys_loading[user_id]['keys']))
                    keys_loading[user_id]['timestamp'] = int(time.time())
                    total = len(keys_loading[user_id]['keys'])
            bot.reply_to(message, f"✅ Загружено {len(found_keys)}. Всего: {total}")
        else:
            bot.reply_to(message, "❌ Ключи не найдены. Отправьте ключи в формате vless://, vmess:// и т.д.")
        return

    if in_search:
        with _cache_lock:
            action = search_cache.get(user_id, {}).get('action', '')
        if action == 'add_broadcast_channel':
            try:
                new_ch_id = int(message.text.strip())
                try:
                    chat_info = bot.get_chat(new_ch_id)
                    ch_name = chat_info.title or str(new_ch_id)
                except:
                    ch_name = str(new_ch_id)
                
                if add_broadcast_channel(new_ch_id, ch_name, user_id):
                    with _cache_lock:
                        if user_id in search_cache:
                            del search_cache[user_id]
                    log_admin_action(user_id, f"Добавил канал рассылки {new_ch_id}", details=ch_name)
                    bot.reply_to(message, f"✅ Канал *{ch_name}* добавлен для рассылки!\n\nТеперь он будет доступен в меню рассылки.", parse_mode="Markdown")
                else:
                    bot.reply_to(message, "❌ Ошибка добавления канала.")
            except ValueError:
                bot.reply_to(message, "❌ Неверный ID. Пример: `-1001234567890`", parse_mode="Markdown")
            return

    if text:
        bot.reply_to(message, "Используйте кнопки меню или /cancel для отмены текущего режима.", reply_markup=main_menu())

# ==================== PRIORITY COMMAND HANDLER ====================

@bot.message_handler(commands=['admin', 'check', 'user', 'add_days', 'remove_days', 
                                'block', 'unblock', 'cancel', 'ref', 'ref_debug', 'logs'])
def cmd_priority_handler(message):
    user_id = message.from_user.id
    command = message.text.split()[0].lower() if message.text else ''
    
    with _cache_lock:
        if user_id in announce_data:
            del announce_data[user_id]
        if user_id in keys_loading:
            del keys_loading[user_id]

    if command == '/admin':
        admin_panel(message)
    elif command == '/check':
        cmd_check_user(message)
    elif command == '/user':
        cmd_user_info(message)
    elif command == '/add_days':
        cmd_add_days(message)
    elif command == '/remove_days':
        cmd_remove_days(message)
    elif command == '/block':
        cmd_block_user(message)
    elif command == '/unblock':
        cmd_unblock_user(message)
    elif command == '/cancel':
        cmd_cancel(message)
    elif command == '/ref':
        cmd_ref_link(message)
    elif command == '/ref_debug':
        cmd_ref_debug(message)
    elif command == '/logs':
        cmd_view_logs(message)

def admin_panel(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.reply_to(message, "⛔️ У вас нет прав администратора.")
        return
    if not has_permission(user_id, 'admin_panel'):
        bot.reply_to(message, "⛔️ У вас нет доступа к админ-панели.")
        return
    role_name = get_admin_role_name(user_id)
    bot.send_message(user_id, f"🏛️ Админ панель\n\n👤 Ваша роль: {role_name}", reply_markup=admin_menu())

def cmd_check_user(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if not is_admin(user_id) or not has_permission(user_id, 'check_user'):
        return
    text = message.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ /check [ID или @username]\n\nПример: `/check 123456789` или `/check @mel1ste` или `/check tg://user?id=123456789`", parse_mode="Markdown")
        return
    target_input = parts[1].strip()
    target_id = get_user_id_from_input(target_input)
    if not target_id:
        bot.reply_to(message, f"❌ Неверный ID или юзернейм: `{target_input}`")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT subscription_end, is_blocked, token FROM users WHERE user_id = %s", (target_id,))
        result = cur.fetchone()
        if not result:
            bot.reply_to(message, "❌ Не найден")
            return
        sub_end, blocked, token = result
        current_time = int(time.time())
        status = "🚫 Заблокирован" if blocked else ("✅ Активен" if sub_end > current_time else "❌ Неактивен")
        text = f"📋 *Проверка*\n🆔 ID: `{target_id}`\n📊 Статус: {status}\n🔗 Токен: `{token}`"
        log_admin_action(user_id, f"Проверил пользователя {target_id}", target_id=target_id)
        bot.reply_to(message, text, parse_mode="Markdown")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def cmd_user_info(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if not is_admin(user_id) or not has_permission(user_id, 'user_info'):
        return
    text = message.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ /user [ID или @username]\n\nПример: `/user 123456789` или `/user @mel1ste` или `/user tg://user?id=123456789`", parse_mode="Markdown")
        return
    target_input = parts[1].strip()
    target_id = get_user_id_from_input(target_input)
    if not target_id:
        bot.reply_to(message, f"❌ Неверный ID или юзернейм: `{target_input}`")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT subscription_end, is_blocked, token, last_activity FROM users WHERE user_id = %s", (target_id,))
        result = cur.fetchone()
        if not result:
            bot.reply_to(message, "❌ Не найден")
            return
        sub_end, blocked, token, last_act = result
        current_time = int(time.time())
        status = "🚫 Заблокирован" if blocked else ("✅ Активен" if sub_end > current_time else "❌ Неактивен")
        name = get_user_display_name_cached(target_id)
        last_act_str = datetime.fromtimestamp(last_act).strftime("%d.%m.%Y %H:%M") if last_act else "Нет"
        text = f"""👤 *{name}*
🆔 ID: `{target_id}`
📊 Статус: {status}
📅 Подписка до: {datetime.fromtimestamp(sub_end).strftime('%d.%m.%Y') if sub_end else 'Нет'}
🕐 Активность: {last_act_str}"""
        log_admin_action(user_id, f"Посмотрел инфо о {target_id}", target_id=target_id)
        bot.reply_to(message, text, parse_mode="Markdown")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def cmd_add_days(message):
    user_id = message.from_user.id
    if not is_admin(user_id) and user_id != ADMIN_ID:
        return
    if not has_permission(user_id, 'add_days') and user_id != ADMIN_ID:
        return
    text = message.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ /add_days [ID или @username] [дни]\n\nПример: `/add_days 123456789 30` или `/add_days @mel1ste 30`", parse_mode="Markdown")
        return
    args = parts[1].strip().split()
    if len(args) < 2:
        bot.reply_to(message, "❌ /add_days [ID или @username] [дни]", parse_mode="Markdown")
        return
    target_id = get_user_id_from_input(args[0])
    if not target_id:
        bot.reply_to(message, f"❌ Неверный ID или юзернейм: `{args[0]}`")
        return
    try:
        days = int(args[1])
    except:
        bot.reply_to(message, "❌ Дни должны быть числом")
        return
    if days < 1:
        bot.reply_to(message, "❌ Количество дней должно быть больше 0.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (target_id,))
        result = cur.fetchone()
        if not result:
            bot.reply_to(message, "❌ Не найден")
            return
        current_time = int(time.time())
        current_end = result[0] if (result[0] and result[0] > current_time) else current_time
        new_end = current_end + days * 24 * 60 * 60
        cur.execute("""
            UPDATE users SET 
                subscription_end = %s, 
                notified_3days = 0, 
                notified_expired = 0 
            WHERE user_id = %s
        """, (new_end, target_id))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
        log_admin_action(user_id, f"Выдал {days} дней {target_id}", target_id=target_id)
        bot.reply_to(message, f"✅ +{days} дней")
    except Exception as e:
        print(f"[add_days] Ошибка: {e}")
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def cmd_remove_days(message):
    user_id = message.from_user.id
    if not is_admin(user_id) and user_id != ADMIN_ID:
        return
    if not has_permission(user_id, 'remove_days') and user_id != ADMIN_ID:
        return
    text = message.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ /remove_days [ID или @username] [дни]\n\nПример: `/remove_days 123456789 30` или `/remove_days @mel1ste 30`", parse_mode="Markdown")
        return
    args = parts[1].strip().split()
    if len(args) < 2:
        bot.reply_to(message, "❌ /remove_days [ID или @username] [дни]", parse_mode="Markdown")
        return
    target_id = get_user_id_from_input(args[0])
    if not target_id:
        bot.reply_to(message, f"❌ Неверный ID или юзернейм: `{args[0]}`")
        return
    try:
        days = int(args[1])
    except:
        bot.reply_to(message, "❌ Дни должны быть числом")
        return
    if days < 1:
        bot.reply_to(message, "❌ Количество дней должно быть больше 0.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (target_id,))
        result = cur.fetchone()
        if not result:
            bot.reply_to(message, "❌ Не найден")
            return
        current_time = int(time.time())
        current_end = result[0] if (result[0] and result[0] > current_time) else current_time
        new_end = current_end - days * 24 * 60 * 60
        if new_end < current_time:
            new_end = current_time - 1
        cur.execute("UPDATE users SET subscription_end = %s, notified_3days = 0 WHERE user_id = %s", (new_end, target_id))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
        log_admin_action(user_id, f"Забрал {days} дней у {target_id}", target_id=target_id)
        bot.reply_to(message, f"✅ -{days} дней")
    except Exception as e:
        print(f"[remove_days] Ошибка: {e}")
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def cmd_block_user(message):
    user_id = message.from_user.id
    if not is_admin(user_id) and user_id != ADMIN_ID:
        return
    if not has_permission(user_id, 'block_user') and user_id != ADMIN_ID:
        return
    text = message.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ /block [ID или @username]\n\nПример: `/block 123456789` или `/block @mel1ste`", parse_mode="Markdown")
        return
    target_input = parts[1].strip()
    target_id = get_user_id_from_input(target_input)
    if not target_id:
        bot.reply_to(message, f"❌ Неверный ID или юзернейм: `{target_input}`")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET is_blocked = 1 WHERE user_id = %s", (target_id,))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
        log_admin_action(user_id, f"Заблокировал {target_id}", target_id=target_id)
        bot.reply_to(message, f"🚫 Заблокирован {target_id}")
        with _user_blocked_cache_lock:
            if target_id in _user_blocked_cache:
                del _user_blocked_cache[target_id]
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def cmd_unblock_user(message):
    user_id = message.from_user.id
    if not is_admin(user_id) and user_id != ADMIN_ID:
        return
    if not has_permission(user_id, 'unblock_user') and user_id != ADMIN_ID:
        return
    text = message.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ /unblock [ID или @username]\n\nПример: `/unblock 123456789` или `/unblock @mel1ste`", parse_mode="Markdown")
        return
    target_input = parts[1].strip()
    target_id = get_user_id_from_input(target_input)
    if not target_id:
        bot.reply_to(message, f"❌ Неверный ID или юзернейм: `{target_input}`")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET is_blocked = 0 WHERE user_id = %s", (target_id,))
        conn.commit()
        
        clear_user_cache(target_id)
        if target_id != user_id:
            clear_user_cache(user_id)
        
        log_admin_action(user_id, f"Разблокировал {target_id}", target_id=target_id)
        bot.reply_to(message, f"✅ Разблокирован {target_id}")
        with _user_blocked_cache_lock:
            if target_id in _user_blocked_cache:
                del _user_blocked_cache[target_id]
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def cmd_cancel(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    cleared = False
    with _cache_lock:
        if user_id in announce_data:
            del announce_data[user_id]
            cleared = True
        if user_id in keys_loading:
            del keys_loading[user_id]
            cleared = True
    if cleared:
        bot.reply_to(message, "✅ Все режимы отменены.")
    else:
        bot.reply_to(message, "❌ Нет активных режимов для отмены.")

def cmd_ref_link(message):
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    bot_username = get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    bot.reply_to(message, f"🔗 *Реферальная ссылка:*\n`{ref_link}`", parse_mode="Markdown")

def cmd_ref_debug(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, referrer_id, referred_id, rewarded FROM referrals ORDER BY id DESC LIMIT 10")
        rows = cur.fetchall()
        if not rows:
            bot.reply_to(message, "📭 Нет рефералов")
            return
        text = "📊 *Рефералы (последние 10):*\n\n"
        for ref_id, refr, refd, rew in rows:
            text += f"{'✅' if rew else '⏳'} {get_user_display_name_cached(refd)} → {get_user_display_name_cached(refr)}\n"
        bot.reply_to(message, text, parse_mode="Markdown")
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

def cmd_view_logs(message):
    if message.chat.type != 'private':
        return
    user_id = message.from_user.id
    if not is_admin(user_id) or not has_permission(user_id, 'view_logs'):
        bot.reply_to(message, "⛔️ У вас нет прав на просмотр логов.")
        return
    
    text = message.text.strip()
    parts = text.split(None, 1)
    limit = 20
    if len(parts) > 1:
        try:
            limit = int(parts[1])
            if limit > 100:
                limit = 100
        except:
            pass
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT admin_name, action, target_name, details, created_at
            FROM admin_logs
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        logs = cur.fetchall()
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)
    
    if not logs:
        bot.reply_to(message, "📋 *Логи админов*\n\nПусто", parse_mode="Markdown")
        return
    
    text = f"📋 *Последние {len(logs)} действий:*\n\n"
    for admin_name, action, target_name, details, created_at in logs:
        time_str = datetime.fromtimestamp(created_at).strftime("%d.%m %H:%M")
        target = f" → {target_name}" if target_name else ""
        text += f"🕐 {time_str} | *{admin_name}* {action}{target}\n"
        if details:
            text += f"  📎 {details}\n"
        text += "\n"
    
    if len(text) > 4000:
        text = text[:3950] + "\n…"
    
    bot.reply_to(message, text, parse_mode="Markdown")

# ==================== FLASK APP ====================

@app.route('/')
def index():
    return "VPN Bot is running!"

@app.route('/ping')
def ping():
    return "OK", 200

@app.route('/health')
def health():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        return_db_connection(conn)
        return "OK", 200
    except:
        return "DB Error", 500

_rate_limit = defaultdict(list)
_RATE_LIMIT_LAST_CLEAN = time.time()

def rate_limit(limit=10, window=60):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            global _RATE_LIMIT_LAST_CLEAN
            ip = request.remote_addr
            now = time.time()
            
            with _rate_limit_lock:
                if now - _RATE_LIMIT_LAST_CLEAN > 3600:
                    cutoff = now - 3600
                    dead = [k for k, v in _rate_limit.items() 
                            if not any(t > cutoff for t in v)]
                    for k in dead:
                        del _rate_limit[k]
                    _RATE_LIMIT_LAST_CLEAN = now
                
                _rate_limit[ip] = [t for t in _rate_limit[ip] if now - t < window]
                if len(_rate_limit[ip]) >= limit:
                    return "Too many requests", 429
                _rate_limit[ip].append(now)
            return f(*args, **kwargs)
        return decorated
    return decorator

@app.route('/sub/<token>')
@rate_limit(limit=10, window=60)
def subscription(token):
    if not token:
        return "Invalid token", 400
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id, subscription_end, is_frozen, is_blocked FROM users WHERE token = %s", (token,))
        result = cur.fetchone()
        if not result:
            return "Invalid token", 404
        user_id, sub_end, is_frozen, is_blocked = result
        
        if is_blocked:
            return "User blocked", 403
        
        current_time = int(time.time())
        
        if is_frozen:
            content = KEY_TEMPLATE.format(
                expire=int(time.time()),
                keys='# Подписка заморожена'
            )
            return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}
        
        if not sub_end or sub_end < current_time:
            return "Subscription expired", 403
        
        try:
            cur.execute("UPDATE users SET last_activity = %s WHERE user_id = %s", (int(time.time()), user_id))
            conn.commit()
        except Exception as e:
            print(f"[sub] Ошибка обновления активности: {e}")
            try:
                conn.rollback()
            except:
                pass
        
        keys = get_subscription_keys_from_db()
        if not keys:
            keys = get_keys_from_db()
        if not keys:
            keys = DEFAULT_KEYS
        expire_timestamp = sub_end
        content = KEY_TEMPLATE.format(expire=expire_timestamp, keys='\n'.join(keys))
        return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    finally:
        try:
            cur.close()
        except:
            pass
        return_db_connection(conn)

# ==================== ЗАПУСК ====================

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN не задан!")
        sys.exit(1)
    if not DATABASE_URL:
        print("❌ DATABASE_URL не задан!")
        sys.exit(1)
    
    port = int(os.getenv('PORT', 5000))
    
    def delayed_start():
        print("🚀 Запуск бота...")
        init_db_pool()
        
        try:
            init_db()
            print("✅ База данных инициализирована")
        except Exception as e:
            print(f"❌ Ошибка БД: {e}")
            sys.exit(1)
        
        ensure_bot_start_time()
        
        try:
            bot.set_my_commands([
                types.BotCommand("start", "Запустить бота"),
                types.BotCommand("admin", "Админ-панель"),
                types.BotCommand("ref", "Реферальная ссылка"),
                types.BotCommand("cancel", "Отменить режим"),
            ])
        except Exception as e:
            print(f"[set_commands] Ошибка: {e}")
        
        Thread(target=cleanup_sessions_scheduler, daemon=True).start()
        
        if os.getenv('RENDER'):
            Thread(target=keep_alive_ping, daemon=True).start()
            Thread(target=auto_restart_monitor, daemon=True).start()
        
        while True:
            try:
                bot.delete_webhook(drop_pending_updates=True)
                time.sleep(1)
                bot.infinity_polling(
                    timeout=30,
                    long_polling_timeout=30,
                    skip_pending=True,
                    allowed_updates=['message', 'callback_query', 'my_chat_member', 'chat_member']
                )
            except Exception as e:
                err = str(e)
                if '409' in err:
                    print(f"⚠️ Конфликт. Ждём 30 сек...")
                    time.sleep(30)
                else:
                    print(f"❌ Polling ошибка: {e}")
                    time.sleep(10)
    
    Thread(target=delayed_start, daemon=True).start()
    
    print(f"📡 Flask на порту {port}...")
    serve(app, host='0.0.0.0', port=port)
