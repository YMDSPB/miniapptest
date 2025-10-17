# miniapp.py
# -*- coding: utf-8 -*-

import os
import re
import json
import asyncio
import logging
import socket
import subprocess
import tempfile
import time
import random
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

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

from openai import OpenAI

# ========= CONFIG =========

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("В .env должны быть BOT_TOKEN и WEBAPP_URL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
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

def save_db(data: Dict[str, Any]):
    json.dump(data, open(DB_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини-аппу", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True
    )

# ========= CHROME HELPERS =========

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def _chrome_candidates() -> Tuple[str, ...]:
    return (
        os.environ.get("GOOGLE_CHROME_BIN") or "",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "google-chrome", "chrome", "chromium", "chromium-browser"
    )

def _launch_chrome(profile_dir: Optional[str] = None) -> Tuple[subprocess.Popen, int, str]:
    port = _free_port()
    profile = profile_dir or tempfile.mkdtemp(prefix="chrome-hse-")

    exe = None
    for c in _chrome_candidates():
        if c and (os.path.exists(c) or c in ("google-chrome", "chrome")):
            exe = c; break
    if not exe:
        raise RuntimeError("Chrome не найден!")

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--start-maximized", "about:blank"
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    return None, port, profile


# ========= PLAYWRIGHT LOGIN & PARSE =========

HUMAN_DELAY_MS = 400  # 160 симв/мин
COURSES_URL = "https://edu.hse.ru/my/courses.php"

def human_pause(a=350, b=750):
    time.sleep(random.uniform(a/1000, b/1000))

def _iter_frames(page: Page):
    yield page.main_frame
    for f in page.frames:
        yield f

def _find(fr: Frame, sels: List[str], timeout=6000):
    for sel in sels:
        try:
            loc = fr.locator(sel)
            loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception:
            continue
    return None


def login_and_parse_courses(user_id: int, start_url: str, login: str, password: str) -> str:
    """Полный сценарий: логин → переход на курсы → парсинг → GPT"""
    proc, port, profile = _launch_chrome()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        # Шаг 1: открываем логин
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass

        # Кликаем «Войти»
        try:
            page.locator("text=Войти").first.click(timeout=4000)
        except Exception:
            try:
                page.mouse.click(650, 400)
            except:
                pass
        human_pause(1000, 1500)

        # Переход в новое окно (SSO)
        try:
            newp = context.wait_for_event("page", timeout=6000)
            page = newp
        except Exception:
            pass

        page.wait_for_load_state("domcontentloaded", timeout=30_000)

        # Находим поля логина/пароля
        login_sel = ['#username', 'input[name="username"]', 'input[type="email"]']
        pass_sel = ['#password', 'input[name="password"]']
        submit_sel = ['#kc-login', 'button[type="submit"]', 'input[type="submit"]']

        fr = page.main_frame
        login_el = _find(fr, login_sel)
        pass_el = _find(fr, pass_sel)
        submit_el = _find(fr, submit_sel)

        if not (login_el and pass_el):
            return "Не нашёл форму логина — проверь SSO страницу."

        # Медленно вводим данные
        login_el.click()
        for ch in login: login_el.type(ch, delay=HUMAN_DELAY_MS)
        human_pause(500, 800)
        pass_el.click()
        for ch in password: pass_el.type(ch, delay=HUMAN_DELAY_MS)
        human_pause(500, 900)
        submit_el.click()
        page.wait_for_load_state("networkidle", timeout=25_000)

        # После входа → переходим на страницу курсов
        page.goto(COURSES_URL, wait_until="domcontentloaded", timeout=60_000)
        try: page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout: pass

        # Парсим текст
        titles = []
        for el in page.locator("a, .card, h3").all():
            try:
                t = el.inner_text(timeout=500).strip()
                if len(t) > 5: titles.append(t)
            except:
                continue

        # Фильтруем мусор
        clean = []
        for t in titles:
            if any(x in t.lower() for x in ["в начало", "инструкции", "кабинет", "категория курса"]):
                continue
            clean.append(t)

        # Убираем мусор в скобках и дубликаты
        norm = []
        for t in clean:
            t = re.sub(r"\([^)]*\)", "", t)
            t = re.sub(r"[-–—].*", "", t)
            t = re.sub(r"\s+", " ", t).strip()
            if len(t) >= 5 and t not in norm:
                norm.append(t)

        # Прогоняем через GPT
        oai = OpenAI(api_key=OPENAI_API_KEY)
        prompt = (
            "Ты должен из списка удалить дубликаты и лишнюю информацию "
            "(годы, модули, преподавателей и факультеты), оставив только короткие "
            "названия предметов. Верни чистый список по одному на строку:\n\n"
            + "\n".join(norm)
        )

        resp = oai.responses.create(model=OPENAI_MODEL, input=prompt)
        text = resp.output_text.strip()

        browser.close()

    return f"Нашёл {len(norm)} курсов, вот итоговый список:\n\n{text}"


# ========= BOT =========

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Жми «Открыть мини-аппу» → введи логин и пароль от ЛМС, "
        "я сам залогинюсь, соберу твои предметы и пришлю список.",
        reply_markup=kb()
    )

@dp.message(F.web_app_data)
async def from_webapp(m: Message):
    try:
        data = json.loads(m.web_app_data.data)
    except:
        await m.answer("Ошибка данных из мини-аппы.")
        return

    if data.get("kind") == "login_hse_slow":
        login = data.get("login", "").strip()
        password = data.get("password", "").strip()
        start_url = data.get("start_url", "https://edu.hse.ru/login/hselogin.php")

        if not login or not password:
            await m.answer("Введи логин и пароль.")
            return

        await m.answer("Открываю Chrome и выполняю вход... Это займёт около минуты, не пугайся ⚙️")

        def work():
            return login_and_parse_courses(m.from_user.id, start_url, login, password)

        try:
            result = await asyncio.to_thread(work)
            await m.answer(result)
        except Exception as e:
            log.exception("Ошибка Playwright")
            await m.answer(f"Не удалось завершить вход: {e}")
    else:
        await m.answer("Неизвестная операция.")

# ========= MAIN =========

async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())