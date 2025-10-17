# miniapp.py
# -*- coding: utf-8 -*-

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

# OpenAI можно оставить для других фич; для логина не обязателен
from openai import OpenAI  # noqa: F401

# Playwright
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page, Frame

load_dotenv()
BOT_TOKEN  = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("BOT_TOKEN/WEBAPP_URL не заданы")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("miniapp")
dp = Dispatcher()

# ====== МАПА ЛМС-логинов по вузам (впиши свои реальные URL) ======
LMS_URLS: Dict[str, str] = {
    "ВШЭ — Национальный исследовательский университет": "https://lms.hse.ru/",   # пример
    "МГУ им. М. В. Ломоносова":                        "https://lms.msu.ru/",    # пример
    "СПбГУ":                                           "https://lms.spbu.ru/",
    "МГИМО":                                           "https://lms.mgimo.ru/",
    "Бауманка (МГТУ им. Баумана)":                     "https://lms.bmstu.ru/",
    "ИТМО":                                            "https://lms.itmo.ru/",
    "Физтех (МФТИ)":                                   "https://lms.mipt.ru/",
    "НИТУ МИСИС":                                      "https://lms.misis.ru/",
    "НГУ":                                             "https://lms.nsu.ru/",
    "УРФУ":                                            "https://lms.urfu.ru/",
}
# Если по выбранному вузу нет URL — можно кинуть на какую-то форму/стаб:
DEFAULT_LMS_URL = "https://example.com/login"  # подменишь

# Простое локальное хранилище (демка). В проде — шифровать!
DB_PATH = Path("storage.json")
def load_db() -> Dict[str, Any]:
    if DB_PATH.exists():
        return json.load(open(DB_PATH, "r", encoding="utf-8"))
    return {}
def save_db(data: Dict[str, Any]) -> None:
    json.dump(data, open(DB_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def miniapp_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини-аппу", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
        is_persistent=True,
    )

@dp.message(CommandStart())
async def on_start(m: Message):
    await m.answer(
        "Жми «Открыть мини-аппу» → кнопка «Тест» → выбери вуз → введи логин/пароль. "
        "Я открою браузер и залогинюсь в ЛМС.",
        reply_markup=miniapp_kb()
    )

@dp.message(F.web_app_data)
async def on_web_app_data(m: Message):
    raw = m.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        await m.answer("Не смог распарсить данные из мини-аппы 🤷‍♂️")
        return

    kind = (data.get("kind") or "").strip()
    if kind == "login_lms" or kind == "run_test":  # поддержим старое имя 'run_test'
        await handle_login_lms(m, data)
    else:
        await m.answer("Неизвестная операция. Обнови мини-аппу и попробуй ещё раз.")

# ----------------- Playwright helpers -----------------

def _iter_contexts(page: Page) -> Iterable[Frame]:
    """Возвращает все фреймы: сначала сам page.main_frame, потом вложенные."""
    yield page.main_frame
    for fr in page.frames:
        yield fr

def _first_visible(fr: Frame, selectors: Iterable[str], timeout: int = 4000):
    """Ищем первый видимый элемент по списку селекторов (в заданном фрейме)."""
    for sel in selectors:
        loc = fr.locator(sel)
        try:
            loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception:
            continue
    return None

def playwright_login_flow(url: str, login: str, password: str, keep_open: bool = True) -> str:
    """
    Открывает браузер (НЕ headless), ждёт реальную загрузку, заполняет логин/пароль,
    кликает кнопку Войти. Пытается детектить успех. Возвращает текстовый результат.
    """
    with sync_playwright() as p:
        # видимый браузер, без искусственных задержек
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # Ждём реальную загрузку: domcontentloaded + networkidle
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            # Ок, если сайты постоянно подтягивают данных — идём дальше по селекторам
            pass

        # Ищем поля логина/пароля в основном фрейме и во вложенных
        login_selectors = [
            'input[name="login"]',
            'input[name="username"]',
            'input[id*="user"]',
            'input[type="email"]',
            'input[type="text"]',
        ]
        pass_selectors = [
            'input[name="password"]',
            'input[id*="pass"]',
            'input[type="password"]',
        ]
        submit_selectors = [
            'button:has-text("Войти")',
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
        ]

        login_el = None
        pass_el  = None
        submit_el= None
        used_frame: Optional[Frame] = None

        # Перебираем фреймы
        for fr in _iter_contexts(page):
            if not login_el:
                login_el = _first_visible(fr, login_selectors)
            if not pass_el:
                pass_el  = _first_visible(fr, pass_selectors)
            if not submit_el:
                submit_el = _first_visible(fr, submit_selectors)
            if login_el and pass_el:
                used_frame = fr
                break

        if not (login_el and pass_el):
            return "Не нашёл поля логина/пароля. Проверь URL ЛМС или селекторы."

        # Заполняем
        login_el.click()
        login_el.fill(login)
        pass_el.click()
        pass_el.fill(password)

        # Нажимаем Войти
        if submit_el:
            submit_el.click()
        else:
            # иногда Enter в поле пароля срабатывает
            pass_el.press("Enter")

        # Ждём смену состояния: networkidle / смену URL / пропажу формы
        success = False
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass

        # эвристики успеха: нет полей логина/пароля видимых, есть меню/аватар/выход и т.п.
        try:
            # если логин/пароль снова видимы — вероятно не пустило
            if used_frame:
                still_login = _first_visible(used_frame, login_selectors, timeout=2000)
                still_pass  = _first_visible(used_frame, pass_selectors, timeout=2000)
                success = not (still_login and still_pass)
            else:
                success = True
        except Exception:
            success = True

        # Браузер **НЕ закрываем**, чтобы ты видел, что произошло
        # if not keep_open:
        #     browser.close()

        return "Логин успешен ✅" if success else "Не получилось залогиниться ❌ (проверь логин/пароль/2FA)"

# ----------------- Handler -----------------

async def handle_login_lms(m: Message, data: Dict[str, Any]):
    user_id = str(m.from_user.id)
    uni = (data.get("uni") or "").strip()
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()

    if not (uni and login and password):
        await m.answer("Заполни вуз + логин + пароль в мини-аппе.")
        return

    url = LMS_URLS.get(uni) or DEFAULT_LMS_URL

    # Сохраним (демо; в проде — шифруй!)
    db = load_db()
    db[user_id] = {"uni": uni, "login": login, "password": password, "url": url}
    save_db(db)

    await m.answer(f"Открываю браузер и логинюсь в ЛМС *{uni}*…", parse_mode="Markdown")

    def _run():
        return playwright_login_flow(url, login, password, keep_open=True)

    try:
        result = await asyncio.to_thread(_run)
        await m.answer(result)
    except Exception as e:
        logging.exception("Playwright login error")
        await m.answer(f"Не смог автоматизировать логин: {e}")

async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())