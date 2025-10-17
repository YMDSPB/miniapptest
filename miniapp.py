import os, json, asyncio, logging
from pathlib import Path
from typing import Dict, Any

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

# OpenAI (если нужно для других функций; не обязателен для Playwright-части)
from openai import OpenAI

# Playwright (sync API удобнее крутить в отдельном потоке)
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not BOT_TOKEN or not WEBAPP_URL:
    raise RuntimeError("BOT_TOKEN/WEBAPP_URL не заданы")

oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("miniapp")
dp = Dispatcher()

# простое хранилище кредов (демо!)
DB_PATH = Path("storage.json")
def load_db() -> Dict[str, Any]:
    if DB_PATH.exists():
        return json.load(open(DB_PATH, "r", encoding="utf-8"))
    return {}
def save_db(data: Dict[str, Any]) -> None:
    json.dump(data, open(DB_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини-аппу", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True, is_persistent=True
    )

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("Бро, жми «Открыть мини-аппу», кнопка «Тест» внизу справа 😉", reply_markup=kb())

@dp.message(F.web_app_data)
async def webapp(m: Message):
    raw = m.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        await m.answer("Не смог распарсить данные от мини-аппы.")
        return

    kind = data.get("kind")
    if kind == "run_test":
        await handle_run_test(m, data)
    else:
        await m.answer("Ок, получил, но не знаю этот 'kind'.")

async def handle_run_test(m: Message, data: Dict[str, Any]):
    user_id = str(m.from_user.id)
    uni = (data.get("uni") or "").strip()
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    text = (data.get("text") or "").strip()

    if not (uni and login and password and text):
        await m.answer("Заполни все поля в мини-аппе — универ, логин/пароль и текст.")
        return

    # Запомним выбор — в демо без шифрования (в проде: шифруй/храни безопасно!)
    db = load_db()
    db[user_id] = {"uni": uni, "login": login, "password": password}
    save_db(db)

    await m.answer(f"Принял. Университет: *{uni}*. Открою браузер и вставлю текст…", parse_mode="Markdown")

    # Запускаем Playwright (видимый браузер)
    def run_playwright(note_text: str):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, slow_mo=50)  # slow_mo для наглядности
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://notepadonline.ru/app", timeout=60000)

            # Если есть кнопка «Создать новую запись» — можно нажать:
            try:
                page.get_by_role("button", name=lambda n: n and "Создать новую" in n).click(timeout=3000)
            except Exception:
                pass  # не критично

            # Некоторые онлайн-блокноты рендерят редактор внутри iframe.
            # Ищем contenteditable в основной странице…
            editor = None
            try:
                editor = page.locator('[contenteditable="true"]').first
                editor.wait_for(state="visible", timeout=8000)
            except PWTimeout:
                editor = None

            # …или в кадрах:
            if editor is None or not editor.count():
                for fr in page.frames:
                    try:
                        ed = fr.locator('[contenteditable="true"]').first
                        ed.wait_for(state="visible", timeout=3000)
                        editor = ed
                        break
                    except Exception:
                        continue

            if editor is None or (hasattr(editor, "count") and not editor.count()):
                # подстраховка: клик в центр и попытка печатать напрямую
                page.click("body", position={"x": 400, "y": 300})
                page.keyboard.type(note_text)
            else:
                editor.click()
                # Вставим текст: можно через type, а можно setInnerText через eval
                editor.type(note_text, delay=10)

            # Снимок на память (в твою папку)
            page.screenshot(path="notepad_filled.png", full_page=True)
            # Не закрываю браузер сразу — пусть пользователь увидит результат.
            # Закроем через пару секунд?
            # page.wait_for_timeout(3000)
            # browser.close()

    try:
        await asyncio.to_thread(run_playwright, text)
        await m.answer("Готово. Текст вставлен в онлайн-блокнот ✅\n(Скрин в файле notepad_filled.png у тебя локально).")
    except Exception as e:
        logging.exception("Playwright error")
        await m.answer(f"Не смог автоматизировать браузер: {e}")

async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())