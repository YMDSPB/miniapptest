# miniapp.py
# -*- coding: utf-8 -*-

import os
import json
import asyncio
import logging
import socket
import subprocess
import tempfile
import time
import random
from pathlib import Path
from typing import Dict, Any, Iterable, Optional, Tuple

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
# Конфиг
# =========================

load_dotenv()
BOT_TOKEN  = (os.getenv("BOT_TOKEN") or "").strip()
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()

if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("В .env должны быть BOT_TOKEN и WEBAPP_URL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("miniapp")
dp  = Dispatcher()

def kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини-аппу", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True, is_persistent=True
    )

# =========================
# Утилиты: внешний Chrome (CDP)
# =========================

def _free_port() -> int:
    import socket as _s
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def _chrome_candidates() -> Tuple[str, ...]:
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return (
        os.environ.get("GOOGLE_CHROME_BIN") or "",
        mac, "google-chrome", "chrome", "chromium", "chromium-browser"
    )

def _launch_external_chrome() -> Tuple[subprocess.Popen, int, str]:
    port = _free_port()
    user_data_dir = tempfile.mkdtemp(prefix="chrome-studentplus-")

    exe = None
    for c in _chrome_candidates():
        if c and (os.path.exists(c) or c in ("google-chrome","chrome","chromium","chromium-browser")):
            exe = c; break
    if not exe:
        raise RuntimeError("Chrome не найден. Поставь Google Chrome и повтори.")

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run", "--no-default-browser-check",
        "--start-maximized",
        "about:blank",
    ]
    log.info("Запуск внешнего Chrome: %s", " ".join(args))
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ждём порт
    deadline = time.time() + 10
    import socket as _s
    while time.time() < deadline:
        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    else:
        raise RuntimeError("CDP-порт Chrome не поднялся")
    return proc, port, user_data_dir

# =========================
# Playwright helpers
# =========================

def _iter_frames(page: Page) -> Iterable[Frame]:
    yield page.main_frame
    for fr in page.frames:
        yield fr

def _first_visible(fr: Frame, sels, timeout=4000):
    for sel in sels:
        loc = fr.locator(sel)
        try:
            loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception:
            continue
    return None

HUMAN_DELAY_MS = 750  # ~80 симв/мин

def _human_pause(min_ms=600, max_ms=1200):
    time.sleep(random.uniform(min_ms/1000, max_ms/1000))

def login_via_hse_portal(start_url: str, username: str, password: str) -> str:
    """
    1) Открыть https://edu.hse.ru/login/hselogin.php
    2) Нажать кнопку «Войти»
    3) Дождаться формы SSO (Keycloak)
    4) Набрать username и password "по-человечески" (delay ~ 750ms/символ)
    5) Залогиниться, окно оставить открытым
    """
    proc, port, _prof = _launch_external_chrome()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        # 1) заходим на стартовую
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        try: page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout: pass

        # 2) жмём «Войти»
        # пробуем разные варианты кнопки
        clicked = False
        for sel in [
            'button:has-text("Войти")',
            'text=Войти',
            'button >> text=/Войти/i',
            'input[type="submit"]',
            'a:has-text("Войти")'
        ]:
            try:
                page.locator(sel).first.click(timeout=2000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            # fallback: клик по центру окна модалки (как на скрине)
            page.mouse.click(640, 380)
        _human_pause(700, 1200)

        # после клика либо редирект, либо новая вкладка
        try:
            # если вдруг открылась новая страница
            newp = context.wait_for_event("page", timeout=4000)
            page = newp
        except Exception:
            pass

        # ждём загрузку формы
        try: page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except PWTimeout: pass
        try: page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout: pass

        # 3) ищем поля
        login_selectors = ['#username','input[name="username"]','input[type="email"]','input[type="text"]']
        pass_selectors  = ['#password','input[name="password"]','input[type="password"]']
        submit_selectors= ['#kc-login','button[type="submit"]','input[type="submit"]',
                           'button:has-text("Войти")','button:has-text("Log in")','button:has-text("Sign in")']

        login_el = pass_el = submit_el = None
        used_frame: Optional[Frame] = None
        for fr in _iter_frames(page):
            if not login_el: login_el = _first_visible(fr, login_selectors, timeout=8000)
            if not pass_el:  pass_el  = _first_visible(fr, pass_selectors,  timeout=8000)
            if not submit_el:submit_el= _first_visible(fr, submit_selectors, timeout=4000)
            if login_el and pass_el:
                used_frame = fr
                break

        if not (login_el and pass_el):
            try: browser.close()
            except: pass
            return "Не нашёл форму логина HSE SSO. Возможно, другой поток входа."

        # 4) набираем медленно
        login_el.click()
        login_el.fill("")  # на случай автозаполнений
        for ch in username:
            login_el.type(ch, delay=HUMAN_DELAY_MS)
        _human_pause(900, 1500)

        pass_el.click()
        for ch in password:
            pass_el.type(ch, delay=HUMAN_DELAY_MS)

        _human_pause(600, 1100)

        # 5) сабмит
        if submit_el:
            submit_el.click()
        else:
            pass_el.press("Enter")

        # ждём редирект/успешную загрузку
        try: page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout: pass

        # если поля исчезли — считаем успехом
        success = True
        try:
            still_login = used_frame and _first_visible(used_frame, login_selectors, timeout=2000)
            still_pass  = used_frame and _first_visible(used_frame, pass_selectors,  timeout=2000)
            success = not (still_login and still_pass)
        except Exception:
            success = True

        try: browser.close()  # закрываем ТОЛЬКО CDP-сессию, окно Chrome остаётся
        except: pass

        return "Вход выполнен ✅, окно оставил открытым." if success else \
               "Не удалось войти ❌ (проверь логин/пароль/2FA). Окно оставил открытым."

# =========================
# Бот
# =========================

@dp.message(CommandStart())
async def on_start(m: Message):
    await m.answer(
        "Жми «Открыть мини-аппу» → «Подключить» → введи логин/пароль.\n"
        "Открою Chrome, нажму «Войти» на hselogin.php, заполню форму SSO медленно и оставлю окно открытым.",
        reply_markup=kb()
    )

@dp.message(F.web_app_data)
async def on_webapp(m: Message):
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        await m.answer("Не смог распарсить данные из мини-аппы.")
        return

    kind = (data.get("kind") or "").strip()
    if kind != "login_hse_slow":
        await m.answer(f"Неизвестная операция: {kind}")
        return

    username = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    start_url = (data.get("start_url") or "https://edu.hse.ru/login/hselogin.php").strip()

    if not (username and password):
        await m.answer("Введи логин и пароль.")
        return

    await m.answer("Открываю Chrome и выполняю вход… Печатаю медленно, не пугайся 🙂")

    def run():
        return login_via_hse_portal(start_url, username, password)

    try:
        result = await asyncio.to_thread(run)
        await m.answer(result)
    except Exception as e:
        log.exception("Playwright error")
        await m.answer(f"Playwright упал: {e}")

# =========================
# main
# =========================

async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())