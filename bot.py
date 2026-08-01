import os
import re
import time
import base64
import asyncio
import requests
from collections import defaultdict
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    BusinessConnectionHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

chat_histories = defaultdict(list)
MAX_HISTORY = 16

request = HTTPXRequest(
    connect_timeout=30.0,
    read_timeout=30.0,
    write_timeout=30.0,
    pool_timeout=30.0
)

SYSTEM_PROMPT = """Ты обычный живой человек, который переписывается в Telegram.
Говори естественно, как в обычном чате с другом или знакомым.

Правила:
- Не пиши длинными простынями. Лучше 1–3 коротких сообщения.
- Можно использовать сленг, сокращения, эмодзи (но не перебарщивай).
- Иногда можно ответить очень коротко: «ага», «понял», «хм», «ладно», «ок» и т.д.
- Не говори, что ты бот, ИИ или ассистент.
- Не используй слишком правильный и официальный язык.
- Если вопрос простой — отвечай коротко.
- Если нужно объяснить что-то — разбивай мысль на несколько сообщений.
- Можешь проявлять эмоции, мнение, лёгкий юмор.
"""

async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    if bc.is_enabled:
        print(f"✅ Подключено: {bc.user.first_name} | connection_id = {bc.id}")
    else:
        print(f"❌ Отключено: {bc.id}")

async def transcribe_voice(file_path: str) -> str:
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (file_path, f, "audio/ogg")},
                data={"model": "whisper-large-v3", "language": "ru"},
                timeout=60,
            )
        data = resp.json()
        return data.get("text", "")
    except Exception as e:
        print(f"Ошибка расшифровки: {e}")
        return ""

async def analyze_image(file_path: str, caption: str = "") -> str:
    try:
        with open(file_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("utf-8")

        mime = "image/png" if file_path.endswith(".png") else "image/jpeg"
        prompt = caption if caption else "Что на этом фото? Опиши коротко и по-человечески."

        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "qwen/qwen3.6-27b",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{image_base64}"},
                            },
                        ],
                    }
                ],
                "temperature": 0.6,
                "max_tokens": 500,
            },
            timeout=60,
        )
        data = resp.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"]
        return "Не понял, что на фото."
    except Exception as e:
        print(f"Ошибка фото: {e}")
        return "Что-то с фото не так."

def split_into_messages(text: str) -> list:
    """Разбивает длинный ответ на несколько коротких сообщений"""
    # Сначала пробуем разбить по двойным переносам
    parts = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    
    if len(parts) >= 2:
        return parts[:4]  # максимум 4 сообщения

    # Если нет явных абзацев — режем по предложениям
    sentences = re.split(r'(?<=[.!?…])\s+', text.strip())
    if len(sentences) <= 2:
        return [text.strip()]

    messages = []
    current = ""
    for s in sentences:
        if len(current) + len(s) < 180:
            current = (current + " " + s).strip()
        else:
            if current:
                messages.append(current)
            current = s
    if current:
        messages.append(current)

    return messages[:4]

async def send_human_like(context, chat_id, connection_id, reply_to, texts: list):
    """Отправляет несколько сообщений с паузами"""
    for i, text in enumerate(texts):
        if not text:
            continue
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                business_connection_id=connection_id,
                reply_to_message_id=reply_to if i == 0 else None,
            )
            print(f"✅ Отправлено: {text[:60]}...")
            
            # Пауза между сообщениями (имитация печати)
            if i < len(texts) - 1:
                await asyncio.sleep(1.2 + len(text) * 0.015)
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")

async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message:
        return

    connection_id = message.business_connection_id
    chat_id = message.chat.id
    user_name = message.from_user.first_name if message.from_user else "Человек"
    user_text = None

    # Текст
    if message.text:
        user_text = message.text

    # Голосовое
    elif message.voice:
        print(f"🎤 [{user_name}] голосовое...")
        try:
            voice_file = await message.voice.get_file()
            path = f"/tmp/{chat_id}_voice.ogg"
            await voice_file.download_to_drive(path)
            user_text = await transcribe_voice(path)
            if os.path.exists(path):
                os.remove(path)
            print(f"📝 → {user_text}")
        except Exception as e:
            print(f"Ошибка голосового: {e}")
            return

    # Фото
    elif message.photo:
        print(f"🖼 [{user_name}] фото...")
        try:
            photo = message.photo[-1]
            photo_file = await photo.get_file()
            path = f"/tmp/{chat_id}_photo.jpg"
            await photo_file.download_to_drive(path)
            user_text = await analyze_image(path, message.caption or "")
            if os.path.exists(path):
                os.remove(path)
            print(f"👁 → {user_text[:80]}...")
        except Exception as e:
            print(f"Ошибка фото: {e}")
            return

    if not user_text:
        return

    print(f"📩 [{user_name}]: {user_text[:80]}")

    # История
    chat_histories[chat_id].append({"role": "user", "content": user_text})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + chat_histories[chat_id]

    # Генерация ответа
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                "temperature": 0.85,
                "max_tokens": 600,
            },
            timeout=40,
        )
        data = resp.json()
        if "choices" in data and data["choices"]:
            raw_answer = data["choices"][0]["message"]["content"].strip()
        else:
            raw_answer = "хм, что-то пошло не так"
            print(data)
    except Exception as e:
        raw_answer = "сейчас немного туплю, повтори"
        print(e)

    # Сохраняем в историю цельный ответ
    chat_histories[chat_id].append({"role": "assistant", "content": raw_answer})

    # Разбиваем и отправляем как человек
    parts = split_into_messages(raw_answer)
    await send_human_like(context, chat_id, connection_id, message.message_id, parts)

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )
    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))

    print("Бот запущен (живой стиль)...")
    app.run_polling(
        allowed_updates=["business_connection", "business_message"],
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
