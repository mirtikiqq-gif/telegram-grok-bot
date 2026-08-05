import os
import re
import json
import random
import base64
import asyncio
import requests
import edge_tts
from datetime import datetime
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

# Твой Telegram ID (чтобы команды /off /on /role работали только от тебя)
# Узнать можно у @userinfobot
OWNER_ID = os.getenv("OWNER_ID")  # строка, например "123456789"

HISTORY_FILE = "history.json"
FACTS_FILE = "facts.json"
BLACKLIST_FILE = "blacklist.json"
SETTINGS_FILE = "settings.json"
MAX_HISTORY = 18

VOICE = "ru-RU-DmitryNeural"
REACTIONS = ["👍", "🔥", "😂", "❤️", "😮", "👏", "💯"]

request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)

BASE_PROMPT = """Ты обычный живой человек в Telegram.
Говори естественно, коротко и по-человечески.
Не пиши одной длинной простынёй — лучше 1-3 коротких сообщения.
Можно использовать сленг.
Никогда не говори, что ты бот, ИИ или ассистент.
Если знаешь факты о человеке — учитывай их естественно.
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

raw_history = load_json(HISTORY_FILE, {})
raw_facts = load_json(FACTS_FILE, {})
raw_blacklist = load_json(BLACKLIST_FILE, [])
settings = load_json(SETTINGS_FILE, {"enabled": True, "role": None})

chat_histories = defaultdict(list, {str(k): v for k, v in raw_history.items()})
user_facts = defaultdict(list, {str(k): v for k, v in raw_facts.items()})
BLACKLIST = set(str(x) for x in raw_blacklist)

def save_all():
    save_json(HISTORY_FILE, dict(chat_histories))
    save_json(FACTS_FILE, dict(user_facts))
    save_json(BLACKLIST_FILE, list(BLACKLIST))
    save_json(SETTINGS_FILE, settings)

async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    if bc.is_enabled:
        print(f"✅ Подключено: {bc.user.first_name}")
    else:
        print("❌ Отключено")

async def call_groq(messages, model="llama-3.3-70b-versatile", temperature=0.85, max_tokens=500):
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=45,
        )
        data = resp.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"].strip()
        print("Groq error:", data)
        return None
    except Exception as e:
        print("Groq exception:", e)
        return None

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
        print("STT error:", e)
        return ""

async def analyze_image(file_path: str, caption: str = "") -> str:
    try:
        with open(file_path, "rb") as f:
            img = base64.b64encode(f.read()).decode()
        mime = "image/png" if file_path.endswith(".png") else "image/jpeg"
        prompt = caption or "Что на фото? Опиши коротко и по-человечески."

        result = await call_groq(
            [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img}"}}
                ]
            }],
            model="qwen/qwen3.6-27b",
            temperature=0.5,
            max_tokens=350,
        )
        return result or "не понял фото"
    except Exception as e:
        print("Vision error:", e)
        return "что-то с фото"

async def detect_mood(text: str) -> str:
    """Определяет настроение человека"""
    result = await call_groq(
        [
            {
                "role": "system",
                "content": (
                    "Определи настроение сообщения одним словом: "
                    "нейтральный, весёлый, грустный, злой, усталый, влюблённый, тревожный, шутит. "
                    "Ответь только одним словом."
                )
            },
            {"role": "user", "content": text}
        ],
        model="llama-3.1-8b-instant",
        temperature=0.1,
        max_tokens=10,
    )
    return (result or "нейтральный").lower().strip()

async def should_reply(text: str) -> bool:
    """Решает, нужно ли вообще отвечать"""
    # Короткие реакции часто не требуют ответа
    if text.lower().strip() in ["ок", "окей", "ладно", "ага", "угу", "понял", "ясно", "спс", "спасибо", "👍", "😂", "🔥"]:
        return random.random() < 0.35

    result = await call_groq(
        [
            {
                "role": "system",
                "content": (
                    "Нужно ли отвечать на это сообщение в переписке? "
                    "Ответь только YES или NO. "
                    "NO — если это просто реакция, подтверждение, стикер-текст или сообщение не требует ответа."
                )
            },
            {"role": "user", "content": text}
        ],
        model="llama-3.1-8b-instant",
        temperature=0.1,
        max_tokens=5,
    )
    return result is None or "YES" in (result or "").upper()

async def extract_facts(chat_id: str, text: str):
    if len(text) < 30 or random.random() > 0.4:
        return

    result = await call_groq(
        [
            {
                "role": "system",
                "content": (
                    "Извлеки только явные факты о человеке. "
                    "Если фактов нет — верни пустую строку. "
                    "Каждый факт с новой строки, коротко."
                )
            },
            {"role": "user", "content": text}
        ],
        model="llama-3.1-8b-instant",
        temperature=0.1,
        max_tokens=120,
    )

    if not result or result.lower() in ["нет", "нет фактов", "пусто"]:
        return

    for line in result.split("\n"):
        fact = line.strip(" -•*").strip()
        if fact and len(fact) > 3 and fact not in user_facts[chat_id]:
            user_facts[chat_id].append(fact)
            print(f"📌 Факт [{chat_id}]: {fact}")

    user_facts[chat_id] = user_facts[chat_id][-12:]
    save_all()

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

    result, current = [], ""
    for s in sentences:
        if len(current) + len(s) < 150:
            current = (current + " " + s).strip()
        else:
            if current:
                result.append(current)
            current = s
    if current:
        result.append(current)
    return result[:3]

async def maybe_react(context, chat_id, message_id, connection_id):
    if random.random() > 0.25:
        return
    try:
        emoji = random.choice(REACTIONS)
        await context.bot.set_message_reaction(
            chat_id=int(chat_id),
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji)],
            business_connection_id=connection_id
        )
        print(f"🎭 {emoji}")
    except Exception as e:
        print("Реакция:", e)

async def text_to_voice(text: str, path: str):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(path)

def should_reply_with_voice(is_voice_input: bool, answer: str) -> bool:
    if not is_voice_input:
        return False
    return len(answer) < 180

def build_system_prompt(chat_id: str, mood: str) -> str:
    prompt = BASE_PROMPT

    # Роль
    if settings.get("role"):
        prompt += f"\n\nСейчас ты в роли: {settings['role']}"

    # Настроение
    mood_hints = {
        "злой": "Человек раздражён или зол. Отвечай спокойно, коротко, без лишних шуток.",
        "грустный": "Человек грустит. Будь мягче и теплее обычного.",
        "весёлый": "Человек в хорошем настроении. Можно пошутить и быть легче.",
        "усталый": "Человек устал. Отвечай коротко и по делу.",
        "тревожный": "Человек волнуется. Будь спокойным и поддерживающим.",
        "шутит": "Человек шутит. Можно ответить в том же тоне.",
    }
    if mood in mood_hints:
        prompt += f"\n\n{mood_hints[mood]}"

    # Факты
    facts = user_facts.get(chat_id, [])
    if facts:
        prompt += "\n\nЧто ты знаешь об этом человеке:\n- " + "\n- ".join(facts[-8:])

    return prompt

async def handle_owner_commands(text: str, chat_id: str, context, connection_id) -> bool:
    """Обработка команд владельца. Возвращает True, если команда обработана."""
    text = text.strip().lower()

    if text in ["/off", "выкл", "выключить"]:
        settings["enabled"] = False
        save_all()
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="бот выключен",
            business_connection_id=connection_id,
        )
        return True

    if text in ["/on", "вкл", "включить"]:
        settings["enabled"] = True
        save_all()
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="бот включен",
            business_connection_id=connection_id,
        )
        return True

    if text.startswith("/role ") or text.startswith("роль "):
        role = text.split(" ", 1)[1].strip()
        settings["role"] = role if role not in ["сброс", "off", "нет"] else None
        save_all()
        msg = f"роль: {settings['role']}" if settings["role"] else "роль сброшена"
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=msg,
            business_connection_id=connection_id,
        )
        return True

    if text in ["/summary", "саммари", "о чём говорили"]:
        history = chat_histories.get(chat_id, [])
        if not history:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text="пока не о чем вспоминать",
                business_connection_id=connection_id,
            )
            return True

        summary = await call_groq(
            [
                {
                    "role": "system",
                    "content": "Кратко саммаризируй диалог на русском в 2-4 предложениях. Пиши как человек."
                },
                {
                    "role": "user",
                    "content": "\n".join(
                        f"{'Я' if m['role']=='assistant' else 'Он'}: {m['content']}"
                        for m in history[-12:]
                    )
                }
            ],
            model="llama-3.1-8b-instant",
            temperature=0.4,
            max_tokens=200,
        )
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=summary or "не смог вспомнить",
            business_connection_id=connection_id,
        )
        return True

    if text in ["/clear", "очистить", "забудь"]:
        chat_histories[chat_id] = []
        user_facts[chat_id] = []
        save_all()
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="окей, всё забыл",
            business_connection_id=connection_id,
        )
        return True

    return False

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

    # Бот выключен
    if not settings.get("enabled", True):
        return

    # Текст / команды
    if message.text:
        user_text = message.text

        # Команды (работают для всех, но особенно полезны тебе)
        if await handle_owner_commands(user_text, chat_id, context, connection_id):
            return

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

    print(f"📩 [{user_name}]: {user_text[:80]}")

    # Иногда не отвечаем
    if not await should_reply(user_text):
        print("🤫 Решил не отвечать")
        await maybe_react(context, chat_id, message.message_id, connection_id)
        return

    # Реакция
    await maybe_react(context, chat_id, message.message_id, connection_id)

    # Факты + настроение
    await extract_facts(chat_id, user_text)
    mood = await detect_mood(user_text)
    print(f"😊 Настроение: {mood}")

    # История
    chat_histories[chat_id].append({"role": "user", "content": user_text})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]

    system_prompt = build_system_prompt(chat_id, mood)
    messages = [{"role": "system", "content": system_prompt}] + chat_histories[chat_id]

    answer = await call_groq(messages, temperature=0.88, max_tokens=450)
    if not answer:
        answer = "хм"

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
            print(f"🎤 → {user_name}")
        else:
            parts = split_messages(answer)
            for i, part in enumerate(parts):
                await context.bot.send_chat_action(
                    chat_id=int(chat_id),
                    action=ChatAction.TYPING,
                    business_connection_id=connection_id
                )
                await asyncio.sleep(0.55 + len(part) * 0.01)
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=part,
                    business_connection_id=connection_id,
                    reply_to_message_id=message.message_id if i == 0 else None,
                )
                if i < len(parts) - 1:
                    await asyncio.sleep(0.7)
            print(f"✅ {len(parts)} сообщ. → {user_name}")

    except Exception as e:
        print("Ошибка отправки:", e)

def main():
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))

    print("Бот запущен (умные фичи + вау)...")
    print(f"Статус: {'ВКЛ' if settings.get('enabled', True) else 'ВЫКЛ'}")
    app.run_polling(
        allowed_updates=["business_connection", "business_message"],
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
