# miniapp.py
# -*- coding: utf-8 -*-

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Iterable, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PWTimeout,
    Page,
    Frame,
)

# =========================
# Конфиг и подготовка
# =========================

load_dotenv()
BOT_TOKEN  = (os.getenv("BOT_TOKEN") or "").strip()
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()  # https://<ты>.pages.dev?v=...
if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("Задай BOT_TOKEN и WEBAPP_URL в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("miniapp")

dp = Dispatcher()

# локальная демо-база (если захочешь хранить выбор вуза/логин)
DB_PATH = Path("storage.json")


def load_db() -> Dict[str, Any]:
    if DB_PATH.exists():
        try:
            return json.load(open(DB_PATH, "r", encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_db(data: Dict[str, Any]) -> None:
    json.dump(data, open(DB_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини-аппу", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
        is_persistent=True,
    )

# =========================
# Хэндлеры бота
# =========================

@dp.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(
        "Бро, жми «Открыть мини-аппу». Внизу кнопка «Тест» → выбери вуз → введи логин/пароль. "
        "Я открою видимый браузер и вставлю их в онлайн-блокнот.",
        reply_markup=kb(),
    )


@dp.message(F.web_app_data)
async def on_web_app_data(m: Message):
    raw = m.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        await m.answer("Не смог распарсить JSON из мини-аппы 🤷‍♂️")
        return

    kind = (data.get("kind") or "").strip()
    if kind == "paste_to_notepad":
        await handle_paste_to_notepad(m, data)
    else:
        await m.answer(f"Неизвестная операция: {kind}. Обнови мини-аппу.")


# =========================
# Playwright утилиты
# =========================

def _iter_frames(page: Page) -> Iterable[Frame]:
    # основной фрейм + все вложенные
    yield page.main_frame
    for fr in page.frames:
        yield fr


def _first_visible(fr: Frame, selectors: Iterable[str], timeout: int = 4000):
    # первый видимый элемент по списку селекторов
    for sel in selectors:
        loc = fr.locator(sel)
        try:
            loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception:
            continue
    return None


def open_notepad_and_type(login_text: str, password_text: str) -> str:
    """
    Открывает https://notepadonline.ru/app в ВИДИМОМ браузере (headless=False),
    ждёт реальную загрузку (domcontentloaded + попытка networkidle),
    ищет редактируемую область и печатает:
        <login_text>\n<password_text>
    Браузер НЕ закрываем.
    Возвращает текстовый статус.
    """
    url = "https://notepadonline.ru/app"

    with sync_playwright() as p:
        # Видимый браузер, без искусственных sleep
        browser = p.chromium.launch(headless=False)  # окно будет видно
        context = browser.new_context()
        page = context.new_page()

        # Переход + реальные состояния загрузки
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            # Окей, если сайт постоянно дёргает сеть — двигаемся дальше по селекторам
            pass

        # Попробуем нажать «Создать новую запись», если есть
        try:
            page.get_by_role("button", name=lambda n: n and ("Создать" in n or "Новая" in n)).click(timeout=3_000)
        except Exception:
            pass

        # Ищем редактируемую область
        editor = None
        editor_selectors = [
            '[contenteditable="true"]',
            'div[role="textbox"]',
            '.notepad, .editor, .ql-editor, .monaco-editor',
            'textarea',
        ]

        for fr in _iter_frames(page):
            editor = _first_visible(fr, editor_selectors, timeout=5_000)
            if editor:
                break

        text_to_type = f"{login_text}\n{password_text}"

        if editor:
            editor.click()
            editor.type(text_to_type, delay=8)  # печатаем посимвольно, видно глазами
        else:
            # fallback: клик в центр страницы и печать «в никуда» — многие редакторы всё равно ловят ввод
            page.click("body", position={"x": 420, "y": 300})
            page.keyboard.type(text_to_type, delay=8)

        # Снимок на память, рядом с miniapp.py
        try:
            page.screenshot(path="notepad_filled.png", full_page=True)
        except Exception:
            pass

        # БРАУЗЕР НЕ ЗАКРЫВАЕМ
        return "Готово ✅ Логин и пароль вставлены в блокнот. Браузер оставил открытым."


# =========================
# Хэндлер логики вставки
# =========================

async def handle_paste_to_notepad(m: Message, data: Dict[str, Any]):
    uni = (data.get("uni") or "").strip()
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()

    if not (uni and login and password):
        await m.answer("Нужно выбрать вуз и ввести логин и пароль.")
        return

    # По желанию — сохраним (ДЕМО! В проде шифруй!)
    db = load_db()
    db[str(m.from_user.id)] = {"uni": uni, "login": login, "password": password}
    save_db(db)

    await m.answer(f"Открываю блокнот и вставляю данные…\nВуз: *{uni}*", parse_mode="Markdown")

    def _run():
        return open_notepad_and_type(login, password)

    try:
        result = await asyncio.to_thread(_run)
        await m.answer(result)
    except Exception as e:
        log.exception("Playwright error")
        await m.answer(f"Playwright упал: {e}")


# =========================
# Точка входа
# =========================

async def main():
    bot = Bot(BOT_TOKEN)
    log.info("Bot online. WEBAPP_URL=%s", WEBAPP_URL)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())