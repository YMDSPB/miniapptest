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
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()  # https://<ты>.pages.dev?v=...

if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("Задай BOT_TOKEN и WEBAPP_URL в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("miniapp")

dp = Dispatcher()
DB_PATH = Path("storage.json")


def kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини-аппу", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
        is_persistent=True,
    )

# =========================
# Вспомогалки: внешний Chrome (CDP)
# =========================

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chrome_executable_candidates() -> Tuple[str, ...]:
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return (
        os.environ.get("GOOGLE_CHROME_BIN") or "",
        mac,
        "google-chrome",
        "chrome",
        "chromium",
        "chromium-browser",
    )


def _launch_external_chrome() -> Tuple[subprocess.Popen, int, str]:
    """
    Стартуем внешний Chrome с CDP-портом. Возвращаем (proc, port, user_data_dir).
    НЕ закрываем proc — окно останется жить после выполнения.
    """
    port = _find_free_port()
    user_data_dir = tempfile.mkdtemp(prefix="chrome-hse-profile-")

    exe = None
    for cand in _chrome_executable_candidates():
        if cand and (os.path.exists(cand) or cand in ("google-chrome", "chrome", "chromium", "chromium-browser")):
            exe = cand
            break
    if not exe:
        raise RuntimeError("Не найден Chrome. Поставь Google Chrome и попробуй снова.")

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-features=Translate,BackForwardCache,AcceptCHFrame",
        "--disable-component-extensions-with-background-pages",
        "--disable-extensions",
        "--start-maximized",
        "about:blank",
    ]
    log.info("Запускаю внешний Chrome: %s", " ".join(args))
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ждём, пока поднимется порт
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                log.info("Chrome CDP порт %s доступен", port)
                break
        time.sleep(0.2)
    else:
        raise RuntimeError("Chrome не поднял CDP порт вовремя")

    return proc, port, user_data_dir

# =========================
# Playwright helpers
# =========================

def _iter_frames(page: Page) -> Iterable[Frame]:
    yield page.main_frame
    for fr in page.frames:
        yield fr


def _first_visible(fr: Frame, selectors, timeout=4000):
    for sel in selectors:
        loc = fr.locator(sel)
        try:
            loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception:
            continue
    return None


def login_hse_openid(auth_url: str, login_text: str, password_text: str) -> str:
    """
    Подключается к внешнему Chrome (CDP), открывает HSE Keycloak auth_url,
    ждёт форму логина, вводит логин/пароль и сабмитит. Окно Chrome не закрываем.
    """
    proc, port, _prof = _launch_external_chrome()

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            # Переход + реальная загрузка
            page.goto(auth_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass

            # Типичные селекторы Keycloak
            login_selectors = ['#username', 'input[name="username"]', 'input[type="email"]', 'input[type="text"]']
            pass_selectors  = ['#password', 'input[name="password"]', 'input[type="password"]']
            submit_selectors= ['#kc-login', 'button[type="submit"]', 'input[type="submit"]',
                               'button:has-text("Войти")', 'button:has-text("Log in")', 'button:has-text("Sign in")']

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
                return "Не нашёл поля логина/пароля на странице SSO. Проверь ссылку/страницу."

            login_el.click()
            login_el.fill(login_text)
            pass_el.click()
            pass_el.fill(password_text)

            if submit_el:
                submit_el.click()
            else:
                pass_el.press("Enter")

            # Ждём редирект или исчезновение формы
            success = False
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PWTimeout:
                pass

            try:
                # если поля всё ещё видимы — вероятно, остались на форме (ошибка пароля/2FA)
                still_login = used_frame and _first_visible(used_frame, login_selectors, timeout=2000)
                still_pass  = used_frame and _first_visible(used_frame, pass_selectors,  timeout=2000)
                success = not (still_login and still_pass)
            except Exception:
                success = True

            # Закрываем ТОЛЬКО CDP-сессию, внешнее окно Chrome остаётся
            try:
                browser.close()
            except Exception:
                pass

            return "Вход выполнен ✅ Окно Chrome оставлено открытым." if success else \
                   "Не удалось войти ❌ Проверь логин/пароль/2FA. Окно Chrome оставлено открытым."

    except Exception as e:
        log.exception("HSE login error")
        return f"Не смог выполнить вход: {e}"

# =========================
# Бот-хэндлеры
# =========================

@dp.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(
        "Жми «Открыть мини-аппу» → «Подключить» → введи логин/пароль. "
        "Я открою HSE SSO в видимом Chrome, залогинюсь и оставлю окно открытым.",
        reply_markup=kb(),
    )


@dp.message(F.web_app_data)
async def on_web_app_data(m: Message):
    raw = m.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        await m.answer("Не смог распарсить JSON из мини-аппы 😐")
        return

    kind = (data.get("kind") or "").strip()
    if kind == "login_hse":
        await handle_login_hse(m, data)
    else:
        await m.answer(f"Неизвестная операция: {kind}. Обнови мини-аппу.")


async def handle_login_hse(m: Message, data: Dict[str, Any]):
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    auth_url = (data.get("auth_url") or "").strip()

    if not (login and password and auth_url):
        await m.answer("Нужны логин, пароль и ссылка auth_url.")
        return

    await m.answer("Открываю Chrome и выполняю вход в ЛМС (HSE SSO)…")

    def _run():
        return login_hse_openid(auth_url, login, password)

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