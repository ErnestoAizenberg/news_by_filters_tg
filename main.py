import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from typing import Dict, List, Optional

import aiosqlite
import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

# ===================== КОНФИГУРАЦИЯ =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN enviroment variable is not set")

DB_NAME = "rss_bot.db"
CHECK_INTERVAL = 300
DEFAULT_MIN_MINOR = 1
RSS_URL = "https://www.kommersant.ru/RSS/news.xml"


# ===================== ГЛОБАЛЬНЫЕ НАСТРОЙКИ =====================
class Settings:
    minor_patterns = [
        r"\bторгов(ый|ого|ом|ые|ых)?\s+центр(е|а|ов)?\b",
        r"\bтц\b",
        r"\bтрц\b",
        r"\bсет(ь|и|ью|ей|ям|ями|ях)\b(?:\s+\w+){0,3}\s+магазин(ов|а|ы)?\b",
        r"\bритейлер\b",
        r"\bсупермаркет\b",
        r"\bгипермаркет\b",
        r"\bарендодател\b",
        r"\bарендатор\b",
        r"\bдевелопер\b",
        r"\bфудкорт\b",
        r"\bфудхолл\b",
        r"\bвыручк\b",
        r"\bпосещаемост\b",
        r"\bмаркетплейс\b",
        r"\bребрендинг\b",
        r"\bонлайн[-\s]?продаж\b",
        r"\bофлайн\s+продаж\b",
        r"\bбренд\b",
        r"\bбутик\b",
        r"fashion\S*",
        r"\bроссийски(ий|ого|иму|им|е)\s+рын(ок|а|ку)\b",
        r"\bторгов(ая|ой)\s+недвижимост\b",
        r"\bкоммерческ(ая|ой)?\s+недвижимост\b",
    ]

    major_patterns = [
        r"\bcommonwealth\b",
        r"\bcmwp\b",
        r"\bcbre\b",
        r"\binventive\s+retail\s+group\b",
        r"\binditex\b",
        r"\blpp\b",
    ]
    min_minor_required = DEFAULT_MIN_MINOR
    rss_url = RSS_URL
    last_checked: Optional[datetime] = None


settings = Settings()

parsing_task: Optional[asyncio.Task] = None


# ===================== БАЗА ДАННЫХ =====================
class Database:
    @staticmethod
    async def init():
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS global_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    config TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guid TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    link TEXT NOT NULL,
                    published TIMESTAMP NOT NULL,
                    is_relevant BOOLEAN DEFAULT 0,
                    major_count INTEGER DEFAULT 0,
                    minor_count INTEGER DEFAULT 0,
                    matched_patterns TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    @staticmethod
    async def load_settings():
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute("SELECT config FROM global_settings WHERE id = 1")
            row = await cursor.fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                settings.minor_patterns = data.get(
                    "minor_patterns", settings.minor_patterns
                )
                settings.major_patterns = data.get(
                    "major_patterns", settings.major_patterns
                )
                settings.min_minor_required = data.get(
                    "min_minor_required", DEFAULT_MIN_MINOR
                )
                settings.rss_url = data.get("rss_url", RSS_URL)
                last = data.get("last_checked")
                if last:
                    try:
                        settings.last_checked = datetime.fromisoformat(last)
                    except Exception as e:
                        print(f"Exception is ignored at load_settings: {e}")
                        settings.last_checked = None

    @staticmethod
    async def save_settings():
        async with aiosqlite.connect(DB_NAME) as db:
            data = {
                "minor_patterns": settings.minor_patterns,
                "major_patterns": settings.major_patterns,
                "min_minor_required": settings.min_minor_required,
                "rss_url": settings.rss_url,
                "last_checked": settings.last_checked.isoformat()
                if settings.last_checked
                else None,
            }
            await db.execute(
                "INSERT OR REPLACE INTO global_settings (id, config) VALUES (1, ?)",
                (json.dumps(data),),
            )
            await db.commit()

    @staticmethod
    async def save_news(entry, pattern_info: Dict) -> bool:
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                guid = entry.get("id", entry.link)
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                link = entry.get("link", "")
                published = entry.get("published", "")
                if hasattr(published, "isoformat"):
                    published = published.isoformat()
                else:
                    published = str(published)

                patterns_str = (
                    "; ".join(pattern_info["matched_patterns"])
                    if pattern_info["matched_patterns"]
                    else ""
                )

                await db.execute(
                    """
                    INSERT OR IGNORE INTO news 
                    (guid, title, summary, link, published, is_relevant, major_count, minor_count, matched_patterns)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        guid,
                        title,
                        summary,
                        link,
                        published,
                        pattern_info["is_relevant"],
                        pattern_info["major_count"],
                        pattern_info["minor_count"],
                        patterns_str,
                    ),
                )
                await db.commit()
                return True
        except Exception as e:
            logging.error(f"Ошибка сохранения новости: {e}")
            return False

    @staticmethod
    async def get_digest(period: str) -> List[Dict]:
        async with aiosqlite.connect(DB_NAME) as db:
            date_filter = ""
            if period == "today":
                date_filter = "AND published >= date('now', '-1 day')"
            elif period == "week":
                date_filter = "AND published >= date('now', '-7 days')"
            elif period == "month":
                date_filter = "AND published >= date('now', '-30 days')"

            query = f"""
                SELECT title, summary, link, published, major_count, minor_count, matched_patterns
                FROM news
                WHERE is_relevant = 1 {date_filter}
                ORDER BY published DESC
                LIMIT 50
            """
            cursor = await db.execute(query)
            rows = await cursor.fetchall()
            return [
                {
                    "title": r[0],
                    "summary": r[1],
                    "link": r[2],
                    "published": r[3],
                    "major_count": r[4],
                    "minor_count": r[5],
                    "matched_patterns": r[6].split("; ") if r[6] else [],
                }
                for r in rows
            ]

    @staticmethod
    async def get_stats():
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM news WHERE is_relevant = 1")
            row = await cursor.fetchone()
            total = row[0] if row else 0

            cursor = await db.execute("""
                SELECT 
                    SUM(CASE WHEN published >= date('now', '-1 day') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN published >= date('now', '-7 days') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN published >= date('now', '-30 days') THEN 1 ELSE 0 END)
                FROM news WHERE is_relevant = 1
            """)
            row = await cursor.fetchone()
            today, week, month = row if row else (0, 0, 0)

            cursor = await db.execute(
                "SELECT SUM(major_count), SUM(minor_count) FROM news WHERE is_relevant = 1"
            )
            row = await cursor.fetchone()
            major_sum, minor_sum = row if row else (0, 0)

            return {
                "total": total,
                "today": today or 0,
                "week": week or 0,
                "month": month or 0,
                "major_count": major_sum or 0,
                "minor_count": minor_sum or 0,
            }


# ===================== ПРОВЕРКА ПАТТЕРНОВ =====================
def check_patterns(text: str) -> Dict:
    if not text:
        return {
            "is_relevant": False,
            "major_count": 0,
            "minor_count": 0,
            "matched_patterns": [],
        }
    text = text.lower()
    matched = []
    major = 0
    minor = 0

    for p in settings.major_patterns:
        if re.search(p, text, re.IGNORECASE):
            major += 1
            matched.append(f"MAJOR: {p}")
    for p in settings.minor_patterns:
        if re.search(p, text, re.IGNORECASE):
            minor += 1
            matched.append(f"MINOR: {p}")

    return {
        "is_relevant": (major > 0) or (minor >= settings.min_minor_required),
        "major_count": major,
        "minor_count": minor,
        "matched_patterns": matched,
    }


# ===================== ПАРСИНГ RSS =====================
async def parse_feed():
    logging.info("Парсинг RSS...")
    try:
        feed = feedparser.parse(settings.rss_url)
        if feed.bozo:
            logging.warning(f"Bozo: {feed.bozo_exception}")

        async with aiosqlite.connect(DB_NAME) as db:
            for entry in feed.entries:
                cursor = await db.execute(
                    "SELECT id FROM news WHERE guid = ?", (entry.get("id", entry.link),)
                )
                if await cursor.fetchone():
                    continue

                text = f"{entry.get('title', '')} {entry.get('summary', '')}"
                info = check_patterns(text)
                await Database.save_news(entry, info)

        settings.last_checked = datetime.now()
        await Database.save_settings()
    except Exception as e:
        logging.error(f"Ошибка парсинга: {e}")


async def parsing_loop():
    global parsing_task
    logging.info("Запуск цикла парсинга")
    while True:
        await parse_feed()
        for _ in range(CHECK_INTERVAL):
            await asyncio.sleep(1)


def restart_parsing():
    global parsing_task
    if parsing_task and not parsing_task.done():
        parsing_task.cancel()
    parsing_task = asyncio.create_task(parsing_loop())


# ===================== FSM СОСТОЯНИЯ =====================
class PatternStates(StatesGroup):
    add_minor = State()
    add_major = State()
    set_threshold = State()
    delete_pattern = State()


# ===================== КЛАВИАТУРЫ =====================
main_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⚙️ Настройки паттернов", callback_data="menu_patterns"
            )
        ],
        [
            InlineKeyboardButton(
                text="📰 Получить дайджест", callback_data="digest_menu"
            )
        ],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
    ]
)

patterns_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Минорный", callback_data="add_minor"),
            InlineKeyboardButton(text="➕ Мажорный", callback_data="add_major"),
        ],
        [InlineKeyboardButton(text="❌ Удалить паттерн", callback_data="delete_menu")],
        [InlineKeyboardButton(text="🎯 Порог", callback_data="set_threshold")],
        [InlineKeyboardButton(text="📋 Показать все", callback_data="show_all")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")],
    ]
)

digest_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇾 Сегодня", callback_data="digest_today"),
            InlineKeyboardButton(text="🇼 Неделя", callback_data="digest_week"),
        ],
        [
            InlineKeyboardButton(text="🇲 Месяц", callback_data="digest_month"),
            InlineKeyboardButton(text="📅 Всё", callback_data="digest_all"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")],
    ]
)

# ===================== ХЕНДЛЕРЫ =====================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🌟 *RSS коллектор*\n\n"
        "Я собираю новости из RSS, фильтрую по общим паттернам.\n"
        "Настройки едины для всех. Дайджест запрашиваешь сам.\n\n"
        "Выбери действие:",
        parse_mode="Markdown",
        reply_markup=main_kb,
    )


@dp.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery):
    message = callback.message

    if isinstance(message, Message):
        await message.edit_text(
            "📱 *Главное меню*", parse_mode="Markdown", reply_markup=main_kb
        )
        await callback.answer()
    elif isinstance(message, InaccessibleMessage):
        return


# ---------- Настройки паттернов ----------
@dp.callback_query(F.data == "menu_patterns")
async def menu_patterns(callback: CallbackQuery):
    text = (
        f"⚙️ *Паттерны (общие)*\n\n"
        f"🔴 Мажорных: {len(settings.major_patterns)}\n"
        f"🟡 Минорных: {len(settings.minor_patterns)}\n"
        f"🎯 Порог: {settings.min_minor_required}"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text, parse_mode="Markdown", reply_markup=patterns_kb
        )
        await callback.answer()
    else:
        pass


@dp.callback_query(F.data == "add_minor")
async def add_minor_cb(callback: CallbackQuery, state: FSMContext):
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "➕ *Добавление минорного паттерна*\nОтправь регулярное выражение.\n❌ /cancel",
            parse_mode="Markdown",
        )
        await state.set_state(PatternStates.add_minor)
        await callback.answer()


@dp.callback_query(F.data == "add_major")
async def add_major_cb(callback: CallbackQuery, state: FSMContext):
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "➕ *Добавление мажорного паттерна*\nОтправь регулярное выражение.\n❌ /cancel",
            parse_mode="Markdown",
        )
        await state.set_state(PatternStates.add_major)
        await callback.answer()


@dp.message(PatternStates.add_minor)
async def process_add_minor(message: Message, state: FSMContext):

    if not message.text:
        await message.answer(
            "❌ Пустой паттерн. Отправь текст с регулярным выражением."
        )
        return

    pattern: str = message.text.strip()

    try:
        re.compile(pattern)
    except re.error:
        await message.answer("❌ Некорректное регулярное выражение. Попробуй снова.")
        return
    settings.minor_patterns.append(pattern)
    await Database.save_settings()
    restart_parsing()
    await state.clear()
    await message.answer(
        f"✅ Минорный паттерн добавлен. Всего: {len(settings.minor_patterns)}",
        reply_markup=patterns_kb,
    )


@dp.message(PatternStates.add_major)
async def process_add_major(message: Message, state: FSMContext):
    pattern = ""
    if message.text:
        pattern = message.text.strip()

    try:
        re.compile(pattern)
    except re.error:
        await message.answer("❌ Некорректное регулярное выражение. Попробуй снова.")
        return
    settings.major_patterns.append(pattern)
    await Database.save_settings()
    restart_parsing()
    await state.clear()
    await message.answer(
        f"✅ Мажорный паттерн добавлен. Всего: {len(settings.major_patterns)}",
        reply_markup=patterns_kb,
    )


# ---------- Удаление паттернов ----------
@dp.callback_query(F.data == "delete_menu")
async def delete_menu(callback: CallbackQuery, state: FSMContext):
    kb_buttons = []
    if settings.major_patterns:
        kb_buttons.append(
            [
                InlineKeyboardButton(
                    text="🔴 Удалить мажорный", callback_data="delete_major"
                )
            ]
        )
    if settings.minor_patterns:
        kb_buttons.append(
            [
                InlineKeyboardButton(
                    text="🟡 Удалить минорный", callback_data="delete_minor"
                )
            ]
        )
    kb_buttons.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_patterns")]
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "❌ *Удаление паттернов*\nВыбери тип для удаления:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
        )
        await callback.answer()


async def delete_pattern_flow(
    callback: CallbackQuery, pattern_type: str, state: FSMContext
):
    patterns = (
        settings.major_patterns if pattern_type == "major" else settings.minor_patterns
    )
    if not patterns:
        await callback.answer("Нет паттернов для удаления", show_alert=True)
        return

    buttons = []
    for i, p in enumerate(patterns):
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{i + 1}. {p[:40]}...",
                    callback_data=f"del_{pattern_type}_{i}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu_patterns")]
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Выбери {pattern_type} паттерн для удаления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await state.update_data(del_type=pattern_type)
        await state.set_state(PatternStates.delete_pattern)
        await callback.answer()


@dp.callback_query(F.data == "delete_major")
async def delete_major_cb(callback: CallbackQuery, state: FSMContext):
    await delete_pattern_flow(callback, "major", state)


@dp.callback_query(F.data == "delete_minor")
async def delete_minor_cb(callback: CallbackQuery, state: FSMContext):
    await delete_pattern_flow(callback, "minor", state)


@dp.callback_query(PatternStates.delete_pattern, F.data.startswith("del_"))
async def delete_pattern_execute(callback: CallbackQuery, state: FSMContext):
    if callback.data is None:
        print("Callback data is missing at delete_pattern_execute, skipping...")
        return

    _, typ, idx_str = callback.data.split("_", maxsplit=2)
    idx = int(idx_str)
    data = await state.get_data()
    if data.get("del_type") != typ:
        await callback.answer("Ошибка", show_alert=True)
        return
    patterns = settings.major_patterns if typ == "major" else settings.minor_patterns
    if 0 <= idx < len(patterns):
        deleted = patterns.pop(idx)
        await Database.save_settings()
        restart_parsing()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"✅ Удалён: `{deleted}`",
                parse_mode="Markdown",
                reply_markup=patterns_kb,
            )
        else:
            pass
    else:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "❌ Паттерн не найден", reply_markup=patterns_kb
            )
        else:
            pass

    await state.clear()
    await callback.answer()


# ---------- Порог ----------
@dp.callback_query(F.data == "set_threshold")
async def set_threshold_cb(callback: CallbackQuery, state: FSMContext):
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"🎯 *Порог минорных*\nТекущее: {settings.min_minor_required}\n"
            "Отправь новое число (>=1):\n❌ /cancel",
            parse_mode="Markdown",
        )
        await state.set_state(PatternStates.set_threshold)
        await callback.answer()


@dp.message(PatternStates.set_threshold)
async def process_threshold(message: Message, state: FSMContext):
    if message.text is None:
        await message.answer("Напишите что-нибудь...")
        return

    try:
        val = int(message.text.strip())
        if val < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи целое число >=1")
        return

    settings.min_minor_required = val
    await Database.save_settings()
    restart_parsing()
    await state.clear()
    await message.answer(f"✅ Порог установлен: {val}", reply_markup=patterns_kb)


# ---------- Показать все ----------
@dp.callback_query(F.data == "show_all")
async def show_all(callback: CallbackQuery):
    # Не редактируем исходное сообщение, а отправляем новое
    text = "📋 *Все паттерны*\n\n"
    text += "🔴 *Мажорные:*\n"
    if settings.major_patterns:
        for i, p in enumerate(settings.major_patterns, 1):
            text += f"{i}. `{p}`\n"
    else:
        text += "—\n"
    text += "\n🟡 *Минорные:*\n"
    if settings.minor_patterns:
        for i, p in enumerate(settings.minor_patterns, 1):
            text += f"{i}. `{p}`\n"
    else:
        text += "—\n"
    text += f"\n🎯 *Порог:* {settings.min_minor_required}"

    await callback.answer()  # сразу отвечаем, чтобы убрать "часики"
    if callback.message:
        await callback.message.answer(text, parse_mode="Markdown")


# ---------- Дайджест ----------
@dp.callback_query(F.data == "digest_menu")
async def digest_menu_cb(callback: CallbackQuery):
    stats = await Database.get_stats()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"📰 *Дайджест*\n\n"
            f"📊 Всего: {stats['total']}\n"
            f"• За сегодня: {stats['today']}\n"
            f"• За неделю: {stats['week']}\n"
            f"• За месяц: {stats['month']}",
            parse_mode="Markdown",
            reply_markup=digest_kb,
        )
        await callback.answer()


@dp.callback_query(F.data.startswith("digest_"))
async def send_digest(callback: CallbackQuery):
    if callback.data is None:
        await callback.answer("❌ Ошибка: данные не найдены")
        return

    period = callback.data.replace("digest_", "")
    await callback.answer("🔍 Формирую дайджест...")
    news_list = await Database.get_digest(period)

    if not news_list and callback.message:
        await callback.message.answer("📭 Новостей за этот период нет.")
        return

    period_names = {
        "today": "СЕГОДНЯ",
        "week": "НЕДЕЛЯ",
        "month": "МЕСЯЦ",
        "all": "ВСЕ",
    }
    name = period_names.get(period, "")

    if len(news_list) > 0:
        content = f"ДАЙДЖЕСТ {name}\n"
        content += f"Всего новостей: {len(news_list)}\n"
        content += "=" * 50 + "\n\n"

        for i, news in enumerate(news_list, 1):
            published = news.get("published", "")
            if published:
                try:
                    dt = datetime.fromisoformat(published)
                    date_str = dt.strftime("%d.%m.%Y %H:%M")
                except Exception as e:
                    print(f"Exception is ignored at send_digest: {e}")
                    date_str = published[:16]
            else:
                date_str = "Неизвестно"

            content += f"Новость #{i}\n"
            content += f"Дата: {date_str}\n"
            content += f"Заголовок: {news['title']}\n"
            content += f"Описание: {news['summary'][:300]}...\n"
            content += f"Ссылка: {news['link']}\n"
            if news["major_count"] > 0 or news["minor_count"] > 0:
                content += f"Паттерны: мажорных={news['major_count']}, минорных={news['minor_count']}\n"
            if news["matched_patterns"]:
                content += f"Совпадения: {', '.join(news['matched_patterns'][:3])}"
                if len(news["matched_patterns"]) > 3:
                    content += f" и ещё {len(news['matched_patterns']) - 3}"
                content += "\n"
            content += "-" * 50 + "\n\n"

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as f:
            f.write(content)
            tmp_path = f.name

        try:
            document = FSInputFile(tmp_path, filename=f"digest_{period}.txt")
            if callback.message:
                await callback.message.answer_document(
                    document,
                    caption=f"📰 *Дайджест {name}* ({len(news_list)} нов.)",
                    parse_mode="Markdown",
                )
        finally:
            os.unlink(tmp_path)

    if len(news_list) <= 5:
        if callback.message:
            await callback.message.answer(
                f"📰 *ДАЙДЖЕСТ {name}* — {len(news_list)}", parse_mode="Markdown"
            )
            for news in news_list:
                if news["major_count"] > 0:
                    emoji = "🔴"
                elif news["minor_count"] >= 3:
                    emoji = "🟠"
                else:
                    emoji = "🟡"

                patterns_desc = []
                if news["major_count"]:
                    patterns_desc.append(f"маж: {news['major_count']}")
                if news["minor_count"]:
                    patterns_desc.append(f"мин: {news['minor_count']}")
                pat_str = f"({', '.join(patterns_desc)})" if patterns_desc else ""

                msg = (
                    f"{emoji} *{news['title']}*\n"
                    f"{news['summary'][:200]}...\n"
                    f"{pat_str}\n"
                    f"[🔗 Читать]({news['link']})\n"
                    f"{'─' * 30}"
                )
                await callback.message.answer(
                    msg, parse_mode="Markdown", disable_web_page_preview=True
                )
                await asyncio.sleep(0.3)


# ---------- Статистика ----------
@dp.callback_query(F.data == "stats")
async def stats_cb(callback: CallbackQuery):
    s = await Database.get_stats()
    text = (
        f"📊 *Статистика новостей*\n\n"
        f"✅ Релевантных всего: {s['total']}\n"
        f"• Сегодня: {s['today']}\n"
        f"• Неделя: {s['week']}\n"
        f"• Месяц: {s['month']}\n\n"
        f"🔍 Найдено паттернов:\n"
        f"• Мажорных: {s['major_count']}\n"
        f"• Минорных: {s['minor_count']}"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text, parse_mode="Markdown", reply_markup=main_kb
        )
        await callback.answer()


# ---------- Отмена ----------
@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено", reply_markup=main_kb)


# ===================== ЗАПУСК =====================
async def on_startup():
    await Database.init()
    await Database.load_settings()
    restart_parsing()


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
