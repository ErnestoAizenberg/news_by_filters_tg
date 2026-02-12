import asyncio
import logging
import re
import json
from datetime import datetime
from typing import Dict, Optional
import aiosqlite
import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# ===================== КОНФИГУРАЦИЯ =====================
TELEGRAM_TOKEN = "8569814463:AAG3TWwMeIqIbn7SZY2VN3Kn7TJmq5JeJ04"
DB_NAME = "rss_bot.db"
CHECK_INTERVAL = 60
DEFAULT_MIN_MINOR = 1

# ===================== FSM СОСТОЯНИЯ =====================
class PatternStates(StatesGroup):
    adding_minor = State()
    adding_major = State()
    editing_minor = State()
    editing_major = State()
    setting_threshold = State()

# ===================== ОСНОВНЫЕ КЛАССЫ =====================
class UserConfig:
    """Конфигурация пользователя"""
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.is_parsing = False
        self.minor_patterns = [
            r"\bторгов(ый|ого|ом|ые|ых)?\s+центр(е|а|ов)?\b",
            r"\bтц\b",
            r"\bтрц\b",
            r"\bсет(ь|и|ью|ей|ям|ями|ях)\b(?:\s+\w+){0,3}\s+магазин(ов|а|ы)?\b",
            r"\bритейлер\b",
            r"\bсупермаркет\b",
            r"\bгипермаркет\b",
        ]
        self.major_patterns = [
            r"\bcommonwealth\b",
            r"\bcmwp\b",
            r"\bcbre\b",
            r"\binventive\s+retail\s+group\b",
        ]
        self.min_minor_required = DEFAULT_MIN_MINOR
        self.rss_url = "https://www.kommersant.ru/RSS/news.xml"
        self.last_checked: Optional[datetime] = None

    def to_dict(self) -> Dict:
        """Преобразование конфига в словарь для JSON"""
        return {
            'user_id': self.user_id,
            'is_parsing': self.is_parsing,
            'minor_patterns': self.minor_patterns,
            'major_patterns': self.major_patterns,
            'min_minor_required': self.min_minor_required,
            'rss_url': self.rss_url,
            # Преобразуем datetime в строку, если он есть
            'last_checked': self.last_checked.isoformat() if self.last_checked else None
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'UserConfig':
        """Создание конфига из словаря"""
        config = cls(data['user_id'])
        config.is_parsing = data.get('is_parsing', False)
        config.minor_patterns = data.get('minor_patterns', [])
        config.major_patterns = data.get('major_patterns', [])
        config.min_minor_required = data.get('min_minor_required', DEFAULT_MIN_MINOR)
        config.rss_url = data.get('rss_url', "https://www.kommersant.ru/RSS/news.xml")
        
        # Восстанавливаем datetime из строки
        last_checked_str = data.get('last_checked')
        if last_checked_str:
            try:
                config.last_checked = datetime.fromisoformat(last_checked_str)
            except (ValueError, TypeError):
                config.last_checked = None
        
        return config

class RSSBot:
    """Основной класс бота"""
    def __init__(self, bot: Bot):
        self.bot = bot
        self.user_configs: Dict[int, UserConfig] = {}
        self.parsing_tasks: Dict[int, asyncio.Task] = {}
        self.logger = logging.getLogger(__name__)
        self._init_keyboards()

    def _init_keyboards(self):
        """Инициализация клавиатур"""
        # Главное меню
        self.main_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Настройки паттернов", callback_data="menu_patterns")],
            [InlineKeyboardButton(text="▶️ Запустить парсинг", callback_data="start_parsing"),
             InlineKeyboardButton(text="⏸️ Остановить парсинг", callback_data="stop_parsing")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
             InlineKeyboardButton(text="ℹ️ Статус", callback_data="status")]
        ])
        
        # Меню паттернов
        self.patterns_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить минорный", callback_data="add_minor")],
            [InlineKeyboardButton(text="➕ Добавить мажорный", callback_data="add_major")],
            [InlineKeyboardButton(text="✏️ Редактировать минорные", callback_data="edit_minor")],
            [InlineKeyboardButton(text="✏️ Редактировать мажорные", callback_data="edit_major")],
            [InlineKeyboardButton(text="🎯 Настроить порог", callback_data="set_threshold")],
            [InlineKeyboardButton(text="📋 Показать все", callback_data="show_all")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
        ])

    def get_config(self, user_id: int) -> UserConfig:
        """Получить или создать конфиг пользователя"""
        if user_id not in self.user_configs:
            self.user_configs[user_id] = UserConfig(user_id)
        return self.user_configs[user_id]

    def check_patterns(self, text: str, config: UserConfig) -> Dict:
        """Проверка паттернов для пользователя"""
        if not text:
            return {'is_relevant': False, 'major_count': 0, 'minor_count': 0, 'matched_patterns': []}
            
        combined_text = text.lower()
        matched = []
        major_count = 0
        minor_count = 0
        
        for pattern in config.major_patterns:
            if re.search(pattern, combined_text, re.IGNORECASE):
                major_count += 1
                matched.append(f"MAJOR: {pattern}")
        
        for pattern in config.minor_patterns:
            if re.search(pattern, combined_text, re.IGNORECASE):
                minor_count += 1
                matched.append(f"MINOR: {pattern}")
        
        is_relevant = (major_count > 0) or (minor_count >= config.min_minor_required)
        
        return {
            'is_relevant': is_relevant,
            'major_count': major_count,
            'minor_count': minor_count,
            'matched_patterns': matched,
            'total_count': major_count + minor_count
        }

# ===================== БАЗА ДАННЫХ =====================
class Database:
    """Работа с базой данных"""
    
    @staticmethod
    async def init():
        """Инициализация БД"""
        async with aiosqlite.connect(DB_NAME) as db:
            # Таблица пользователей
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    config TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Таблица новостей (отдельно для каждого пользователя)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    guid TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    link TEXT NOT NULL,
                    published TIMESTAMP NOT NULL,
                    is_relevant BOOLEAN DEFAULT 0,
                    major_count INTEGER DEFAULT 0,
                    minor_count INTEGER DEFAULT 0,
                    sent_to_user BOOLEAN DEFAULT 0,
                    matched_patterns TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, guid),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            await db.commit()

    @staticmethod
    async def save_user_config(user_id: int, config: UserConfig):
        """Сохранение конфига пользователя"""
        async with aiosqlite.connect(DB_NAME) as db:
            config_json = json.dumps(config.to_dict())
            await db.execute(
                '''INSERT OR REPLACE INTO users (user_id, config, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)''',
                (user_id, config_json)
            )
            await db.commit()

    @staticmethod
    async def load_user_config(user_id: int) -> Optional[UserConfig]:
        """Загрузка конфига пользователя"""
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute(
                "SELECT config FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    data = json.loads(row[0])
                    return UserConfig.from_dict(data)
                except json.JSONDecodeError:
                    logging.error(f"Ошибка декодирования JSON для пользователя {user_id}")
                    return None
        return None

    @staticmethod
    async def save_news_item(user_id: int, entry, pattern_info: Dict) -> Optional[Dict]:
        """Сохранение новости для пользователя"""
        if not pattern_info['is_relevant']:
            return None
            
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                guid = entry.get('id', entry.link)
                patterns_str = '; '.join(pattern_info['matched_patterns']) if pattern_info['matched_patterns'] else ''
                
                # Преобразуем дату публикации в строку
                published = entry.get('published', '')
                if hasattr(published, 'isoformat'):
                    published_str = published.isoformat()
                else:
                    published_str = str(published)
                
                await db.execute(
                    '''INSERT OR IGNORE INTO user_news 
                       (user_id, guid, title, summary, link, published,
                        is_relevant, major_count, minor_count, matched_patterns)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (
                        user_id,
                        guid,
                        entry.get('title', ''),
                        entry.get('summary', ''),
                        entry.link,
                        published_str,
                        True,
                        pattern_info['major_count'],
                        pattern_info['minor_count'],
                        patterns_str
                    )
                )
                await db.commit()
                
                # Получаем ID вставленной записи
                cursor = await db.execute(
                    "SELECT id FROM user_news WHERE user_id = ? AND guid = ?",
                    (user_id, guid)
                )
                row = await cursor.fetchone()
                if row:
                    news_id = row[0]
                else:
                    return None
                
                return {
                    'id': news_id,
                    'guid': guid,
                    'title': entry.get('title', ''),
                    'summary': entry.get('summary', ''),
                    'link': entry.link,
                    'published': published_str,
                    'major_count': pattern_info['major_count'],
                    'minor_count': pattern_info['minor_count'],
                    'matched_patterns': pattern_info['matched_patterns']
                }
        except Exception as e:
            logging.error(f"Ошибка сохранения новости: {e}")
            return None
    
    @staticmethod
    async def mark_as_sent(news_id: int):
        """Пометить новость как отправленную"""
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE user_news SET sent_to_user = 1 WHERE id = ?",
                (news_id,)
            )
            await db.commit()

    @staticmethod
    async def get_user_stats(user_id: int) -> Dict:
        """Получить статистику пользователя"""
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM user_news WHERE user_id = ?",
                (user_id,)
            )
            total_row = await cursor.fetchone()
            total = total_row[0] if total_row else 0
            
            cursor = await db.execute(
                "SELECT COUNT(*) FROM user_news WHERE user_id = ? AND is_relevant = 1",
                (user_id,)
            )
            relevant_row = await cursor.fetchone()
            relevant = relevant_row[0] if relevant_row else 0
            
            cursor = await db.execute(
                "SELECT COUNT(*) FROM user_news WHERE user_id = ? AND sent_to_user = 1",
                (user_id,)
            )
            sent_row = await cursor.fetchone()
            sent = sent_row[0] if sent_row else 0
            
            cursor = await db.execute(
                "SELECT SUM(major_count), SUM(minor_count) FROM user_news WHERE user_id = ?",
                (user_id,)
            )
            pattern_stats = await cursor.fetchone()
            
            return {
                'total': total,
                'relevant': relevant,
                'sent': sent,
                'major_count': pattern_stats[0] if pattern_stats and pattern_stats[0] else 0,
                'minor_count': pattern_stats[1] if pattern_stats and pattern_stats[1] else 0
            }

# ===================== ОБРАБОТЧИКИ =====================
async def start_parsing_for_user(user_id: int, rss_bot: RSSBot):
    """Запуск парсинга для конкретного пользователя"""
    config = rss_bot.get_config(user_id)
    
    if config.is_parsing:
        return
    
    config.is_parsing = True
    await Database.save_user_config(user_id, config)
    
    async def parsing_loop():
        bot = rss_bot.bot
        logger = rss_bot.logger
        
        while config.is_parsing:
            try:
                logger.info(f"Проверка RSS для пользователя {user_id}")
                
                feed = feedparser.parse(config.rss_url)
                if feed.bozo:
                    logger.warning(f"Проблемы с RSS: {feed.bozo_exception}")
                
                for entry in feed.entries:
                    if not config.is_parsing:
                        break
                        
                    title = entry.get('title', '')
                    summary = entry.get('summary', '')
                    text = f"{title} {summary}"
                    
                    pattern_info = rss_bot.check_patterns(text, config)
                    
                    if pattern_info['is_relevant']:
                        news_item = await Database.save_news_item(user_id, entry, pattern_info)
                        if news_item:
                            await send_news_to_user(bot, user_id, news_item)
                            await Database.mark_as_sent(news_item['id'])
                    
                    await asyncio.sleep(0.1)
                
                config.last_checked = datetime.now()
                await Database.save_user_config(user_id, config)
                
            except Exception as e:
                logger.error(f"Ошибка парсинга для {user_id}: {e}")
            
            # Ожидание следующей проверки
            for _ in range(CHECK_INTERVAL):
                if not config.is_parsing:
                    break
                await asyncio.sleep(1)
    
    # Запускаем задачу
    task = asyncio.create_task(parsing_loop())
    rss_bot.parsing_tasks[user_id] = task

async def stop_parsing_for_user(user_id: int, rss_bot: RSSBot):
    """Остановка парсинга для пользователя"""
    config = rss_bot.get_config(user_id)
    config.is_parsing = False
    
    if user_id in rss_bot.parsing_tasks:
        rss_bot.parsing_tasks[user_id].cancel()
        del rss_bot.parsing_tasks[user_id]
    
    await Database.save_user_config(user_id, config)

async def send_news_to_user(bot: Bot, user_id: int, news_item: Dict):
    """Отправка новости пользователю"""
    try:
        relevance_info = ""
        if news_item['major_count'] > 0:
            relevance_info += f"🔴 **Мажорных паттернов: {news_item['major_count']}**\n"
        if news_item['minor_count'] > 0:
            relevance_info += f"🟡 Минорных паттернов: {news_item['minor_count']}\n"
        
        relevance_info += f"Всего совпадений: {news_item['major_count'] + news_item['minor_count']}\n"
        
        if news_item['matched_patterns']:
            patterns_preview = news_item['matched_patterns'][:3]
            patterns_text = "\n".join([p.replace('MAJOR: ', '• ').replace('MINOR: ', '• ') 
                                     for p in patterns_preview])
            if len(news_item['matched_patterns']) > 3:
                patterns_text += f"\n... и ещё {len(news_item['matched_patterns']) - 3}"
            relevance_info += f"\n📌 Найдены паттерны:\n{patterns_text}"
        
        # Добавляем дату публикации если есть
        date_info = ""
        if news_item.get('published'):
            try:
                if isinstance(news_item['published'], str):
                    date_info = f"\n📅 Опубликовано: {news_item['published'][:19]}"
            except Exception:
                pass
        
        message = (
            f"📰 *Новая релевантная новость*\n\n"
            f"*{news_item['title']}*\n"
            f"{date_info}\n\n"
            f"{news_item.get('summary', '')[:300]}...\n\n"
            f"{relevance_info}\n\n"
            f"🔗 [Читать полностью]({news_item['link']})"
        )
        
        await bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=False
        )
    except Exception as e:
        logging.error(f"Ошибка отправки пользователю {user_id}: {e}")

# ===================== ОСНОВНОЙ КОД =====================
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
rss_bot = RSSBot(bot)

# Загрузка конфигов при старте
async def load_all_configs():
    """Загрузка всех конфигов при старте"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id, config FROM users")
        rows = await cursor.fetchall()
        
        for row in rows:
            if row and len(row) >= 2:
                user_id, config_json = row
                if config_json:
                    try:
                        data = json.loads(config_json)
                        config = UserConfig.from_dict(data)
                        rss_bot.user_configs[user_id] = config
                        
                        # Восстанавливаем парсинг если он был активен
                        if config.is_parsing:
                            await start_parsing_for_user(user_id, rss_bot)
                    except json.JSONDecodeError:
                        logging.error(f"Ошибка декодирования JSON для пользователя {user_id}")

# ===================== ХЕНДЛЕРЫ =====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Стартовая команда"""
    user_id = message.from_user.id
    config = rss_bot.get_config(user_id)
    
    # Загружаем сохранённый конфиг если есть
    saved_config = await Database.load_user_config(user_id)
    if saved_config:
        rss_bot.user_configs[user_id] = saved_config
        config = saved_config
    
    await Database.save_user_config(user_id, config)
    
    welcome_text = (
        "*Добро пожаловать!*\n"
        "Используйте меню ниже для управления:"
    )
    
    await message.answer(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=rss_bot.main_kb
    )

@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: CallbackQuery):
    """Главное меню"""
    if callback.message:
        await callback.message.edit_text(
            "📱 *Главное меню*\n\n"
            "Выберите действие:",
            parse_mode="Markdown",
            reply_markup=rss_bot.main_kb
        )
    await callback.answer()

@dp.callback_query(F.data == "menu_patterns")
async def menu_patterns(callback: CallbackQuery):
    """Меню паттернов"""
    config = rss_bot.get_config(callback.from_user.id)
    
    text = (
        "⚙️ *Управление паттернами*\n\n"
        f"• Минорные паттерны: {len(config.minor_patterns)} шт\n"
        f"• Мажорные паттерны: {len(config.major_patterns)} шт\n"
        f"• Требуется минорных: {config.min_minor_required}\n\n"
        "Выберите действие:"
    )
    
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=rss_bot.patterns_kb
        )
    await callback.answer()

@dp.callback_query(F.data == "add_minor")
async def add_minor_pattern(callback: CallbackQuery, state: FSMContext):
    """Добавление минорного паттерна"""
    if callback.message:
        await callback.message.edit_text(
            "➕ *Добавление минорного паттерна*\n\n"
            "Отправьте мне регулярное выражение для минорного паттерна.\n"
            "Например: `\\bритейлер\\b`\n\n"
            "❌ Для отмены отправьте /cancel",
            parse_mode="Markdown"
        )
    await state.set_state(PatternStates.adding_minor)
    await callback.answer()

@dp.callback_query(F.data == "add_major")
async def add_major_pattern(callback: CallbackQuery, state: FSMContext):
    """Добавление мажорного паттерна"""
    if callback.message:
        await callback.message.edit_text(
            "➕ *Добавление мажорного паттерна*\n\n"
            "Отправьте мне регулярное выражение для мажорного паттерна.\n"
            "Например: `\\bcommonwealth\\b`\n\n"
            "❌ Для отмены отправьте /cancel",
            parse_mode="Markdown"
        )
    await state.set_state(PatternStates.adding_major)
    await callback.answer()

@dp.message(Command("cancel"))
@dp.message(F.text.casefold() == "cancel")
async def cancel_handler(message: Message, state: FSMContext):
    """Отмена текущего действия"""
    current_state = await state.get_state()
    if current_state is None:
        return
    
    await state.clear()
    await message.answer(
        "❌ Действие отменено",
        reply_markup=rss_bot.main_kb
    )

@dp.message(PatternStates.adding_minor)
async def process_minor_pattern(message: Message, state: FSMContext):
    """Обработка нового минорного паттерна"""
    pattern = message.text.strip() if message.text else ""
    user_id = message.from_user.id
    config = rss_bot.get_config(user_id)
    
    # Проверка валидности regex
    try:
        re.compile(pattern)
    except re.error:
        await message.answer(
            "❌ Неверное регулярное выражение. Попробуйте еще раз.",
            reply_markup=rss_bot.patterns_kb
        )
        return
    
    config.minor_patterns.append(pattern)
    await Database.save_user_config(user_id, config)
    
    await message.answer(
        f"✅ Минорный паттерн добавлен!\n\n"
        f"Теперь у вас {len(config.minor_patterns)} минорных паттернов.",
        reply_markup=rss_bot.patterns_kb
    )
    await state.clear()

@dp.message(PatternStates.adding_major)
async def process_major_pattern(message: Message, state: FSMContext):
    """Обработка нового мажорного паттерна"""
    pattern = message.text.strip() if message.text else ""
    user_id = message.from_user.id
    config = rss_bot.get_config(user_id)
    
    try:
        re.compile(pattern)
    except re.error:
        await message.answer(
            "❌ Неверное регулярное выражение. Попробуйте еще раз.",
            reply_markup=rss_bot.patterns_kb
        )
        return
    
    config.major_patterns.append(pattern)
    await Database.save_user_config(user_id, config)
    
    await message.answer(
        f"✅ Мажорный паттерн добавлен!\n\n"
        f"Теперь у вас {len(config.major_patterns)} мажорных паттернов.",
        reply_markup=rss_bot.patterns_kb
    )
    await state.clear()

@dp.callback_query(F.data == "edit_minor")
async def edit_minor_patterns(callback: CallbackQuery, state: FSMContext):
    """Редактирование минорных паттернов"""
    config = rss_bot.get_config(callback.from_user.id)
    
    if not config.minor_patterns:
        if callback.message:
            await callback.message.edit_text(
                "📝 У вас пока нет минорных паттернов для редактирования.",
                reply_markup=rss_bot.patterns_kb
            )
        await callback.answer()
        return
    
    # Создаём клавиатуру с чекбоксами
    keyboard = []
    for i, pattern in enumerate(config.minor_patterns):
        keyboard.append([
            InlineKeyboardButton(
                text=f"{i+1}. {pattern[:30]}...",
                callback_data=f"toggle_minor_{i}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="❌ Удалить выбранные", callback_data="delete_minor")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_patterns")])
    
    if callback.message:
        await callback.message.edit_text(
            "✏️ *Редактирование минорных паттернов*\n\n"
            "Выберите паттерны для удаления:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    
    await state.update_data(selected_minors=[])
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_minor_"))
async def toggle_minor_pattern(callback: CallbackQuery, state: FSMContext):
    """Переключение выбора минорного паттерна"""
    try:
        pattern_index = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка обработки запроса")
        return
    
    data = await state.get_data()
    selected = data.get("selected_minors", [])
    
    if pattern_index in selected:
        selected.remove(pattern_index)
    else:
        selected.append(pattern_index)
    
    await state.update_data(selected_minors=selected)
    
    # Обновляем текст кнопки
    config = rss_bot.get_config(callback.from_user.id)
    keyboard = []
    for i, pattern in enumerate(config.minor_patterns):
        prefix = "✅ " if i in selected else "☐ "
        keyboard.append([
            InlineKeyboardButton(
                text=f"{prefix}{i+1}. {pattern[:30]}...",
                callback_data=f"toggle_minor_{i}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="❌ Удалить выбранные", callback_data="delete_minor")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_patterns")])
    
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    await callback.answer()

@dp.callback_query(F.data == "delete_minor")
async def delete_selected_minors(callback: CallbackQuery, state: FSMContext):
    """Удаление выбранных минорных паттернов"""
    data = await state.get_data()
    selected = data.get("selected_minors", [])
    
    if not selected:
        await callback.answer("❌ Ничего не выбрано")
        return
    
    user_id = callback.from_user.id
    config = rss_bot.get_config(user_id)
    
    # Удаляем в обратном порядке, чтобы индексы не сдвигались
    for index in sorted(selected, reverse=True):
        if index < len(config.minor_patterns):
            config.minor_patterns.pop(index)
    
    await Database.save_user_config(user_id, config)
    await state.clear()
    
    if callback.message:
        await callback.message.edit_text(
            f"✅ Удалено {len(selected)} минорных паттернов.\n"
            f"Осталось: {len(config.minor_patterns)}",
            reply_markup=rss_bot.patterns_kb
        )
    await callback.answer()

@dp.callback_query(F.data == "edit_major")
async def edit_major_patterns(callback: CallbackQuery, state: FSMContext):
    """Редактирование мажорных паттернов"""
    config = rss_bot.get_config(callback.from_user.id)
    
    if not config.major_patterns:
        if callback.message:
            await callback.message.edit_text(
                "📝 У вас пока нет мажорных паттернов для редактирования.",
                reply_markup=rss_bot.patterns_kb
            )
        await callback.answer()
        return
    
    keyboard = []
    for i, pattern in enumerate(config.major_patterns):
        keyboard.append([
            InlineKeyboardButton(
                text=f"{i+1}. {pattern[:30]}...",
                callback_data=f"toggle_major_{i}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="❌ Удалить выбранные", callback_data="delete_major")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_patterns")])
    
    if callback.message:
        await callback.message.edit_text(
            "✏️ *Редактирование мажорных паттернов*\n\n"
            "Выберите паттерны для удаления:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    
    await state.update_data(selected_majors=[])
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_major_"))
async def toggle_major_pattern(callback: CallbackQuery, state: FSMContext):
    """Переключение выбора мажорного паттерна"""
    try:
        pattern_index = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка обработки запроса")
        return
    
    data = await state.get_data()
    selected = data.get("selected_majors", [])
    
    if pattern_index in selected:
        selected.remove(pattern_index)
    else:
        selected.append(pattern_index)
    
    await state.update_data(selected_majors=selected)
    
    config = rss_bot.get_config(callback.from_user.id)
    keyboard = []
    for i, pattern in enumerate(config.major_patterns):
        prefix = "✅ " if i in selected else "☐ "
        keyboard.append([
            InlineKeyboardButton(
                text=f"{prefix}{i+1}. {pattern[:30]}...",
                callback_data=f"toggle_major_{i}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="❌ Удалить выбранные", callback_data="delete_major")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_patterns")])
    
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    await callback.answer()

@dp.callback_query(F.data == "delete_major")
async def delete_selected_majors(callback: CallbackQuery, state: FSMContext):
    """Удаление выбранных мажорных паттернов"""
    data = await state.get_data()
    selected = data.get("selected_majors", [])
    
    if not selected:
        await callback.answer("❌ Ничего не выбрано")
        return
    
    user_id = callback.from_user.id
    config = rss_bot.get_config(user_id)
    
    for index in sorted(selected, reverse=True):
        if index < len(config.major_patterns):
            config.major_patterns.pop(index)
    
    await Database.save_user_config(user_id, config)
    await state.clear()
    
    if callback.message:
        await callback.message.edit_text(
            f"✅ Удалено {len(selected)} мажорных паттернов.\n"
            f"Осталось: {len(config.major_patterns)}",
            reply_markup=rss_bot.patterns_kb
        )
    await callback.answer()

@dp.callback_query(F.data == "set_threshold")
async def set_threshold(callback: CallbackQuery, state: FSMContext):
    """Настройка порога минорных паттернов"""
    config = rss_bot.get_config(callback.from_user.id)
    
    if callback.message:
        await callback.message.edit_text(
            "🎯 *Настройка порога минорных паттернов*\n\n"
            f"Текущее значение: {config.min_minor_required}\n"
            f"У вас {len(config.minor_patterns)} минорных паттернов.\n\n"
            "Отправьте мне новое число (например: 2)\n\n"
            "❌ Для отмены отправьте /cancel",
            parse_mode="Markdown"
        )
    await state.set_state(PatternStates.setting_threshold)
    await callback.answer()

@dp.message(PatternStates.setting_threshold)
async def process_threshold(message: Message, state: FSMContext):
    """Обработка нового порога"""
    try:
        threshold = int(message.text.strip()) if message.text else 0
        if threshold < 1:
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Неверное значение. Введите число больше 0.",
            reply_markup=rss_bot.patterns_kb
        )
        return
    
    user_id = message.from_user.id
    config = rss_bot.get_config(user_id)
    config.min_minor_required = threshold
    
    await Database.save_user_config(user_id, config)
    
    await message.answer(
        f"✅ Порог установлен: {threshold}\n\n"
        f"Теперь новость будет считаться релевантной если:\n"
        f"• Есть ≥1 мажорный паттерн ИЛИ\n"
        f"• Есть ≥{threshold} минорных паттернов",
        reply_markup=rss_bot.patterns_kb
    )
    await state.clear()

@dp.callback_query(F.data == "show_all")
async def show_all_patterns(callback: CallbackQuery):
    """Показать все паттерны"""
    config = rss_bot.get_config(callback.from_user.id)
    
    text = "📋 *Все ваши паттерны*\n\n"
    
    if config.major_patterns:
        text += "🔴 *Мажорные паттерны:*\n"
        for i, pattern in enumerate(config.major_patterns, 1):
            text += f"{i}. `{pattern}`\n"
        text += "\n"
    else:
        text += "🔴 Мажорные паттерны: нет\n\n"
    
    if config.minor_patterns:
        text += "🟡 *Минорные паттерны:*\n"
        for i, pattern in enumerate(config.minor_patterns, 1):
            text += f"{i}. `{pattern}`\n"
        text += "\n"
    else:
        text += "🟡 Минорные паттерны: нет\n\n"
    
    text += f"🎯 *Порог минорных:* {config.min_minor_required}\n"
    text += f"🌐 *RSS лента:* {config.rss_url}"
    
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=rss_bot.patterns_kb
        )
    await callback.answer()

@dp.callback_query(F.data == "start_parsing")
async def start_parsing_handler(callback: CallbackQuery):
    """Запуск парсинга"""
    user_id = callback.from_user.id
    config = rss_bot.get_config(user_id)
    
    if config.is_parsing:
        await callback.answer("✅ Парсинг уже запущен")
        return
    
    await start_parsing_for_user(user_id, rss_bot)
    if callback.message:
        await callback.message.edit_text(
            "▶️ *Парсинг запущен!*\n\n"
            "Теперь я буду проверять RSS ленту и присылать вам релевантные новости.\n\n"
            f"Настройки:\n"
            f"• Минорные паттерны: {len(config.minor_patterns)}\n"
            f"• Мажорные паттерны: {len(config.major_patterns)}\n"
            f"• Порог: {config.min_minor_required}\n"
            f"• RSS: {config.rss_url}",
            parse_mode="Markdown",
            reply_markup=rss_bot.main_kb
        )
    await callback.answer()

@dp.callback_query(F.data == "stop_parsing")
async def stop_parsing_handler(callback: CallbackQuery):
    """Остановка парсинга"""
    user_id = callback.from_user.id
    config = rss_bot.get_config(user_id)
    
    if not config.is_parsing:
        await callback.answer("⏸️ Парсинг уже остановлен")
        return
    
    await stop_parsing_for_user(user_id, rss_bot)
    if callback.message:
        await callback.message.edit_text(
            "⏸️ *Парсинг остановлен!*\n\n"
            "Я больше не буду проверять RSS ленту для вас.\n\n"
            "Вы можете:\n"
            "• Изменить паттерны и снова запустить\n"
            "• Просмотреть статистику\n"
            "• Изменить настройки",
            parse_mode="Markdown",
            reply_markup=rss_bot.main_kb
        )
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    """Показать статистику"""
    user_id = callback.from_user.id
    stats = await Database.get_user_stats(user_id)
    config = rss_bot.get_config(user_id)
    
    status = "▶️ Активен" if config.is_parsing else "⏸️ Остановлен"
    
    text = (
        f"📊 *Ваша статистика*\n\n"
        f"Статус: {status}\n"
        f"Всего новостей: {stats['total']}\n"
        f"Релевантных: {stats['relevant']}\n"
        f"Отправлено вам: {stats['sent']}\n"
        f"Найдено паттернов:\n"
        f"• Мажорных: {stats['major_count']}\n"
        f"• Минорных: {stats['minor_count']}\n\n"
        f"*Текущие настройки:*\n"
        f"• Минорных паттернов: {len(config.minor_patterns)}\n"
        f"• Мажорных паттернов: {len(config.major_patterns)}\n"
        f"• Требуется минорных: {config.min_minor_required}\n"
        f"• RSS лента: {config.rss_url}"
    )
    
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=rss_bot.main_kb
        )
    await callback.answer()

@dp.callback_query(F.data == "status")
async def show_status(callback: CallbackQuery):
    """Показать статус"""
    user_id = callback.from_user.id
    config = rss_bot.get_config(user_id)
    
    status = "🟢 *Активен*" if config.is_parsing else "🔴 *Остановлен*"
    last_check = config.last_checked.strftime("%H:%M:%S") if config.last_checked else "никогда"
    
    text = (
        f"ℹ️ *Статус системы*\n\n"
        f"Парсинг: {status}\n"
        f"Последняя проверка: {last_check}\n"
        f"Интервал: {CHECK_INTERVAL} сек\n\n"
        f"*Ресурсы:*\n"
        f"• Память конфигов: {len(rss_bot.user_configs)} пользователей\n"
        f"• Активные задачи: {len(rss_bot.parsing_tasks)}\n\n"
        f"*Ваши настройки:*\n"
        f"• Паттерны: {len(config.minor_patterns)} минорных, "
        f"{len(config.major_patterns)} мажорных\n"
        f"• Порог: {config.min_minor_required}\n"
        f"• RSS: {config.rss_url}"
    )
    
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=rss_bot.main_kb
        )
    await callback.answer()

async def main():
    """Основная функция"""
    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('rss_bot.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info("Запуск бота...")
    
    # Инициализация БД
    await Database.init()
    
    # Загрузка сохранённых конфигов
    await load_all_configs()
    logger.info(f"Загружено {len(rss_bot.user_configs)} конфигов пользователей")
    
    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
