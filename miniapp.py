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
# Утилиты для внешнего Chrome (CDP)
# =========================

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chrome_executable_candidates() -> Tuple[str, ...]:
    # macOS
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    # Linux / WSL / others
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
    Стартует ВНЕШНИЙ Chrome с портом CDP. Возвращает (proc, port, user_data_dir).
    Процесс НЕ трогаем после выполнения — он остаётся жить.
    """
    port = _find_free_port()
    user_data_dir = tempfile.mkdtemp(prefix="chrome-pw-profile-")

    exe = None
    for cand in _chrome_executable_candidates():
        if cand and (os.path.exists(cand) or cand in ("google-chrome", "chrome", "chromium", "chromium-browser")):
            exe = cand
            break
    if not exe:
        raise RuntimeError("Не нашёл исполняемый файл Chrome. Поставь Google Chrome и попробуй снова.")

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-features=Translate,BackForwardCache,AcceptCHFrame",
        "--disable-component-extensions-with-background-pages",
        "--disable-extensions",  # можешь убрать, если надо с расширениями
        "--start-maximized",
        "about:blank",
    ]
    log.info("Запускаю внешний Chrome: %s", " ".join(args))
    # macOS: subprocess без shell — ок
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ждём, пока порт поднимется
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


def open_notepad_and_type_persistent(login_text: str, password_text: str) -> str:
    """
    Подключается к ВНЕШНЕМУ Chrome через CDP, открывает https://notepadonline.ru/app,
    ждёт загрузку, находит поле и печатает:
        <login_text>\n<password_text>
    Внешний Chrome НЕ закрываем (процесс остаётся жить).
    """
    proc, port, _profile = _launch_external_chrome()

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            # откроем новое окно/контекст (страницы доступны через browser.contexts)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            url = "https://notepadonline.ru/app"
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass

            # иногда есть кнопка "Новая/Создать"
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
                editor.type(text_to_type, delay=8)
            else:
                page.click("body", position={"x": 420, "y": 300})
                page.keyboard.type(text_to_type, delay=8)

            try:
                page.screenshot(path="notepad_filled.png", full_page=True)
            except Exception:
                pass

            # ВАЖНО: закрываем ТОЛЬКО CDP-сессию, но НЕ процесс Chrome.
            try:
                browser.close()  # это закрывает только сессию подключения по CDP
            except Exception:
                pass

        # ВНЕШНИЙ Chrome продолжается жить — окно остаётся открытым
        return f"Готово ✅ Данные вставлены. Chrome оставлен открытым (порт {port})."

    except Exception as e:
        log.exception("CDP/Playwright error")
        # даже при ошибке НЕ убиваем proc — чтобы окно можно было увидеть
        return f"Не смог вставить в блокнот: {e}"


# =========================
# Хэндлеры бота
# =========================

@dp.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(
        "Бро, жми «Открыть мини-аппу». Внизу «Тест» → выбери вуз → введи логин/пароль. "
        "Открою видимый Chrome и вставлю их в онлайн-блокнот.",
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


async def handle_paste_to_notepad(m: Message, data: Dict[str, Any]):
    uni = (data.get("uni") or "").strip()
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()

    if not (uni and login and password):
        await m.answer("Нужно выбрать вуз и ввести логин и пароль.")
        return

    # по желанию — сохраним (демо! в проде шифруй)
    db = load_db()
    db[str(m.from_user.id)] = {"uni": uni, "login": login, "password": password}
    save_db(db)

    await m.answer(f"Открываю Chrome и вставляю в блокнот…\nВуз: *{uni}*", parse_mode="Markdown")

    def _run():
        return open_notepad_and_type_persistent(login, password)

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