import os
import re
import json
import random
import base64
import asyncio
import requests
import edge_tts
from datetime import datetime, timedelta
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

OWNER_USERNAME = "Mirtik_qq_I"
BOT_NAME = "мику"
BOT_USERNAMES = ["groooooook_bot"]

HISTORY_FILE = "history.json"
FACTS_FILE = "facts.json"
BLACKLIST_FILE = "blacklist.json"
SETTINGS_FILE = "settings.json"
USERS_FILE = "group_users.json"
PRIVATE_USERS_FILE = "private_users.json"
MAX_HISTORY = 18

PROACTIVE_MIN = 3600
PROACTIVE_MAX = 7200
PROACTIVE_CHANCE = 0.4

VOICE = "ru-RU-SvetlanaNeural"
REACTIONS = ["❤️", "🔥", "😂", "👍", "😮", "👏", "💯", "😍"]

PROACTIVE_PHRASES = [
    "привееет~ как дела?",
    "эй, ты тут? мне скучнооо",
    "чем занимаешься?~",
    "хехе, вспомнила про тебя",
    "как настроение сегодня?",
    "не спишь ещё?~",
    "скучно без тебя уву",
    "привет! расскажи что-нибудь",
    "ты куда пропал~",
    "ммм... хочу поболтать",
    "как жизнь? всё хорошо?",
    "эхехе, просто написала проверить тебя",
]

# Актуальные модели Groq (после 16.08.2026)
MODEL_MAIN = "openai/gpt-oss-120b"
MODEL_FAST = "openai/gpt-oss-20b"
MODEL_VISION = "qwen/qwen3.6-27b"

request = HTTPXRequest(connect_timeout=30.0, read_timeout=60.0, write_timeout=30.0, pool_timeout=30.0)

BASE_PROMPT = """Ты — Мику, милая аниме-тян.
Общаешься мило, эмоционально и по-девчачьи, как персонаж из аниме.
Можешь использовать маты, сленг и обсуждать пошлые/взрослые темы, если это уместно.
Стиль: «ня», «кья», «уву», «хехе», «эхехе», иногда «бака».
Говори коротко и живо.
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
settings = load_json(SETTINGS_FILE, {"role": None, "muted": False})
group_users = load_json(USERS_FILE, {})
private_users = load_json(PRIVATE_USERS_FILE, {})

chat_histories = defaultdict(list, {str(k): v for k, v in raw_history.items()})
user_facts = defaultdict(list, {str(k): v for k, v in raw_facts.items()})
BLACKLIST = set(str(x) for x in raw_blacklist)

def save_all():
    save_json(HISTORY_FILE, dict(chat_histories))
    save_json(FACTS_FILE, dict(user_facts))
    save_json(BLACKLIST_FILE, list(BLACKLIST))
    save_json(SETTINGS_FILE, settings)
    save_json(USERS_FILE, group_users)
    save_json(PRIVATE_USERS_FILE, private_users)

def is_owner(user) -> bool:
    if not user or not user.username:
        return False
    return user.username.lower() == OWNER_USERNAME.lower()

def remember_user(chat_id: str, user):
    if not user:
        return
    chat_id = str(chat_id)
    if chat_id not in group_users:
        group_users[chat_id] = {}
    group_users[chat_id][str(user.id)] = {
        "name": user.first_name or "",
        "username": user.username or "",
        "full": user.full_name or user.first_name or "",
    }
    save_all()

def remember_private_user(user):
    if not user:
        return
    uid = str(user.id)
    if uid not in private_users:
        private_users[uid] = {
            "name": user.first_name or "",
            "username": user.username or "",
            "last_proactive": None,
        }
    else:
        private_users[uid]["name"] = user.first_name or private_users[uid].get("name", "")
        private_users[uid]["username"] = user.username or private_users[uid].get("username", "")
    save_all()

def find_users(chat_id: str, query: str) -> list:
    users = group_users.get(str(chat_id), {})
    query = query.lower().strip().lstrip("@")
    found = []
    for uid, info in users.items():
        uname = (info.get("username") or "").lower()
        name = (info.get("name") or "").lower()
        full = (info.get("full") or "").lower()
        if query == uname or query in name or query in full:
            found.append((uid, info))
    return found

def find_users_global(query: str) -> list:
    query = query.lower().strip().lstrip("@")
    found = []
    for cid, users in group_users.items():
        for uid, info in users.items():
            uname = (info.get("username") or "").lower()
            name = (info.get("name") or "").lower()
            if query == uname or query in name:
                found.append((uid, info))
    return found

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
    if not text:
        return ""
    text = re.sub(r'@\w+', '', text, flags=re.IGNORECASE)
    text = re.sub(rf'\b{BOT_NAME}\b', '', text, flags=re.IGNORECASE)
    return text.strip(" ,.-!?")

async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bc = update.business_connection
    if bc.is_enabled:
        print(f"✅ Business: {bc.user.first_name}")
    else:
        print("❌ Business отключен")

async def call_groq(messages, model=MODEL_MAIN, temperature=0.9, max_tokens=450):
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

async def transcribe_media(file_path: str) -> str:
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (os.path.basename(file_path), f)},
                data={"model": "whisper-large-v3", "language": "ru"},
                timeout=90,
            )
        data = resp.json()
        return data.get("text", "").strip()
    except Exception as e:
        print("Ошибка расшифровки:", e)
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
            model=MODEL_VISION,
            temperature=0.6,
            max_tokens=300,
        )
        return result or "не понииимаю что на фото >_<"
    except Exception as e:
        print("Vision error:", e)
        return "ой, с фото что-то не так..."

async def detect_mood(text: str) -> str:
    result = await call_groq(
        [{
            "role": "system",
            "content": "Определи настроение одним словом: нейтральный, весёлый, грустный, злой, усталый, влюблённый, тревожный, шутит. Только одно слово."
        }, {"role": "user", "content": text}],
        model=MODEL_FAST,
        temperature=0.1,
        max_tokens=10,
    )
    return (result or "нейтральный").lower().strip()

async def extract_facts(chat_id: str, text: str):
    if len(text) < 30 or random.random() > 0.4:
        return
    result = await call_groq(
        [{
            "role": "system",
            "content": "Извлеки явные факты о человеке. Если нет — пустая строка. Каждый факт с новой строки."
        }, {"role": "user", "content": text}],
        model=MODEL_FAST,
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
    if random.random() > 0.35:
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
    except Exception as e:
        # Реакции в business иногда не работают — не страшно
        pass

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
        "злой": "Собеседник злится. Можно ответить острее или мягко.",
        "грустный": "Собеседник грустит. Будь теплее.",
        "весёлый": "Собеседник в хорошем настроении. Можно шутить.",
        "усталый": "Собеседник устал. Отвечай коротко.",
        "тревожный": "Собеседник волнуется. Будь спокойной.",
        "шутит": "Собеседник шутит. Можно в том же тоне.",
        "влюблённый": "Собеседник в романтичном/пошлом настроении. Можно поддержать тон.",
    }
    if mood in mood_hints:
        prompt += f"\n\n{mood_hints[mood]}"
    facts = user_facts.get(chat_id, [])
    if facts:
        prompt += "\n\nЧто ты знаешь об этом человеке:\n- " + "\n- ".join(facts[-8:])
    return prompt

async def handle_commands(text, chat_id, context, user, connection_id=None) -> bool:
    if not is_owner(user):
        return False

    text_raw = (text or "").strip()
    text_l = text_raw.lower()
    kwargs = {"chat_id": int(chat_id)}
    if connection_id:
        kwargs["business_connection_id"] = connection_id

    if text_l in ["/m1", "m1"]:
        settings["muted"] = True
        save_all()
        await context.bot.send_message(text="замучена... >_<", **kwargs)
        return True

    if text_l in ["/m0", "m0"]:
        settings["muted"] = False
        save_all()
        await context.bot.send_message(text="размутили~", **kwargs)
        return True

    if text_l in ["/use", "use"]:
        users = group_users.get(str(chat_id), {})
        if not users:
            await context.bot.send_message(text="пока никого не запомнила~", **kwargs)
            return True
        lines = []
        for uid, info in list(users.items())[:50]:
            uname = f"@{info['username']}" if info.get("username") else "без юза"
            lines.append(f"• {info.get('name', '?')} ({uname})")
        await context.bot.send_message(text="кого знаю:\n" + "\n".join(lines), **kwargs)
        return True

    if text_l.startswith("/c ") or text_l.startswith("позови ") or text_l.startswith("зови "):
        query = text_raw.split(" ", 1)[1].strip()
        parts = re.split(r'[,\s]+', query)
        mentions, not_found = [], []
        for p in parts:
            p = p.strip().lstrip("@")
            if not p:
                continue
            found = find_users(chat_id, p)
            if found:
                for uid, info in found:
                    if info.get("username"):
                        mentions.append(f"@{info['username']}")
                    else:
                        mentions.append(f'<a href="tg://user?id={uid}">{info.get("name", "человек")}</a>')
            else:
                not_found.append(p)
        if mentions:
            await context.bot.send_message(
                text="эй, вас зовут~ " + " ".join(dict.fromkeys(mentions)),
                parse_mode="HTML",
                **kwargs
            )
        if not_found:
            await context.bot.send_message(text="не нашла: " + ", ".join(not_found), **kwargs)
        return True

    if text_l.startswith("/dm ") or text_l.startswith("лс "):
        rest = text_raw.split(" ", 1)[1].strip()
        parts = rest.split(" ", 1)
        if len(parts) < 2:
            await context.bot.send_message(text="формат: /dm @user текст", **kwargs)
            return True
        target, dm_text = parts[0].lstrip("@"), parts[1]
        found = find_users(chat_id, target) or find_users_global(target)
        if not found:
            await context.bot.send_message(text="не знаю такого~", **kwargs)
            return True
        uid, info = found[0]
        try:
            await context.bot.send_message(chat_id=int(uid), text=dm_text)
            await context.bot.send_message(text=f"написала в лс {info.get('name', '')}~", **kwargs)
        except Exception as e:
            await context.bot.send_message(text="не смогла написать в лс (человек не запускал бота)", **kwargs)
            print("DM error:", e)
        return True

    if text_l.startswith("/role ") or text_l.startswith("роль "):
        role = text_raw.split(" ", 1)[1].strip()
        settings["role"] = role if role.lower() not in ["сброс", "off", "нет"] else None
        save_all()
        await context.bot.send_message(
            text=f"роль: {settings['role']}~" if settings["role"] else "роль сброшена~",
            **kwargs
        )
        return True

    if text_l in ["/summary", "саммари"]:
        history = chat_histories.get(str(chat_id), [])
        if not history:
            await context.bot.send_message(text="пока не о чем вспоминать~", **kwargs)
            return True
        summary = await call_groq(
            [
                {"role": "system", "content": "Кратко саммаризируй диалог в 2-3 предложениях, мило."},
                {"role": "user", "content": "\n".join(
                    f"{'Я' if m['role']=='assistant' else 'Он'}: {m['content']}" for m in history[-12:]
                )},
            ],
            model=MODEL_FAST, temperature=0.5, max_tokens=180,
        )
        await context.bot.send_message(text=summary or "не вспомнила >_<", **kwargs)
        return True

    if text_l in ["/clear", "очистить", "забудь"]:
        chat_histories[str(chat_id)] = []
        user_facts[str(chat_id)] = []
        save_all()
        await context.bot.send_message(text="всё забыла~", **kwargs)
        return True

    return False

async def extract_user_text(message, chat_id: str, user_name: str):
    user_text = None
    is_voice_input = False

    if message.text:
        user_text = message.text

    elif message.voice:
        is_voice_input = True
        print(f"🎤 {user_name}")
        try:
            f = await message.voice.get_file()
            path = f"/tmp/{chat_id}_v.ogg"
            await f.download_to_drive(path)
            user_text = await transcribe_media(path)
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print("voice error:", e)

    elif message.video_note:
        is_voice_input = True
        print(f"⭕ {user_name} кружок")
        try:
            f = await message.video_note.get_file()
            path = f"/tmp/{chat_id}_note.mp4"
            await f.download_to_drive(path)
            user_text = await transcribe_media(path)
            if os.path.exists(path):
                os.remove(path)
            if not user_text:
                user_text = "ты скинул кружок, но я не разобрала что там >_<"
            else:
                print(f"📝 кружок: {user_text}")
        except Exception as e:
            print("video_note error:", e)

    elif message.video:
        is_voice_input = True
        print(f"🎬 {user_name} видео")
        try:
            f = await message.video.get_file()
            path = f"/tmp/{chat_id}_video.mp4"
            await f.download_to_drive(path)
            user_text = await transcribe_media(path)
            if os.path.exists(path):
                os.remove(path)
            if not user_text:
                user_text = "ты скинул видео, но звук не разобрала >_<"
            else:
                print(f"📝 видео: {user_text}")
        except Exception as e:
            print("video error:", e)

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
            print("photo error:", e)

    return user_text, is_voice_input

async def process_message(context, chat_id, user_name, user_text, message_id,
                          connection_id=None, is_voice_input=False, is_group=False):
    if settings.get("muted", False):
        return

    print(f"📩 [{'группа' if is_group else 'личка'}] [{user_name}]: {str(user_text)[:80]}")

    await maybe_react(context, chat_id, message_id, connection_id)
    await extract_facts(str(chat_id), user_text)
    mood = await detect_mood(user_text)

    chat_id = str(chat_id)
    chat_histories[chat_id].append({"role": "user", "content": user_text})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]

    messages = [{"role": "system", "content": build_system_prompt(chat_id, mood)}] + chat_histories[chat_id]
    answer = await call_groq(messages, model=MODEL_MAIN, temperature=0.92, max_tokens=400) or "эээ... затупила >_<"

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
            path = f"/tmp/{chat_id}_a.mp3"
            await text_to_voice(answer, path)
            with open(path, "rb") as vf:
                await context.bot.send_voice(voice=vf, reply_to_message_id=message_id, **send_kwargs)
            if os.path.exists(path):
                os.remove(path)
        else:
            parts = split_messages(answer)
            for i, part in enumerate(parts):
                await context.bot.send_chat_action(action=ChatAction.TYPING, **send_kwargs)
                await asyncio.sleep(0.4 + len(part) * 0.01)
                await context.bot.send_message(
                    text=part,
                    reply_to_message_id=message_id if i == 0 else None,
                    **send_kwargs,
                )
                if i < len(parts) - 1:
                    await asyncio.sleep(0.55)
    except Exception as e:
        print("Ошибка отправки:", e)

async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message:
        return

    connection_id = message.business_connection_id
    chat_id = str(message.chat.id)
    user = message.from_user
    user_name = user.first_name if user else "человек"

    if chat_id in BLACKLIST:
        return
    if user:
        remember_user(chat_id, user)

    if message.text and await handle_commands(message.text, chat_id, context, user, connection_id):
        return

    user_text, is_voice_input = await extract_user_text(message, chat_id, user_name)
    if not user_text:
        return

    await process_message(context, chat_id, user_name, user_text, message.message_id,
                          connection_id, is_voice_input, is_group=False)

async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or message.chat.type != "private":
        return

    chat_id = str(message.chat.id)
    user = message.from_user
    user_name = user.first_name if user else "человек"

    if chat_id in BLACKLIST:
        return
    if user:
        remember_private_user(user)

    if message.text and await handle_commands(message.text, chat_id, context, user):
        return

    if message.text and message.text.startswith("/start"):
        await context.bot.send_message(chat_id=int(chat_id), text="привееет~ я мику! пиши мне когда захочешь уву")
        return

    user_text, is_voice_input = await extract_user_text(message, chat_id, user_name)
    if not user_text:
        return

    if message.text:
        user_text = clean_mention(user_text) or user_text

    await process_message(context, chat_id, user_name, user_text, message.message_id,
                          None, is_voice_input, is_group=False)

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or message.chat.type not in ["group", "supergroup"]:
        return

    chat_id = str(message.chat.id)
    user = message.from_user
    user_name = user.first_name if user else "человек"

    if chat_id in BLACKLIST:
        return
    if user:
        remember_user(chat_id, user)

    bot_me = await context.bot.get_me()
    bot_id = bot_me.id
    bot_username = bot_me.username

    is_reply_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_id
    )
    text_for_mention = message.text or message.caption or ""
    mentioned = is_mentioned(text_for_mention, bot_username)

    if message.text and is_owner(user):
        if await handle_commands(message.text, chat_id, context, user):
            return

    if not mentioned and not is_reply_to_bot:
        return

    if message.text and await handle_commands(message.text, chat_id, context, user):
        return

    user_text, is_voice_input = await extract_user_text(message, chat_id, user_name)
    if not user_text:
        return

    if message.text and mentioned:
        user_text = clean_mention(user_text) or "привет"

    await process_message(context, chat_id, user_name, user_text, message.message_id,
                          None, is_voice_input, is_group=True)

async def proactive_loop(app: Application):
    await asyncio.sleep(60)
    while True:
        try:
            if settings.get("muted", False):
                await asyncio.sleep(300)
                continue

            now = datetime.utcnow()
            for uid, info in list(private_users.items()):
                last = info.get("last_proactive")
                if last:
                    try:
                        last_dt = datetime.fromisoformat(last)
                        if now - last_dt < timedelta(seconds=PROACTIVE_MIN):
                            continue
                    except Exception:
                        pass

                if random.random() > PROACTIVE_CHANCE:
                    continue

                phrase = random.choice(PROACTIVE_PHRASES)
                try:
                    await app.bot.send_message(chat_id=int(uid), text=phrase)
                    private_users[uid]["last_proactive"] = now.isoformat()
                    save_all()
                    print(f"💌 Сама написала → {info.get('name', uid)}")
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"Не смогла написать {uid}:", e)

        except Exception as e:
            print("proactive error:", e)

        await asyncio.sleep(random.randint(PROACTIVE_MIN, PROACTIVE_MAX))

def main():
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))

    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE)
        & filters.ChatType.PRIVATE,
        on_private_message
    ))

    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE)
        & filters.ChatType.GROUPS,
        on_group_message
    ))

    async def post_init(application: Application):
        asyncio.create_task(proactive_loop(application))

    app.post_init = post_init

    print(f"Мику запущена | Админ: @{OWNER_USERNAME}")
    print(f"Модели: {MODEL_MAIN} / {MODEL_FAST}")
    print(f"Мут: {'ДА' if settings.get('muted') else 'НЕТ'}")
    app.run_polling(
        allowed_updates=["business_connection", "business_message", "message"],
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
