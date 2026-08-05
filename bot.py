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

# Имя бота (для упоминаний в группе)
BOT_NAME = "мику"
BOT_USERNAMES = ["groooooook_bot"]  # свой username бота без @ (можно несколько)

HISTORY_FILE = "history.json"
FACTS_FILE = "facts.json"
BLACKLIST_FILE = "blacklist.json"
SETTINGS_FILE = "settings.json"
MAX_HISTORY = 18

VOICE = "ru-RU-SvetlanaNeural"  # женский голос
REACTIONS = ["❤️", "🔥", "😂", "👍", "😮", "👏", "💯", "😍"]

request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)

BASE_PROMPT = """Ты — Мику, милая аниме-тян.
Общаешься мило, эмоционально и по-девчачьи, как персонаж из аниме.
Используй лёгкий аниме-стиль: «ня», «кья», «уву», «хехе», «эхехе», «бака» (редко и по делу), тянущиеся гласные (нееет, дааа).
Можно эмодзи, но не перебарщивай.
Говори коротко, живо, как в переписке.
Никогда не говори, что ты бот, ИИ или программа.
Если знаешь факты о человеке — учитывай их естественно.
Отвечай на русском.
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
settings = load_json(SETTINGS_FILE, {"role": None})

chat_histories = defaultdict(list, {str(k): v for k, v in raw_history.items()})
user_facts = defaultdict(list, {str(k): v for k, v in raw_facts.items()})
BLACKLIST = set(str(x) for x in raw_blacklist)

def save_all():
    save_json(HISTORY_FILE, dict(chat_histories))
    save_json(FACTS_FILE, dict(user_facts))
    save_json(BLACKLIST_FILE, list(BLACKLIST))
    save_json(SETTINGS_FILE, settings)

def is_mentioned(text: str, bot_username: str = None) -> bool:
    if not text:
        return False
    text_l = text.lower()
    if BOT_NAME in text_l:
        return True
    for uname in BOT_USERNAMES:
        if f"@{uname.lower()}" in text_l:
            return True
    if bot_username and f"@{bot_username.lower()}" in text_l:
        return True
    return False

def clean_mention(text: str) -> str:
    """Убирает упоминание бота из текста"""
    text = re.sub(r'@\w+', '', text, flags=re.IGNORECASE)
    text = re.sub(rf'\b{BOT_NAME}\b', '', text, flags=re.IGNORECASE)
    return text.strip(" ,.-!?")

async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    if bc.is_enabled:
        print(f"✅ Business подключен: {bc.user.first_name}")
    else:
        print("❌ Business отключен")

async def call_groq(messages, model="llama-3.3-70b-versatile", temperature=0.9, max_tokens=450):
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
        prompt = caption or "Что на фото? Опиши мило и коротко, как аниме-тян."

        result = await call_groq(
            [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img}"}}
                ]
            }],
            model="qwen/qwen3.6-27b",
            temperature=0.6,
            max_tokens=300,
        )
        return result or "не понииимаю что на фото >_<"
    except Exception as e:
        print("Vision error:", e)
        return "ой, с фото что-то не так..."

async def detect_mood(text: str) -> str:
    result = await call_groq(
        [
            {
                "role": "system",
                "content": (
                    "Определи настроение одним словом: "
                    "нейтральный, весёлый, грустный, злой, усталый, влюблённый, тревожный, шутит. "
                    "Только одно слово."
                )
            },
            {"role": "user", "content": text}
        ],
        model="llama-3.1-8b-instant",
        temperature=0.1,
        max_tokens=10,
    )
    return (result or "нейтральный").lower().strip()

async def extract_facts(chat_id: str, text: str):
    if len(text) < 30 or random.random() > 0.4:
        return

    result = await call_groq(
        [
            {
                "role": "system",
                "content": (
                    "Извлеки явные факты о человеке. "
                    "Если нет — пустая строка. Каждый факт с новой строки."
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
        if len(current) + len(s) < 140:
            current = (current + " " + s).strip()
        else:
            if current:
                result.append(current)
            current = s
    if current:
        result.append(current)
    return result[:3]

async def maybe_react(context, chat_id, message_id, connection_id=None):
    if random.random() > 0.3:
        return
    try:
        emoji = random.choice(REACTIONS)
        kwargs = {
            "chat_id": int(chat_id),
            "message_id": message_id,
            "reaction": [ReactionTypeEmoji(emoji)],
        }
        if connection_id:
            kwargs["business_connection_id"] = connection_id
        await context.bot.set_message_reaction(**kwargs)
        print(f"🎭 {emoji}")
    except Exception as e:
        print("Реакция:", e)

async def text_to_voice(text: str, path: str):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(path)

def should_reply_with_voice(is_voice_input: bool, answer: str) -> bool:
    if not is_voice_input:
        return False
    return len(answer) < 160

def build_system_prompt(chat_id: str, mood: str) -> str:
    prompt = BASE_PROMPT

    if settings.get("role"):
        prompt += f"\n\nДополнительно сейчас ты в роли: {settings['role']}"

    mood_hints = {
        "злой": "Собеседник злится. Отвечай мягко, не спорь, постарайся успокоить.",
        "грустный": "Собеседник грустит. Будь особенно милой и поддерживающей.",
        "весёлый": "Собеседник в хорошем настроении. Можно быть ещё более игривой.",
        "усталый": "Собеседник устал. Отвечай коротко и нежно.",
        "тревожный": "Собеседник волнуется. Будь спокойной и заботливой.",
        "шутит": "Собеседник шутит. Можно ответить в том же лёгком тоне.",
        "влюблённый": "Собеседник в романтичном настроении. Можно чуть смущаться и быть милой.",
    }
    if mood in mood_hints:
        prompt += f"\n\n{mood_hints[mood]}"

    facts = user_facts.get(chat_id, [])
    if facts:
        prompt += "\n\nЧто ты знаешь об этом человеке:\n- " + "\n- ".join(facts[-8:])

    return prompt

async def handle_commands(text: str, chat_id: str, context, connection_id=None) -> bool:
    text_l = text.strip().lower()
    kwargs = {"chat_id": int(chat_id)}
    if connection_id:
        kwargs["business_connection_id"] = connection_id

    if text_l.startswith("/role ") or text_l.startswith("роль "):
        role = text.split(" ", 1)[1].strip()
        settings["role"] = role if role.lower() not in ["сброс", "off", "нет"] else None
        save_all()
        msg = f"роль: {settings['role']}~" if settings["role"] else "роль сброшена~"
        await context.bot.send_message(text=msg, **kwargs)
        return True

    if text_l in ["/summary", "саммари", "о чём говорили"]:
        history = chat_histories.get(chat_id, [])
        if not history:
            await context.bot.send_message(text="пока не о чем вспоминать~", **kwargs)
            return True

        summary = await call_groq(
            [
                {
                    "role": "system",
                    "content": "Кратко саммаризируй диалог на русском в 2-3 предложениях, мило, как аниме-тян."
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
            temperature=0.5,
            max_tokens=180,
        )
        await context.bot.send_message(text=summary or "не смогла вспомнить >_<", **kwargs)
        return True

    if text_l in ["/clear", "очистить", "забудь"]:
        chat_histories[chat_id] = []
        user_facts[chat_id] = []
        save_all()
        await context.bot.send_message(text="хорошо, всё забыла~", **kwargs)
        return True

    return False

async def process_message(
    context,
    chat_id: str,
    user_name: str,
    user_text: str,
    message_id: int,
    connection_id=None,
    is_voice_input: bool = False,
    is_group: bool = False,
):
    print(f"📩 [{'группа' if is_group else 'личка'}] [{user_name}]: {user_text[:80]}")

    await maybe_react(context, chat_id, message_id, connection_id)
    await extract_facts(chat_id, user_text)
    mood = await detect_mood(user_text)
    print(f"😊 Настроение: {mood}")

    chat_histories[chat_id].append({"role": "user", "content": user_text})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]

    system_prompt = build_system_prompt(chat_id, mood)
    messages = [{"role": "system", "content": system_prompt}] + chat_histories[chat_id]

    answer = await call_groq(messages, temperature=0.92, max_tokens=400)
    if not answer:
        answer = "эээ... что-то я затупила >_<"

    chat_histories[chat_id].append({"role": "assistant", "content": answer})
    save_all()

    use_voice = should_reply_with_voice(is_voice_input, answer) and not is_group

    send_kwargs = {"chat_id": int(chat_id)}
    if connection_id:
        send_kwargs["business_connection_id"] = connection_id

    try:
        action = ChatAction.RECORD_VOICE if use_voice else ChatAction.TYPING
        await context.bot.send_chat_action(action=action, **send_kwargs)

        if use_voice:
            voice_path = f"/tmp/{chat_id}_answer.mp3"
            await text_to_voice(answer, voice_path)
            with open(voice_path, "rb") as vf:
                await context.bot.send_voice(
                    voice=vf,
                    reply_to_message_id=message_id,
                    **send_kwargs,
                )
            if os.path.exists(voice_path):
                os.remove(voice_path)
            print(f"🎤 → {user_name}")
        else:
            parts = split_messages(answer)
            for i, part in enumerate(parts):
                await context.bot.send_chat_action(action=ChatAction.TYPING, **send_kwargs)
                await asyncio.sleep(0.5 + len(part) * 0.01)
                await context.bot.send_message(
                    text=part,
                    reply_to_message_id=message_id if i == 0 else None,
                    **send_kwargs,
                )
                if i < len(parts) - 1:
                    await asyncio.sleep(0.65)
            print(f"✅ {len(parts)} сообщ. → {user_name}")

    except Exception as e:
        print("Ошибка отправки:", e)

async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message:
        return

    connection_id = message.business_connection_id
    chat_id = str(message.chat.id)
    user_name = message.from_user.first_name if message.from_user else "человек"
    user_text = None
    is_voice_input = False

    if chat_id in BLACKLIST:
        return

    if message.text:
        user_text = message.text
        if await handle_commands(user_text, chat_id, context, connection_id):
            return

    elif message.voice:
        is_voice_input = True
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

    elif message.photo:
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

    await process_message(
        context, chat_id, user_name, user_text,
        message.message_id, connection_id, is_voice_input, is_group=False
    )

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    # Только группы/супергруппы
    if message.chat.type not in ["group", "supergroup"]:
        return

    chat_id = str(message.chat.id)
    user_name = message.from_user.first_name if message.from_user else "человек"
    text = message.text

    if chat_id in BLACKLIST:
        return

    # Отвечаем только если упомянули
    bot_info = context.bot
    bot_username = (await bot_info.get_me()).username if bot_info else None
    if not is_mentioned(text, bot_username):
        return

    # Команды
    if await handle_commands(text, chat_id, context):
        return

    user_text = clean_mention(text)
    if not user_text:
        user_text = "привет"

    await process_message(
        context, chat_id, user_name, user_text,
        message.message_id, connection_id=None,
        is_voice_input=False, is_group=True
    )

def main():
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, on_group_message))

    print("Мику запущена~")
    app.run_polling(
        allowed_updates=["business_connection", "business_message", "message"],
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
