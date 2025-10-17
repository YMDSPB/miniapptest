# miniapp.py
# -*- coding: utf-8 -*-

import os
import json
import asyncio
import logging
from typing import Any, Dict

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
)

# ==== Загружаем .env ====
# В этой же папке создаёшь .env (см. шаблон ниже)
load_dotenv()

BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL    = os.getenv("WEBAPP_URL", "").strip()   # https://<твое>.pages.dev
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# --- валидация конфигурации ---
missing = []
if not BOT_TOKEN:      missing.append("BOT_TOKEN")
if not WEBAPP_URL:     missing.append("WEBAPP_URL")
if not OPENAI_API_KEY: missing.append("OPENAI_API_KEY")
if missing:
    raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")

# ==== OpenAI (официальный SDK) ====
# pip install openai>=1.44
from openai import OpenAI
oai = OpenAI(api_key=OPENAI_API_KEY)

# ==== Логирование ====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("miniapp")

# ==== Aiogram v3 ====
dp = Dispatcher()


def miniapp_kb() -> ReplyKeyboardMarkup:
    """
    Кнопка, открывающая твою Mini App (Telegram WebApp).
    В URL можно добавлять ?v=номер, чтобы пробивать кэш WebView.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="Открыть мини-аппу",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ]],
        resize_keyboard=True,
        is_persistent=True
    )


@dp.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(
        "Бро, жми «Открыть мини-аппу», вводи текст и я пришлю ответ GPT сюда в чат.",
        reply_markup=miniapp_kb()
    )


@dp.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong ✅")


@dp.message(F.web_app_data)
async def on_web_app_data(m: Message):
    """
    Обработчик данных из WebApp.
    Ожидаем JSON, например:
      { "kind": "ask_gpt", "prompt": "Привет, как дела?" }
    """
    raw = m.web_app_data.data
    log.info("web_app_data raw: %s", raw)

    # Аккуратно парсим
    try:
        data: Dict[str, Any] = json.loads(raw)
    except Exception:
        data = {"prompt": str(raw)}

    kind   = str(data.get("kind") or "").strip()
    prompt = str(data.get("prompt") or "").strip()

    if kind != "ask_gpt" or not prompt:
        await m.answer("Не разобрал запрос 🤔 Введи текст в мини-аппе и нажми отправить.")
        return

    # Сообщим, что работаем
    await m.answer("Думаю над ответом… ⏳")

    # Вызов OpenAI Responses API
    def ask_openai(p: str) -> str:
        resp = oai.responses.create(
            model=OPENAI_MODEL,
            input=p,
            # Можно задать system/instructions, temperature и т.п. при желании:
            # instructions="Отвечай кратко и по делу."
        )
        return resp.output_text.strip()

    try:
        reply_text = await asyncio.to_thread(ask_openai, prompt)
    except Exception as e:
        log.exception("OpenAI error")
        await m.answer(f"Не смог спросить у модели: {e}")
        return

    # Отправляем ответ
    await m.answer(
        f"*Твой запрос:*\n{prompt}\n\n*Ответ GPT:*\n{reply_text}",
        parse_mode="Markdown"
    )


async def main():
    bot = Bot(BOT_TOKEN)
    log.info("Bot started. MiniApp URL: %s", WEBAPP_URL)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())