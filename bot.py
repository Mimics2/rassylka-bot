# [file name]: railway_session_bot/bot.py
import os
import logging
import asyncio
import json
import random
import qrcode
import sqlite3
import secrets
from io import BytesIO
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from contextlib import asynccontextmanager

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

try:
    from aiogram import Bot, Dispatcher, Router, F, html
    from aiogram.types import (
        Message, CallbackQuery, BufferedInputFile,
        InlineKeyboardButton, InlineKeyboardMarkup,
        WebAppInfo
    )
    from aiogram.filters import Command, CommandStart
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError
    from fastapi import FastAPI, Request, Depends, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    import jinja2
    import uvicorn
except ImportError as e:
    logger.error(f"❌ Missing dependencies: {e}")
    print("📦 Установите зависимости: pip install -r requirements.txt")
    exit(1)

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не установлен в переменных окружения")
    exit(1)

WEBHOOK_URL = os.environ.get('RAILWAY_STATIC_URL', '').rstrip('/')
WEBAPP_URL = os.environ.get('RAILWAY_STATIC_URL', '').rstrip('/') + "/panel"
SECRET_KEY = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Настройка базы данных
DB_PATH = "sessions.db"

# ==================== БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        """Инициализация базы данных"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Таблица API ключей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_id INTEGER NOT NULL,
                api_hash TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица сессий
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                telegram_id INTEGER,
                phone_number TEXT,
                session_string TEXT NOT NULL,
                api_config_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (api_config_id) REFERENCES api_configs (id)
            )
        ''')
        
        # Таблица статистики
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                user_id INTEGER,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица настроек бота
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
        
        # Добавляем дефолтные API если их нет
        self.add_default_apis()
    
    def add_default_apis(self):
        """Добавляем дефолтные API ключи"""
        default_apis = [
            {"name": "Telegram Desktop", "api_id": 2040, "api_hash": "b18441a1ff607e10a989891a5462e627"},
            {"name": "Android", "api_id": 6, "api_hash": "eb06d4abfb49dc3eeb1aeb98ae0f581e"},
            {"name": "iOS", "api_id": 4, "api_hash": "014b35b6184100b085b0d0572f9b5103"},
            {"name": "Webogram", "api_id": 2496, "api_hash": "8da85b0d5bfe62527e5b244c209159c3"},
        ]
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for api in default_apis:
            cursor.execute('''
                INSERT OR IGNORE INTO api_configs (name, api_id, api_hash)
                VALUES (?, ?, ?)
            ''', (api['name'], api['api_id'], api['api_hash']))
        
        conn.commit()
        conn.close()
    
    def add_api_config(self, name: str, api_id: int, api_hash: str):
        """Добавить новый API конфиг"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO api_configs (name, api_id, api_hash)
            VALUES (?, ?, ?)
        ''', (name, api_id, api_hash))
        conn.commit()
        conn.close()
    
    def get_active_apis(self) -> List[Tuple]:
        """Получить список активных API"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT id, name, api_id, api_hash FROM api_configs WHERE is_active = 1')
        result = cursor.fetchall()
        conn.close()
        return result
    
    def get_api_by_id(self, api_id: int) -> Optional[Tuple]:
        """Получить API по ID"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT id, name, api_id, api_hash FROM api_configs WHERE id = ?', (api_id,))
        result = cursor.fetchone()
        conn.close()
        return result
    
    def save_session(self, user_id: int, session_string: str, api_config_id: int, 
                    telegram_id: int = None, phone_number: str = None):
        """Сохранить сессию в БД"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO user_sessions (user_id, telegram_id, phone_number, session_string, api_config_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, telegram_id, phone_number, session_string, api_config_id))
        conn.commit()
        conn.close()
        
        # Логируем в статистику
        self.log_action(user_id, "session_created", f"api_config_id: {api_config_id}")
    
    def get_user_sessions(self, user_id: int) -> List[Tuple]:
        """Получить сессии пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT us.id, us.created_at, us.telegram_id, us.phone_number, ac.name
            FROM user_sessions us
            LEFT JOIN api_configs ac ON us.api_config_id = ac.id
            WHERE us.user_id = ?
            ORDER BY us.created_at DESC
        ''', (user_id,))
        result = cursor.fetchall()
        conn.close()
        return result
    
    def log_action(self, user_id: int, action: str, details: str = ""):
        """Логирование действий"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO stats (action, user_id, details)
            VALUES (?, ?, ?)
        ''', (action, user_id, details))
        conn.commit()
        conn.close()
    
    def get_stats(self, days: int = 7) -> Dict:
        """Получить статистику"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Общая статистика
        cursor.execute('SELECT COUNT(*) FROM user_sessions')
        total_sessions = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM api_configs WHERE is_active = 1')
        active_apis = cursor.fetchone()[0]
        
        # Статистика по дням
        cursor.execute('''
            SELECT DATE(created_at) as date, COUNT(*) as count
            FROM user_sessions
            WHERE created_at >= datetime('now', ?)
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        ''', (f'-{days} days',))
        daily_stats = cursor.fetchall()
        
        conn.close()
        
        return {
            "total_sessions": total_sessions,
            "active_apis": active_apis,
            "daily_stats": daily_stats
        }
    
    def update_setting(self, key: str, value: str):
        """Обновить настройку"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO bot_settings (key, value)
            VALUES (?, ?)
        ''', (key, value))
        conn.commit()
        conn.close()
    
    def get_setting(self, key: str, default: str = "") -> str:
        """Получить настройку"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM bot_settings WHERE key = ?', (key,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else default

# Инициализация БД
db = Database()

# ==================== МЕНЕДЖЕР СЕССИЙ ====================
class SessionManager:
    def __init__(self):
        self.active_qr_sessions: Dict[int, Dict] = {}
        self.clients: Dict[int, TelegramClient] = {}
    
    async def create_qr_session(self, user_id: int, api_config_id: int, message: Message):
        """Создание QR-сессии"""
        try:
            # Получаем API конфиг
            api_config = db.get_api_by_id(api_config_id)
            if not api_config:
                return False, "❌ API конфиг не найден"
            
            _, api_name, api_id, api_hash = api_config
            
            # Закрываем старую сессию если есть
            if user_id in self.active_qr_sessions:
                try:
                    await self.active_qr_sessions[user_id]['client'].disconnect()
                except:
                    pass
            
            # Выбираем случайное устройство
            devices = [
                {
                    "device_model": "Samsung SM-G991B",
                    "system_version": "Android 13",
                    "app_version": "10.0.0",
                },
                {
                    "device_model": "iPhone15,3", 
                    "system_version": "iOS 17.1.2",
                    "app_version": "10.0.0",
                },
                {
                    "device_model": "Desktop",
                    "system_version": "Windows 10",
                    "app_version": "4.0.0",
                }
            ]
            
            device = random.choice(devices)
            
            # Создаем клиент
            client = TelegramClient(StringSession(), api_id, api_hash, **device)
            await client.connect()
            
            # Создаем QR-логин
            qr_login = await client.qr_login()
            
            # Сохраняем сессию
            self.active_qr_sessions[user_id] = {
                'client': client,
                'qr_login': qr_login,
                'api_config_id': api_config_id,
                'api_name': api_name,
                'created_at': datetime.now(),
                'message': message
            }
            
            db.log_action(user_id, "qr_created", f"api: {api_name}")
            
            return True, qr_login.url
            
        except Exception as e:
            logger.error(f"QR creation error: {e}")
            return False, f"❌ Ошибка: {str(e)}"
    
    async def wait_for_qr_scan(self, user_id: int):
        """Ожидание сканирования QR-кода"""
        if user_id not in self.active_qr_sessions:
            return False, "❌ Сессия не найдена"
        
        data = self.active_qr_sessions[user_id]
        
        try:
            # Ждем сканирования (120 секунд)
            await asyncio.wait_for(data['qr_login'].wait(), timeout=120)
            
            # Даем время на подтверждение
            await asyncio.sleep(3)
            
            # Проверяем авторизацию
            is_authorized = await data['client'].is_user_authorized()
            if not is_authorized:
                return False, "❌ Авторизация не завершена"
            
            # Получаем строку сессии
            session_string = data['client'].session.save()
            
            # Получаем информацию о пользователе
            try:
                me = await data['client'].get_me()
                telegram_id = me.id
                phone_number = me.phone
            except:
                telegram_id = None
                phone_number = None
            
            # Сохраняем сессию в БД
            db.save_session(
                user_id=user_id,
                session_string=session_string,
                api_config_id=data['api_config_id'],
                telegram_id=telegram_id,
                phone_number=phone_number
            )
            
            # Закрываем клиент
            await data['client'].disconnect()
            
            # Удаляем из активных
            del self.active_qr_sessions[user_id]
            
            return True, {
                "session_string": session_string,
                "telegram_id": telegram_id,
                "phone_number": phone_number,
                "api_name": data['api_name']
            }
            
        except asyncio.TimeoutError:
            # Очищаем сессию
            await self.cleanup_session(user_id)
            return False, "❌ Время ожидания истекло"
        except Exception as e:
            logger.error(f"QR wait error: {e}")
            await self.cleanup_session(user_id)
            return False, f"❌ Ошибка: {str(e)}"
    
    async def cleanup_session(self, user_id: int):
        """Очистка сессии"""
        if user_id in self.active_qr_sessions:
            try:
                await self.active_qr_sessions[user_id]['client'].disconnect()
            except:
                pass
            del self.active_qr_sessions[user_id]

# ==================== AIOGRAM БОТ ====================
class SessionBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.router = Router()
        self.dp.include_router(self.router)
        
        self.manager = SessionManager()
        self.setup_handlers()
    
    def setup_handlers(self):
        """Настройка обработчиков"""
        
        @self.router.message(CommandStart())
        async def cmd_start(message: Message, state: FSMContext):
            await state.clear()
            
            # Приветственное сообщение с веб-панелью
            builder = InlineKeyboardBuilder()
            builder.button(
                text="📱 Открыть веб-панель", 
                web_app=WebAppInfo(url=WEBAPP_URL + f"?user_id={message.from_user.id}")
            )
            builder.button(text="🔧 Создать сессию", callback_data="create_session")
            builder.button(text="📊 Мои сессии", callback_data="my_sessions")
            builder.adjust(1)
            
            await message.answer(
                f"👋 Привет, {html.bold(message.from_user.first_name)}!\n\n"
                f"🔐 <b>Генератор сессий Telegram</b>\n\n"
                f"📊 <b>Веб-панель:</b> {WEBAPP_URL}\n"
                f"🔑 <b>API конфигов:</b> {len(db.get_active_apis())}\n\n"
                f"Выберите действие:",
                reply_markup=builder.as_markup()
            )
        
        @self.router.callback_query(F.data == "create_session")
        async def handle_create_session(callback: CallbackQuery):
            """Выбор API для создания сессии"""
            apis = db.get_active_apis()
            
            if not apis:
                await callback.answer("❌ Нет доступных API конфигов", show_alert=True)
                return
            
            builder = InlineKeyboardBuilder()
            for api_id, api_name, _, _ in apis:
                builder.button(text=f"🔧 {api_name}", callback_data=f"api_select_{api_id}")
            
            builder.button(text="⬅️ Назад", callback_data="back_to_main")
            builder.adjust(1)
            
            await callback.message.edit_text(
                "🔧 <b>Выберите API конфиг:</b>\n\n"
                "Каждый API соответствует разному клиенту Telegram.",
                reply_markup=builder.as_markup()
            )
        
        @self.router.callback_query(F.data.startswith("api_select_"))
        async def handle_api_select(callback: CallbackQuery):
            """Обработка выбора API"""
            api_id = int(callback.data.split("_")[-1])
            api_config = db.get_api_by_id(api_id)
            
            if not api_config:
                await callback.answer("❌ API не найден", show_alert=True)
                return
            
            _, api_name, api_id_num, _ = api_config
            
            await callback.message.edit_text(f"🔄 Создаю сессию с API: {api_name}...")
            
            # Создаем QR-сессию
            success, result = await self.manager.create_qr_session(
                user_id=callback.from_user.id,
                api_config_id=api_id,
                message=callback.message
            )
            
            if not success:
                await callback.message.edit_text(f"❌ {result}")
                return
            
            # Создаем QR-код изображение
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(result)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            bio = BytesIO()
            img.save(bio, 'PNG')
            bio.seek(0)
            
            qr_file = BufferedInputFile(bio.getvalue(), filename="qr_code.png")
            
            # Отправляем QR-код
            await callback.message.answer_photo(
                photo=qr_file,
                caption=(
                    f"📷 <b>QR-код для {api_name}</b>\n\n"
                    f"🔑 API ID: <code>{api_id_num}</code>\n\n"
                    f"📱 <b>Инструкция:</b>\n"
                    f"1. Откройте Telegram → Настройки\n"
                    f"2. Устройства → Подключить устройство\n"
                    f"3. Отсканируйте QR-код\n"
                    f"4. Подтвердите вход в приложении\n\n"
                    f"⏳ <b>Ожидаю 2 минуты...</b>\n"
                    f"✅ Сессия придет автоматически"
                )
            )
            
            # Запускаем ожидание сканирования
            asyncio.create_task(self.wait_and_send_session(callback.from_user.id, callback.message))
            
            db.log_action(callback.from_user.id, "qr_sent", f"api: {api_name}")
        
        @self.router.callback_query(F.data == "my_sessions")
        async def handle_my_sessions(callback: CallbackQuery):
            """Показать сессии пользователя"""
            sessions = db.get_user_sessions(callback.from_user.id)
            
            if not sessions:
                await callback.message.edit_text(
                    "📭 <b>У вас еще нет сессий</b>\n\n"
                    "Создайте первую сессию через меню.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")
                        ]]
                    )
                )
                return
            
            text = f"📋 <b>Ваши сессии ({len(sessions)}):</b>\n\n"
            for i, (session_id, created_at, tg_id, phone, api_name) in enumerate(sessions[:10], 1):
                date_str = created_at.split()[0] if created_at else "N/A"
                text += f"{i}. <b>{api_name or 'Unknown'}</b>\n"
                text += f"   📅 {date_str}\n"
                if tg_id:
                    text += f"   👤 ID: <code>{tg_id}</code>\n"
                if phone:
                    text += f"   📱 {phone}\n"
                text += "\n"
            
            if len(sessions) > 10:
                text += f"<i>... и еще {len(sessions) - 10} сессий</i>\n\n"
            
            text += "💾 <b>Веб-панель:</b> Для просмотра и управления всеми сессиями используйте веб-панель."
            
            builder = InlineKeyboardBuilder()
            builder.button(
                text="🌐 Открыть веб-панель", 
                web_app=WebAppInfo(url=WEBAPP_URL + f"?user_id={callback.from_user.id}")
            )
            builder.button(text="⬅️ Назад", callback_data="back_to_main")
            builder.adjust(1)
            
            await callback.message.edit_text(
                text,
                reply_markup=builder.as_markup()
            )
        
        @self.router.callback_query(F.data == "back_to_main")
        async def handle_back(callback: CallbackQuery, state: FSMContext):
            """Возврат в главное меню"""
            await cmd_start(callback.message, state)
        
        @self.router.message(Command("stats"))
        async def cmd_stats(message: Message):
            """Статистика бота"""
            if message.from_user.id not in [ADMIN_ID for ADMIN_ID in map(int, os.environ.get('ADMIN_IDS', '').split(',')) if ADMIN_ID]:
                await message.answer("❌ Доступ запрещен")
                return
            
            stats = db.get_stats()
            
            text = f"📊 <b>Статистика бота:</b>\n\n"
            text += f"🔐 Всего сессий: <b>{stats['total_sessions']}</b>\n"
            text += f"🔧 Активных API: <b>{stats['active_apis']}</b>\n\n"
            
            if stats['daily_stats']:
                text += f"<b>За последние 7 дней:</b>\n"
                for date, count in stats['daily_stats'][:7]:
                    text += f"📅 {date}: <b>{count}</b> сессий\n"
            
            await message.answer(text)
        
        @self.router.message(Command("panel"))
        async def cmd_panel(message: Message):
            """Ссылка на веб-панель"""
            panel_url = WEBAPP_URL + f"?user_id={message.from_user.id}"
            builder = InlineKeyboardBuilder()
            builder.button(text="🌐 Открыть панель", url=panel_url)
            
            await message.answer(
                f"🌐 <b>Веб-панель управления</b>\n\n"
                f"📊 Статистика\n"
                f"🔧 Управление API\n"
                f"📋 Просмотр сессий\n\n"
                f"<a href='{panel_url}'>Нажмите здесь или используйте кнопку ниже</a>",
                reply_markup=builder.as_markup()
            )
    
    async def wait_and_send_session(self, user_id: int, message: Message):
        """Ожидание и отправка сессии"""
        try:
            # Ждем сканирования
            await asyncio.sleep(2)  # Даем время пользователю прочитать инструкции
            
            status_msg = await message.answer("⏳ Ожидаю сканирование QR-кода...")
            
            success, result = await self.manager.wait_for_qr_scan(user_id)
            
            if success:
                session_info = result
                await status_msg.edit_text("✅ Сессия получена! Отправляю...")
                
                # Создаем файл сессии
                session_bytes = session_info['session_string'].encode('utf-8')
                session_file = BufferedInputFile(session_bytes, filename=f"telegram_session_{session_info.get('telegram_id', '')}.txt")
                
                # Отправляем файл
                caption = (
                    f"✅ <b>Сессия успешно создана!</b>\n\n"
                    f"🔧 API: {session_info['api_name']}\n"
                )
                
                if session_info.get('telegram_id'):
                    caption += f"👤 Telegram ID: <code>{session_info['telegram_id']}</code>\n"
                if session_info.get('phone_number'):
                    caption += f"📱 Номер: {session_info['phone_number']}\n"
                
                caption += "\n💾 <b>Сохраните этот файл!</b>\n🔒 Он дает полный доступ к аккаунту."
                
                await message.answer_document(
                    document=session_file,
                    caption=caption
                )
                
                # Также отправляем текстовую версию
                await message.answer(
                    f"📋 <b>Session String:</b>\n"
                    f"<code>{session_info['session_string'][:100]}...</code>\n\n"
                    f"<i>Полный текст в файле выше</i>"
                )
                
                db.log_action(user_id, "session_sent", f"tg_id: {session_info.get('telegram_id', 'unknown')}")
                
            else:
                await status_msg.edit_text(f"❌ {result}")
                db.log_action(user_id, "qr_failed", f"reason: {result}")
                
        except Exception as e:
            logger.error(f"Error in wait_and_send: {e}")
            try:
                await message.answer(f"❌ Ошибка: {str(e)}")
            except:
                pass
    
    async def start(self):
        """Запуск бота"""
        logger.info("🚀 Starting Session Bot...")
        await self.dp.start_polling(self.bot)

# ==================== FASTAPI ВЕБ-ПАНЕЛЬ ====================
app = FastAPI(title="Telegram Session Manager")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Создаем папки
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

# Создаем шаблоны
templates = Jinja2Templates(directory="templates")

# Создаем HTML шаблон для панели
PANEL_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Telegram Session Manager</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        
        .header {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.1);
        }
        
        h1 {
            color: #333;
            margin-bottom: 10px;
            font-size: 2.5em;
        }
        
        .subtitle {
            color: #666;
            font-size: 1.1em;
            margin-bottom: 20px;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .stat-card {
            background: white;
            border-radius: 15px;
            padding: 25px;
            text-align: center;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);
            transition: transform 0.3s, box-shadow 0.3s;
        }
        
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 40px rgba(0, 0, 0, 0.12);
        }
        
        .stat-number {
            font-size: 3em;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 10px;
        }
        
        .stat-label {
            color: #666;
            font-size: 1.1em;
        }
        
        .section {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.1);
        }
        
        h2 {
            color: #333;
            margin-bottom: 20px;
            font-size: 1.8em;
        }
        
        .api-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .api-card {
            background: white;
            border-radius: 15px;
            padding: 20px;
            border-left: 5px solid #667eea;
            box-shadow: 0 5px 20px rgba(0, 0, 0, 0.05);
        }
        
        .api-name {
            font-weight: bold;
            color: #333;
            margin-bottom: 5px;
            font-size: 1.2em;
        }
        
        .api-id {
            color: #666;
            font-family: monospace;
            font-size: 0.9em;
        }
        
        .sessions-table {
            width: 100%;
            background: white;
            border-radius: 15px;
            overflow: hidden;
            box-shadow: 0 5px 20px rgba(0, 0, 0, 0.05);
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        th, td {
            padding: 15px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }
        
        th {
            background: #f8f9fa;
            font-weight: bold;
            color: #333;
        }
        
        tr:hover {
            background: #f8f9fa;
        }
        
        .btn {
            display: inline-block;
            background: #667eea;
            color: white;
            padding: 12px 25px;
            border-radius: 10px;
            text-decoration: none;
            font-weight: bold;
            margin: 10px 5px;
            transition: all 0.3s;
            border: none;
            cursor: pointer;
            font-size: 1em;
        }
        
        .btn:hover {
            background: #5a6fd8;
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3);
        }
        
        .btn-success {
            background: #10b981;
        }
        
        .btn-success:hover {
            background: #0da271;
            box-shadow: 0 10px 20px rgba(16, 185, 129, 0.3);
        }
        
        .btn-danger {
            background: #ef4444;
        }
        
        .btn-danger:hover {
            background: #dc2626;
            box-shadow: 0 10px 20px rgba(239, 68, 68, 0.3);
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #333;
            font-weight: 500;
        }
        
        .form-control {
            width: 100%;
            padding: 12px 15px;
            border: 2px solid #e5e7eb;
            border-radius: 10px;
            font-size: 1em;
            transition: border-color 0.3s;
        }
        
        .form-control:focus {
            border-color: #667eea;
            outline: none;
        }
        
        .qr-container {
            text-align: center;
            padding: 30px;
            background: white;
            border-radius: 15px;
            margin: 20px 0;
        }
        
        .qr-image {
            max-width: 300px;
            margin: 20px auto;
            display: block;
        }
        
        .alert {
            padding: 15px;
            border-radius: 10px;
            margin: 20px 0;
        }
        
        .alert-success {
            background: #d1fae5;
            color: #065f46;
            border: 1px solid #a7f3d0;
        }
        
        .alert-warning {
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fde68a;
        }
        
        .alert-error {
            background: #fee2e2;
            color: #991b1b;
            border: 1px solid #fecaca;
        }
        
        .loader {
            border: 4px solid #f3f3f3;
            border-top: 4px solid #667eea;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 20px auto;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .status-badge {
            display: inline-block;
            padding: 5px 10px;
            border-radius: 20px;
            font-size: 0.8em;
            font-weight: bold;
        }
        
        .status-active {
            background: #d1fae5;
            color: #065f46;
        }
        
        .status-inactive {
            background: #fef3c7;
            color: #92400e;
        }
        
        @media (max-width: 768px) {
            .container {
                padding: 10px;
            }
            
            .header, .section {
                padding: 20px;
            }
            
            .stats-grid {
                grid-template-columns: 1fr;
            }
            
            h1 {
                font-size: 2em;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔐 Telegram Session Manager</h1>
            <p class="subtitle">Управление сессиями Telegram через QR-код</p>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-number">{{ stats.total_sessions }}</div>
                    <div class="stat-label">Всего сессий</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">{{ stats.active_apis }}</div>
                    <div class="stat-label">API конфигов</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">{{ user_sessions|length }}</div>
                    <div class="stat-label">Ваших сессий</div>
                </div>
            </div>
            
            <div>
                <button onclick="window.Telegram.WebApp.close()" class="btn btn-danger">Закрыть панель</button>
                <button onclick="location.reload()" class="btn">Обновить</button>
                <button onclick="showCreateSession()" class="btn btn-success">➕ Создать сессию</button>
            </div>
        </div>
        
        {% if message %}
        <div class="alert alert-{{ message.type }}">
            {{ message.text }}
        </div>
        {% endif %}
        
        {% if qr_url %}
        <div class="section">
            <h2>📷 QR-код для подключения</h2>
            <div class="qr-container">
                <img src="{{ qr_url }}" alt="QR Code" class="qr-image">
                <p>1. Откройте Telegram → Настройки</p>
                <p>2. Устройства → Подключить устройство</p>
                <p>3. Отсканируйте QR-код</p>
                <p>4. Подтвердите вход в приложении</p>
                <div class="loader" id="qrLoader"></div>
                <p id="qrStatus">⏳ Ожидаю сканирование...</p>
            </div>
        </div>
        {% endif %}
        
        <div class="section">
            <h2>🔧 API Конфигурации</h2>
            <div class="api-grid">
                {% for api in apis %}
                <div class="api-card">
                    <div class="api-name">{{ api.name }}</div>
                    <div class="api-id">ID: {{ api.api_id }} | Hash: {{ api.api_hash[:8] }}...</div>
                    <button onclick="createSession({{ api.id }})" class="btn" style="margin-top: 10px;">
                        Создать сессию
                    </button>
                </div>
                {% endfor %}
            </div>
            
            <h3 style="margin-top: 30px;">➕ Добавить новый API</h3>
            <form id="apiForm" onsubmit="return addApi(event)">
                <div class="form-group">
                    <label>Название API</label>
                    <input type="text" name="name" class="form-control" placeholder="Например: My Telegram App" required>
                </div>
                <div class="form-group">
                    <label>API ID</label>
                    <input type="number" name="api_id" class="form-control" placeholder="123456" required>
                </div>
                <div class="form-group">
                    <label>API Hash</label>
                    <input type="text" name="api_hash" class="form-control" placeholder="a1b2c3d4e5f6..." required>
                </div>
                <button type="submit" class="btn btn-success">Добавить API</button>
            </form>
        </div>
        
        <div class="section">
            <h2>📋 Ваши сессии</h2>
            {% if user_sessions %}
            <div class="sessions-table">
                <table>
                    <thead>
                        <tr>
                            <th>Дата</th>
                            <th>API</th>
                            <th>Telegram ID</th>
                            <th>Номер</th>
                            <th>Действия</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for session in user_sessions %}
                        <tr>
                            <td>{{ session.created_at.split()[0] }}</td>
                            <td>{{ session.api_name or 'Unknown' }}</td>
                            <td>{{ session.telegram_id or '—' }}</td>
                            <td>{{ session.phone_number or '—' }}</td>
                            <td>
                                <button onclick="copySession('{{ session.session_string }}')" class="btn">
                                    Копировать
                                </button>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% else %}
            <p>У вас еще нет созданных сессий. Создайте первую!</p>
            {% endif %}
        </div>
    </div>
    
    <script>
        // Получаем user_id из URL
        const urlParams = new URLSearchParams(window.location.search);
        const userId = urlParams.get('user_id');
        
        function showCreateSession() {
            document.querySelector('#apiForm').scrollIntoView({ behavior: 'smooth' });
        }
        
        async function createSession(apiId) {
            try {
                const response = await fetch(`/api/create_session?user_id=${userId}&api_id=${apiId}`);
                const data = await response.json();
                
                if (data.success && data.qr_url) {
                    // Показываем QR-код и начинаем опрос статуса
                    window.location.href = `/panel?user_id=${userId}&qr=${encodeURIComponent(data.qr_url)}&api_id=${apiId}`;
                } else {
                    alert(data.error || 'Ошибка создания сессии');
                }
            } catch (error) {
                alert('Ошибка: ' + error.message);
            }
        }
        
        async function addApi(event) {
            event.preventDefault();
            
            const formData = new FormData(event.target);
            const data = Object.fromEntries(formData.entries());
            
            try {
                const response = await fetch('/api/add_api', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        ...data,
                        user_id: userId
                    })
                });
                
                const result = await response.json();
                
                if (result.success) {
                    alert('API успешно добавлен!');
                    location.reload();
                } else {
                    alert(result.error || 'Ошибка добавления API');
                }
            } catch (error) {
                alert('Ошибка: ' + error.message);
            }
        }
        
        function copySession(sessionString) {
            navigator.clipboard.writeText(sessionString).then(() => {
                alert('Сессия скопирована в буфер обмена!');
            });
        }
        
        // Если есть QR в URL, начинаем опрос статуса
        {% if qr_url and api_id %}
        let checkCount = 0;
        const maxChecks = 60; // 2 минуты (60 * 2 секунды)
        
        async function checkSessionStatus() {
            checkCount++;
            
            if (checkCount > maxChecks) {
                document.getElementById('qrStatus').textContent = '❌ Время ожидания истекло';
                document.getElementById('qrLoader').style.display = 'none';
                return;
            }
            
            try {
                const response = await fetch(`/api/check_session?user_id=${userId}&api_id={{ api_id }}`);
                const data = await response.json();
                
                if (data.status === 'completed') {
                    document.getElementById('qrStatus').textContent = '✅ Сессия создана!';
                    document.getElementById('qrLoader').style.display = 'none';
                    
                    // Показываем результат
                    alert(`Сессия успешно создана!\n\nTelegram ID: ${data.telegram_id || 'Неизвестно'}`);
                    
                    // Обновляем страницу через 2 секунды
                    setTimeout(() => {
                        window.location.href = `/panel?user_id=${userId}`;
                    }, 2000);
                    
                } else if (data.status === 'waiting') {
                    document.getElementById('qrStatus').textContent = `⏳ Ожидаю сканирование... (${checkCount}/60)`;
                    setTimeout(checkSessionStatus, 2000);
                } else if (data.status === 'error') {
                    document.getElementById('qrStatus').textContent = `❌ Ошибка: ${data.error}`;
                    document.getElementById('qrLoader').style.display = 'none';
                }
            } catch (error) {
                console.error('Error checking status:', error);
                setTimeout(checkSessionStatus, 2000);
            }
        }
        
        // Запускаем проверку статуса
        setTimeout(checkSessionStatus, 2000);
        {% endif %}
        
        // Инициализация Telegram Web App
        if (window.Telegram && window.Telegram.WebApp) {
            Telegram.WebApp.ready();
            Telegram.WebApp.expand();
            Telegram.WebApp.enableClosingConfirmation();
        }
    </script>
</body>
</html>
"""

# Сохраняем HTML шаблон
with open("templates/panel.html", "w", encoding="utf-8") as f:
    f.write(PANEL_HTML)

# ==================== FASTAPI РОУТЫ ====================
session_bot = SessionBot()
session_manager = session_bot.manager

@app.get("/")
async def root():
    return RedirectResponse(url="/panel")

@app.get("/panel", response_class=HTMLResponse)
async def web_panel(request: Request, user_id: int = None, qr: str = None, api_id: int = None):
    """Веб-панель управления"""
    if not user_id:
        return HTMLResponse("<h1>❌ User ID required</h1>")
    
    # Получаем данные для панели
    apis = db.get_active_apis()
    user_sessions = db.get_user_sessions(user_id)
    stats = db.get_stats()
    
    # Форматируем API для отображения
    api_list = []
    for api in apis:
        api_list.append({
            "id": api[0],
            "name": api[1],
            "api_id": api[2],
            "api_hash": api[3]
        })
    
    # Форматируем сессии
    sessions_list = []
    for session in user_sessions:
        sessions_list.append({
            "created_at": session[1],
            "api_name": session[4],
            "telegram_id": session[2],
            "phone_number": session[3],
            "session_string": session[5] if len(session) > 5 else None
        })
    
    context = {
        "request": request,
        "user_id": user_id,
        "apis": api_list,
        "user_sessions": sessions_list,
        "stats": stats,
        "qr_url": qr,
        "api_id": api_id
    }
    
    return templates.TemplateResponse("panel.html", context)

@app.get("/api/create_session")
async def api_create_session(user_id: int, api_id: int):
    """API для создания сессии"""
    try:
        # Создаем временное сообщение для бота
        from aiogram.types import Message
        dummy_msg = type('obj', (object,), {
            'answer': lambda *args, **kwargs: None,
            'edit_text': lambda *args, **kwargs: None
        })
        
        # Создаем QR-сессию
        success, result = await session_manager.create_qr_session(user_id, api_id, dummy_msg)
        
        if success:
            return JSONResponse({
                "success": True,
                "qr_url": f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={result}"
            })
        else:
            return JSONResponse({
                "success": False,
                "error": result
            })
            
    except Exception as e:
        logger.error(f"API create session error: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e)
        })

@app.get("/api/check_session")
async def api_check_session(user_id: int, api_id: int):
    """API для проверки статуса сессии"""
    try:
        # Проверяем, есть ли активная сессия
        if user_id in session_manager.active_qr_sessions:
            return JSONResponse({
                "status": "waiting",
                "message": "Ожидание сканирования QR-кода"
            })
        
        # Проверяем последнюю созданную сессию пользователя
        user_sessions = db.get_user_sessions(user_id)
        if user_sessions:
            latest_session = user_sessions[0]
            return JSONResponse({
                "status": "completed",
                "telegram_id": latest_session[2],
                "phone_number": latest_session[3]
            })
        
        return JSONResponse({
            "status": "error",
            "error": "Сессия не найдена"
        })
        
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e)
        })

@app.post("/api/add_api")
async def api_add_api(request: Request):
    """API для добавления нового API"""
    try:
        data = await request.json()
        
        # Проверяем user_id (здесь можно добавить проверку прав)
        user_id = data.get('user_id')
        if not user_id:
            raise HTTPException(status_code=400, detail="User ID required")
        
        # Добавляем API
        db.add_api_config(
            name=data['name'],
            api_id=int(data['api_id']),
            api_hash=data['api_hash']
        )
        
        db.log_action(user_id, "api_added", f"name: {data['name']}")
        
        return JSONResponse({
            "success": True,
            "message": "API добавлен"
        })
        
    except Exception as e:
        logger.error(f"API add error: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e)
        })

@app.get("/api/stats")
async def api_stats():
    """API для получения статистики"""
    stats = db.get_stats()
    return JSONResponse(stats)

@app.get("/health")
async def health_check():
    """Health check для Railway"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запуск и остановка приложения"""
    # Запускаем бота в фоне
    bot_task = asyncio.create_task(session_bot.start())
    logger.info("🚀 Application started")
    
    yield
    
    # Останавливаем бота
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    logger.info("🛑 Application stopped")

app.lifespan = lifespan

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"🌐 Starting web server on port {port}")
    
    uvicorn.run(
        "bot:app",
        host="0.0.0.0",
        port=port,
        reload=False
    )
