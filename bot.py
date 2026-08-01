import os
import re
import json
import base64
import asyncio
import requests
from collections import defaultdict
from telegram import Update, ReactionTypeEmoji
from telegram.ext import (
    Application,
    MessageHandler,
    BusinessConnectionHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError
from telegram.constants import ChatAction

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# === Настройки ===
HISTORY_FILE = "history.json"
BLACKLIST_FILE = "blacklist.json"
MAX_HISTORY = 16

# ID людей, которых бот должен игнорировать (добавляй сюда chat_id)
# Узнать chat_id можно в логах
BLACKLIST = set()

request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)

SYSTEM_PROMPT = """Ты обычный живой человек в Telegram.
Говори естественно, коротко и по-человечески.
Не пиши длинными простынями. Лучше 1-3 коротких сообщения.
Можно использовать сленг и лёгкие эмодзи.
Не говори, что ты бот или ИИ.
Если вопрос простой — отвечай коротко.
"""

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Ошибка сохранения:", e)

# Загружаем историю и чёрный список
chat_histories = defaultdict(list, load_json(HISTORY_FILE, {}))
BLACKLIST = set(load_json(BLACKLIST_FILE, []))

def save_history():
    save_json(HISTORY_FILE, dict(chat_histories))

def save_blacklist():
    save_json(BLACKLIST_FILE, list(BLACKLIST))

async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    if bc.is_enabled:
        print(f"✅ Подключено: {bc.user.first_name} | {bc.id}")
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
        return resp.json().get("text", "")
    except Exception as e:
        print("Ошибка голосового:", e)
        return ""

async def analyze_image(file_path: str, caption: str = "") -> str:
    try:
        with open(file_path, "rb") as f:
            img = base64.b64encode(f.read()).decode()
        mime = "image/png" if file_path.endswith(".png") else "image/jpeg"
        prompt = caption or "Что на фото? Опиши коротко и по-человечески."

        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "qwen/qwen3.6-27b",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img}"}}
                    ]
                }],
                "temperature": 0.6,
                "max_tokens": 400,
            },
            timeout=60,
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"] if "choices" in data else "не понял фото"
    except Exception as e:
        print("Ошибка фото:", e)
        return "что-то с фото"

def split_messages(text: str) -> list:
    parts = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    if len(parts) >= 2:
        return parts[:3]
    sentences = re.split(r'(?<=[.!?…])\s+', text.strip())
    if len(sentences) <= 2:
        return [text.strip()]
    result, current = [], ""
    for s in sentences:
        if len(current) + len(s) < 160:
            current = (current + " " + s).strip()
        else:
            if current:
                result.append(current)
            current = s
    if current:
        result.append(current)
    return result[:3]

async def send_human(context, chat_id, connection_id, reply_to, texts):
    for i, text in enumerate(texts):
        if not text:
            continue
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=connection_id)
            await asyncio.sleep(0.8 + len(text) * 0.012)
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                business_connection_id=connection_id,
                reply_to_message_id=reply_to if i == 0 else None,
            )
            print(f"✅ {text[:50]}...")
            if i < len(texts) - 1:
                await asyncio.sleep(0.9)
        except Exception as e:
            print("Ошибка отправки:", e)

async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message:
        return

    connection_id = message.business_connection_id
    chat_id = message.chat.id
    user_name = message.from_user.first_name if message.from_user else "Человек"
    user_text = None

    # Чёрный список
    if chat_id in BLACKLIST:
        print(f"⛔ Игнор {user_name} ({chat_id})")
        return

    # Команда /clear
    if message.text and message.text.strip().lower() in ["/clear", "очистить", "забудь"]:
        chat_histories[chat_id] = []
        save_history()
        await context.bot.send_message(
            chat_id=chat_id,
            text="окей, забыл наш разговор",
            business_connection_id=connection_id,
        )
        return

    # Текст
    if message.text:
        user_text = message.text

    # Голосовое
    elif message.voice:
        print(f"🎤 {user_name}")
        try:
            f = await message.voice.get_file()
            path = f"/tmp/{chat_id}_v.ogg"
            await f.download_to_drive(path)
            user_text = await transcribe_voice(path)
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(e)
            return

    # Фото
    elif message.photo:
        print(f"🖼 {user_name}")
        try:
            f = await message.photo[-1].get_file()
            path = f"/tmp/{chat_id}_p.jpg"
            await f.download_to_drive(path)
            user_text = await analyze_image(path, message.caption or "")
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(e)
            return

    if not user_text:
        return

    print(f"📩 [{user_name}]: {user_text[:70]}")

    # История
    chat_histories[chat_id].append({"role": "user", "content": user_text})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + chat_histories[chat_id]

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                "temperature": 0.88,
                "max_tokens": 500,
            },
            timeout=40,
        )
        data = resp.json()
        answer = data["choices"][0]["message"]["content"].strip() if "choices" in data else "хм"
    except Exception as e:
        answer = "сейчас туплю немного"
        print(e)

    chat_histories[chat_id].append({"role": "assistant", "content": answer})
    save_history()

    parts = split_messages(answer)
    await send_human(context, chat_id, connection_id, message.message_id, parts)

def main():
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))
    print("Бот запущен (живой + память + /clear)...")
    app.run_polling(allowed_updates=["business_connection", "business_message"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
