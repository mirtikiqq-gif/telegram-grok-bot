import os
import re
import json
import random
import base64
import asyncio
import requests
import edge_tts
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
from telegram.constants import ChatAction

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

HISTORY_FILE = "history.json"
FACTS_FILE = "facts.json"
BLACKLIST_FILE = "blacklist.json"
MAX_HISTORY = 16

VOICE = "ru-RU-DmitryNeural"
REACTIONS = ["👍", "🔥", "😂", "❤️", "😮", "👏", "💯"]

request = HTTPXRequest(
    connect_timeout=30.0,
    read_timeout=30.0,
    write_timeout=30.0,
    pool_timeout=30.0
)

SYSTEM_PROMPT = """Ты обычный живой человек в Telegram.
Говори естественно, коротко и по-человечески.
Не пиши одной длинной простынёй — лучше 1-3 коротких сообщения.
Можно использовать сленг.
Не говори, что ты бот или ИИ.
Если знаешь факты о человеке — учитывай их естественно, не перечисляй их прямо.
"""

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"Ошибка загрузки {path}:", e)
    return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения {path}:", e)

# Всегда храним ключи как строки
raw_history = load_json(HISTORY_FILE, {})
raw_facts = load_json(FACTS_FILE, {})
raw_blacklist = load_json(BLACKLIST_FILE, [])

chat_histories = defaultdict(list, {str(k): v for k, v in raw_history.items()})
user_facts = defaultdict(list, {str(k): v for k, v in raw_facts.items()})
BLACKLIST = set(str(x) for x in raw_blacklist)

def save_all():
    save_json(HISTORY_FILE, dict(chat_histories))
    save_json(FACTS_FILE, dict(user_facts))
    save_json(BLACKLIST_FILE, list(BLACKLIST))

async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    if bc.is_enabled:
        print(f"✅ Подключено: {bc.user.first_name}")
    else:
        print("❌ Отключено")

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
        print("Ошибка STT:", e)
        return ""

async def analyze_image(file_path: str, caption: str = "") -> str:
    try:
        with open(file_path, "rb") as f:
            img = base64.b64encode(f.read()).decode()
        mime = "image/png" if file_path.endswith(".png") else "image/jpeg"
        prompt = caption or "Что на фото? Коротко и по-человечески."

        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
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
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"]
        return "не понял фото"
    except Exception as e:
        print("Ошибка фото:", e)
        return "что-то с фото"

async def extract_facts(chat_id: str, text: str):
    """Извлекает факты только если сообщение достаточно информативное"""
    if len(text) < 25:
        return

    # Не на каждое сообщение
    if random.random() > 0.45:
        return

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Извлеки только явные факты о человеке. "
                            "Если фактов нет — верни пустую строку. "
                            "Пиши каждый факт с новой строки, коротко. "
                            "Примеры: Живёт в Москве\nЛюбит аниме\nУчится на программиста"
                        )
                    },
                    {"role": "user", "content": text}
                ],
                "temperature": 0.1,
                "max_tokens": 120,
            },
            timeout=20,
        )
        data = resp.json()
        if "choices" not in data:
            return

        raw = data["choices"][0]["message"]["content"].strip()
        if not raw or raw.lower() in ["нет", "нет фактов", "пусто", ""]:
            return

        for line in raw.split("\n"):
            fact = line.strip(" -•*").strip()
            if fact and len(fact) > 3 and fact not in user_facts[chat_id]:
                user_facts[chat_id].append(fact)
                print(f"📌 Факт [{chat_id}]: {fact}")

        # Храним только последние 12 фактов
        user_facts[chat_id] = user_facts[chat_id][-12:]
        save_all()

    except Exception as e:
        print("Ошибка фактов:", e)

def split_messages(text: str) -> list:
    text = text.strip()
    if not text:
        return []

    parts = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    if len(parts) >= 2:
        return parts[:3]

    sentences = re.split(r'(?<=[.!?…])\s+', text)
    if len(sentences) <= 2:
        return [text]

    result = []
    current = ""
    for s in sentences:
        if len(current) + len(s) < 155:
            current = (current + " " + s).strip()
        else:
            if current:
                result.append(current)
            current = s
    if current:
        result.append(current)

    return result[:3]

async def maybe_react(context, chat_id, message_id, connection_id):
    """Иногда ставит реакцию (не на каждое сообщение)"""
    if random.random() > 0.27:
        return

    try:
        emoji = random.choice(REACTIONS)
        await context.bot.set_message_reaction(
            chat_id=int(chat_id),
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji)],
            business_connection_id=connection_id
        )
        print(f"🎭 Реакция: {emoji}")
    except Exception as e:
        # В Business режиме реакции могут быть ограничены
        print(f"Реакция не поставилась: {e}")

async def text_to_voice(text: str, path: str):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(path)

def should_reply_with_voice(is_voice_input: bool, answer: str) -> bool:
    if not is_voice_input:
        return False
    return len(answer) < 180

async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message:
        return

    connection_id = message.business_connection_id
    chat_id = str(message.chat.id)
    user_name = message.from_user.first_name if message.from_user else "Человек"
    user_text = None
    is_voice_input = False

    if chat_id in BLACKLIST:
        return

    # Команда очистки
    if message.text and message.text.strip().lower() in ["/clear", "очистить", "забудь"]:
        chat_histories[chat_id] = []
        user_facts[chat_id] = []
        save_all()
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="окей, всё забыл",
            business_connection_id=connection_id,
        )
        return

    # Текст
    if message.text:
        user_text = message.text

    # Голосовое
    elif message.voice:
        is_voice_input = True
        print(f"🎤 {user_name}")
        try:
            f = await message.voice.get_file()
            path = f"/tmp/{chat_id}_v.ogg"
            await f.download_to_drive(path)
            user_text = await transcribe_voice(path)
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print("Ошибка голосового:", e)
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
            print("Ошибка фото:", e)
            return

    if not user_text:
        return

    print(f"📩 [{user_name}]: {user_text[:70]}")

    # Реакция (иногда)
    await maybe_react(context, chat_id, message.message_id, connection_id)

    # Факты
    await extract_facts(chat_id, user_text)

    # История
    chat_histories[chat_id].append({"role": "user", "content": user_text})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]

    # Факты в промпт
    facts = user_facts.get(chat_id, [])
    facts_text = ""
    if facts:
        facts_text = "\n\nЧто ты знаешь об этом человеке:\n- " + "\n- ".join(facts[-8:])

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + facts_text}
    ] + chat_histories[chat_id]

    # Генерация ответа
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                "temperature": 0.88,
                "max_tokens": 450,
            },
            timeout=40,
        )
        data = resp.json()
        if "choices" in data and data["choices"]:
            answer = data["choices"][0]["message"]["content"].strip()
        else:
            answer = "хм"
            print("Ошибка API:", data)
    except Exception as e:
        answer = "сейчас туплю"
        print("Ошибка запроса:", e)

    chat_histories[chat_id].append({"role": "assistant", "content": answer})
    save_all()

    use_voice = should_reply_with_voice(is_voice_input, answer)

    try:
        await context.bot.send_chat_action(
            chat_id=int(chat_id),
            action=ChatAction.RECORD_VOICE if use_voice else ChatAction.TYPING,
            business_connection_id=connection_id
        )

        if use_voice:
            voice_path = f"/tmp/{chat_id}_answer.mp3"
            await text_to_voice(answer, voice_path)
            with open(voice_path, "rb") as vf:
                await context.bot.send_voice(
                    chat_id=int(chat_id),
                    voice=vf,
                    business_connection_id=connection_id,
                    reply_to_message_id=message.message_id,
                )
            if os.path.exists(voice_path):
                os.remove(voice_path)
            print(f"🎤 Голосовой → {user_name}")
        else:
            parts = split_messages(answer)
            for i, part in enumerate(parts):
                await context.bot.send_chat_action(
                    chat_id=int(chat_id),
                    action=ChatAction.TYPING,
                    business_connection_id=connection_id
                )
                await asyncio.sleep(0.6 + len(part) * 0.011)
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=part,
                    business_connection_id=connection_id,
                    reply_to_message_id=message.message_id if i == 0 else None,
                )
                if i < len(parts) - 1:
                    await asyncio.sleep(0.75)
            print(f"✅ Текст ({len(parts)} сообщ.) → {user_name}")

    except Exception as e:
        print("Ошибка отправки:", e)
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=answer,
                business_connection_id=connection_id,
            )
        except:
            pass

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )
    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))

    print("Бот запущен (факты + реакции + несколько сообщений)...")
    app.run_polling(
        allowed_updates=["business_connection", "business_message"],
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
