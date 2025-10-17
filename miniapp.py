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
from typing import Dict, Any, Iterable, Optional, Tuple, List

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

# =========== CONFIG ===========

load_dotenv()
BOT_TOKEN  = (os.getenv("BOT_TOKEN") or "").strip()
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("В .env должны быть BOT_TOKEN и WEBAPP_URL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("miniapp")
dp  = Dispatcher()

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
        resize_keyboard=True, is_persistent=True
    )

# =========== Chrome via CDP helpers ===========

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def _chrome_candidates() -> Tuple[str, ...]:
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return (
        os.environ.get("GOOGLE_CHROME_BIN") or "",
        mac, "google-chrome", "chrome", "chromium", "chromium-browser"
    )

def _launch_external_chrome(user_profile_dir: Optional[str] = None) -> Tuple[subprocess.Popen, int, str]:
    """
    Стартуем внешний Chrome. Если user_profile_dir задан — используем его,
    чтобы подхватить существующую сессию. Возвращает (proc, port, profile_dir).
    """
    port = _free_port()
    profile = user_profile_dir or tempfile.mkdtemp(prefix="chrome-studentplus-")

    exe = None
    for c in _chrome_candidates():
        if c and (os.path.exists(c) or c in ("google-chrome","chrome","chromium","chromium-browser")):
            exe = c; break
    if not exe:
        raise RuntimeError("Chrome не найден. Поставь Google Chrome и повтори.")

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--start-maximized",
        "about:blank",
    ]
    log.info("Запуск внешнего Chrome: %s", " ".join(args))
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ждём порт
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    else:
        raise RuntimeError("CDP-порт Chrome не поднялся")
    return proc, port, profile

# =========== Playwright helpers ===========

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

# ускорили печать вдвое: ~160 симв/мин
HUMAN_DELAY_MS = 375

def _human_pause(min_ms=350, max_ms=800):
    time.sleep(random.uniform(min_ms/1000, max_ms/1000))

# =========== LOGIN FLOW ===========

def login_via_hse_portal(user_id: int, start_url: str, username: str, password: str) -> str:
    """
    1) Открыть https://edu.hse.ru/login/hselogin.php
    2) Нажать «Войти»
    3) Заполнить форму SSO медленно
    4) Сохранить порт и профиль для юзера
    5) Окно НЕ закрывать
    """
    # если у юзера уже есть живой порт — не стартуем новый
    db = load_db()
    rec = db.get(str(user_id)) or {}
    port = rec.get("cdp_port")
    profile = rec.get("chrome_profile")

    # всегда стартуем новый Chrome (надежнее), но с тем же profile если есть
    proc, port, profile = _launch_external_chrome(profile)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        try: page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout: pass

        # «Войти»
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
            page.mouse.click(640, 380)
        _human_pause(600, 1000)

        try:
            newp = context.wait_for_event("page", timeout=4000)
            page = newp
        except Exception:
            pass

        try: page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except PWTimeout: pass
        try: page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout: pass

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
            return "Не нашёл форму логина HSE SSO. Проверь поток входа."

        # печатаем медленно (x2 быстрее, чем раньше)
        login_el.click(); login_el.fill("")
        for ch in username: login_el.type(ch, delay=HUMAN_DELAY_MS)
        _human_pause(500, 900)
        pass_el.click()
        for ch in password: pass_el.type(ch, delay=HUMAN_DELAY_MS)
        _human_pause(400, 800)

        if submit_el: submit_el.click()
        else: pass_el.press("Enter")

        try: page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout: pass

        # сохраняем порт и профиль для следующей операции
        rec.update({"cdp_port": port, "chrome_profile": profile})
        db[str(user_id)] = rec
        save_db(db)

        try: browser.close()  # только CDP-сессию
        except: pass

        return "Вход выполнен ✅, окно оставил открытым."

# =========== COURSES PARSE ===========

COURSES_URL = "https://edu.hse.ru/my/courses.php"

def _connect_or_launch_from_profile(profile: str, port_hint: Optional[int]) -> Tuple[int, str]:
    """
    Пытаемся подключиться к уже живому Chrome по port_hint.
    Если не удалось — поднимаем новый Chrome с тем же профилем.
    Возвращает (port, profile)
    """
    if port_hint:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port_hint)) == 0:
                    return port_hint, profile
        except Exception:
            pass
    # стартуем новый
    _proc, port, profile = _launch_external_chrome(profile)
    return port, profile

def extract_course_titles(page: Page) -> List[str]:
    """
    Тянем максимум текста и фильтруем до вероятных названий курсов.
    """
    # попробуем разумные селекторы
    candidates = []
    sel_list = [
        'a', 'h1,h2,h3,h4,h5',
        '.course-title', '.media-body', '.list-group-item',
        '.card', '.card-body'
    ]
    for sel in sel_list:
        try:
            for el in page.locator(sel).all():
                try:
                    t = el.inner_text(timeout=1000).strip()
                    if t: candidates.append(t)
                except Exception:
                    continue
        except Exception:
            continue

    # нарезаем на строки
    lines: List[str] = []
    for block in candidates:
        for line in re.split(r'[\n\r]+', block):
            l = line.strip()
            if l: lines.append(l)

    # фильтрация мусора
    ban_phrases = [
        "Личный кабинет", "Мои курсы", "Инструкции", "Техническая поддержка",
        "Категория курса", "Название курса", "выполнено", "В начало"
    ]
    def looks_like_course(s: str) -> bool:
        if any(bp.lower() in s.lower() for bp in ban_phrases): return False
        if len(s) < 8: return False
        if not re.search(r'[А-Яа-яA-Za-z]', s): return False
        return True

    lines = [s for s in lines if looks_like_course(s)]

    # хард-нормализация: убираем всё в скобках/после «—»/«-» и лишние пробелы
    def normalize(s: str) -> str:
        s1 = re.sub(r'\([^()]*\)', '', s)  # вырезаем (....)
        # несколько раз — вдруг вложенные скобки
        s1 = re.sub(r'\([^()]*\)', '', s1)
        s1 = re.sub(r'—.*$', '', s1)  # после длинного тире
        s1 = re.sub(r'-.*$', '', s1)  # после дефиса
        s1 = re.sub(r'\s+', ' ', s1).strip(' ,;:–—')
        # иногда дублируется «Название курса...»
        s1 = s1.replace("Название курса", "").strip()
        return s1

    prelim = [normalize(s) for s in lines]
    prelim = [s for s in prelim if len(s) >= 3]

    # грубая дедупликация до GPT
    uniq = []
    seen = set()
    for s in prelim:
        key = s.lower()
        if key not in seen:
            uniq.append(s)
            seen.add(key)
    return uniq

# OpenAI (Responses API)
from openai import OpenAI
oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def canonicalize_with_gpt(titles: List[str]) -> List[str]:
    """
    Отдаём GPT список, просим вернуть короткие канонические названия без дубликатов.
    """
    if not titles:
        return []
    if not oai:
        return titles  # если ключа нет — возвращаем как есть

    prompt = (
        "Ты помощник, который приводит названия университетских курсов к краткой канонической форме.\n"
        "Правила:\n"
        "— Удали упоминания преподавателей, годов, модулей, групп, факультетов и прочих пояснений.\n"
        "— Сохрани только краткое название предмета (например, «Гражданское право»).\n"
        "— Объедини повторы/варианты в один пункт.\n"
        "— Верни только итоговый список, по одному названию на строку, без нумерации и комментариев.\n\n"
        "Список исходных строк:\n" + "\n".join(f"- {t}" for t in titles)
    )

    resp = oai.responses.create(model=OPENAI_MODEL, input=prompt)
    text = resp.output_text.strip()
    # разбираем по строкам
    out = [re.sub(r'^\s*[-•\d.)]+\s*', '', ln).strip() for ln in text.splitlines()]
    out = [ln for ln in out if ln]
    # финальная дедупликация
    seen = set(); res = []
    for s in out:
        k = s.lower()
        if k not in seen:
            seen.add(k); res.append(s)
    return res

def parse_courses_for_user(user_id: int) -> Tuple[List[str], str]:
    """
    Коннект к текущему Chrome (или запуск с тем же профилем), переход на courses.php,
    парсинг, прогон через GPT. Возвращает (финальный_список, источник_сообщение).
    """
    db = load_db()
    rec = db.get(str(user_id))
    if not rec or not rec.get("chrome_profile"):
        return [], "Сначала нужно войти в ЛМС через кнопку «Подключить» в мини-аппе."

    port_hint = rec.get("cdp_port")
    profile   = rec.get("chrome_profile")
    port, profile = _connect_or_launch_from_profile(profile, port_hint)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        page.goto(COURSES_URL, wait_until="domcontentloaded", timeout=60_000)
        try: page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout: pass

        titles_raw = extract_course_titles(page)
        try: browser.close()
        except: pass

    cleaned = canonicalize_with_gpt(titles_raw)
    return cleaned, f"Нашёл {len(titles_raw)} строк(и), после чистки — {len(cleaned)} предмет(а)."

# =========== Bot Handlers ===========

@dp.message(CommandStart())
async def on_start(m: Message):
    await m.answer(
        "Бро, жми «Открыть мини-аппу» → «Подключить» — залогиню тебя в ЛМС.\n"
        "Потом кнопка «Собрать предметы» — спарсю курсы и пришлю список сюда.",
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

    if kind == "login_hse_slow":
        username = (data.get("login") or "").strip()
        password = (data.get("password") or "").strip()
        start_url = (data.get("start_url") or "https://edu.hse.ru/login/hselogin.php").strip()

        if not (username and password):
            await m.answer("Введи логин и пароль.")
            return

        await m.answer("Открываю Chrome и выполняю вход… Печатаю побыстрее, но по-человечески 🙂")

        def run_login():
            return login_via_hse_portal(m.from_user.id, start_url, username, password)

        try:
            result = await asyncio.to_thread(run_login)
            await m.answer(result)
        except Exception as e:
            log.exception("Playwright error")
            await m.answer(f"Playwright упал: {e}")

    elif kind == "parse_courses":
        await m.answer("Иду на страницу «Мои курсы», собираю названия…")

        def run_parse():
            return parse_courses_for_user(m.from_user.id)

        try:
            final_list, info = await asyncio.to_thread(run_parse)
            if not final_list:
                await m.answer(info)
                return
            pretty = "\n".join(f"• {x}" for x in final_list)
            await m.answer(f"{info}\n\n*Твои предметы:*\n{pretty}", parse_mode="Markdown")
        except Exception as e:
            log.exception("Parse error")
            await m.answer(f"Не смог собрать курсы: {e}")

    else:
        await m.answer(f"Неизвестная операция: {kind}")

# =========== main ===========

async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())