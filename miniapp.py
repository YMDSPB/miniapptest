# miniapp.py
# -*- coding: utf-8 -*-

import os
import re
import json
import base64
import asyncio
import logging
import socket
import subprocess
import tempfile
import time
import random
from typing import List, Optional, Tuple

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

# ========== CONFIG ==========

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")  # твой постоянный URL на Pages

if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("В .env должны быть BOT_TOKEN и WEBAPP_URL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("miniapp")
dp = Dispatcher()

def kb_open():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини-аппу", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True
    )

# ========== CHROME HELPERS ==========

def _free_port() -> int:
    import socket as _s
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def _chrome_candidates():
    return (
        os.environ.get("GOOGLE_CHROME_BIN") or "",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "google-chrome", "chrome", "chromium", "chromium-browser"
    )

def _launch_chrome(profile_dir: Optional[str] = None) -> Tuple[int, str]:
    port = _free_port()
    profile = profile_dir or tempfile.mkdtemp(prefix="chrome-hse-")

    exe = None
    for c in _chrome_candidates():
        if c and (os.path.exists(c) or c in ("google-chrome","chrome","chromium","chromium-browser")):
            exe = c; break
    if not exe:
        raise RuntimeError("Chrome не найден! Установи Google Chrome.")

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--start-maximized", "about:blank"
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # подождём порт
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    return port, profile

# ========== LOGIN & PARSE ==========

HUMAN_DELAY_MS = 400  # реалистичная печать
COURSES_URL = "https://edu.hse.ru/my/courses.php"

BAN_SUBSTRINGS = [
    "кабинет","инструкции","категория курса","в начало","техническая поддержка",
    "уо юриспруденция","научно","orientation","адаптационный курс","minor","майнор",
    "тестирование","экзамен","exam","внутренний","независимый",
    "учебник","цифровая грамотность","digital literacy",
    "physical training","физическая культура","soft skills",
]

def _human_pause(a=350, b=750):
    time.sleep(random.uniform(a/1000, b/1000))

def _find(fr: Frame, sels: List[str], timeout=6000):
    for sel in sels:
        try:
            loc = fr.locator(sel)
            loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception:
            continue
    return None

def _normalize_title(s: str) -> str:
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[–—-].*$", "", s)
    s = re.sub(r"\s+", " ", s)
    s = s.replace("Название курса", "")
    return s.strip(" ,;:·—–-").strip()

def _prefilter(lines: List[str]) -> List[str]:
    out, seen = [], set()
    for t in lines:
        if len(t) < 6: 
            continue
        low = t.lower()
        if any(b in low for b in BAN_SUBSTRINGS):
            continue
        t2 = _normalize_title(t)
        if len(t2) < 3: 
            continue
        k = t2.lower()
        if k not in seen:
            seen.add(k); out.append(t2)
    return out

def login_and_collect_courses(start_url: str, username: str, password: str) -> List[str]:
    port, _profile = _launch_chrome()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        # 1) логин-страница
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        try: page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout: pass

        # 2) нажать «Войти»
        clicked = False
        for sel in ['text=Войти','button:has-text("Войти")','input[type="submit"]']:
            try: page.locator(sel).first.click(timeout=2000); clicked=True; break
            except: continue
        if not clicked:
            try: page.mouse.click(650, 400)
            except: pass
        _human_pause(900, 1400)

        # 3) SSO вкладка
        try:
            newp = context.wait_for_event("page", timeout=6000)
            page = newp
        except Exception:
            pass

        try: page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except PWTimeout: pass

        # 4) вводим по-человечески
        login_sel = ['#username','input[name="username"]','input[type="email"]']
        pass_sel  = ['#password','input[name="password"]']
        submit_sel= ['#kc-login','button[type="submit"]','input[type="submit"]']

        fr = page.main_frame
        le = _find(fr, login_sel)
        pe = _find(fr, pass_sel)
        se = _find(fr, submit_sel)
        if not (le and pe):
            browser.close()
            return []

        le.click(); le.fill("")
        for ch in username: le.type(ch, delay=HUMAN_DELAY_MS)
        _human_pause(500, 800)
        pe.click()
        for ch in password: pe.type(ch, delay=HUMAN_DELAY_MS)
        _human_pause(500, 900)
        if se: se.click()
        else: pe.press("Enter")

        try: page.wait_for_load_state("networkidle", timeout=25_000)
        except PWTimeout: pass

        # 5) мои курсы
        page.goto(COURSES_URL, wait_until="domcontentloaded", timeout=60_000)
        try: page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout: pass

        # 6) сбор текстов
        raw = []
        for sel in ["a",".course-title",".media-body",".card","h1,h2,h3"]:
            try:
                for el in page.locator(sel).all():
                    try:
                        t = el.inner_text(timeout=500).strip()
                        if t: raw.append(t)
                    except: pass
            except: pass

        browser.close()
    # 7) предфильтр
    prelim = _prefilter(raw)
    return prelim

# ========== BOT (минимум: принять веб-данные, распарсить, перекинуть в мини-аппу через #hash и удалить сообщение) ==========

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Жми «Открыть мини-аппу», введи логин/пароль — дальше всё сделаю тихо.\n"
        "Список курсов попадает в мини-аппу невидимо.",
        reply_markup=kb_open()
    )

@dp.message(F.web_app_data)
async def from_webapp(m: Message):
    # ожидаем, что мини-аппа прислала JSON с полями: kind, login, password, start_url
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        await m.answer("Ошибка данных из мини-аппы.")
        return

    if data.get("kind") != "login_hse_slow":
        await m.answer("Неизвестная операция.")
        return

    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    start_url = (data.get("start_url") or "https://edu.hse.ru/login/hselogin.php").strip()

    if not login or not password:
        await m.answer("Введи логин и пароль.")
        return

    # 1) парсим
    def work():
        return login_and_collect_courses(start_url, login, password)

    await m.answer("Секунду, синхронизирую ЛМС…")  # будет удалено сразу
    courses = await asyncio.to_thread(work)

    # 2) запаковываем в hash (base64url(JSON))
    payload = {
        "uid": m.from_user.id,
        "ts": int(time.time()),
        "courses": courses
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",",":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    url = f"{WEBAPP_URL}#data={b64}"

    # 3) отправляем невидимую кнопку на мини-аппу и удаляем
    msg = await m.answer(
        "Готово ✅ Открываем мини-аппу…",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Открыть мини-аппу", web_app=WebAppInfo(url=url))]],
            resize_keyboard=True
        )
    )
    # микро-пауза и удалить оба сообщения
    await asyncio.sleep(0.5)
    try:
        await m.bot.delete_message(m.chat.id, msg.message_id)
    except Exception:
        pass
    try:
        # удалить предыдущее "Секунду, синхронизирую ЛМС…"
        await m.bot.delete_message(m.chat.id, m.message_id + 1)
    except Exception:
        pass