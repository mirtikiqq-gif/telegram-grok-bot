import os
import base64
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
MAX_HISTORY = 12

request = HTTPXRequest(
    connect_timeout=30.0,
    read_timeout=30.0,
    write_timeout=30.0,
    pool_timeout=30.0
)

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
    """Анализирует изображение через Groq Vision"""
    try:
        with open(file_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("utf-8")

        # Определяем тип изображения
        if file_path.endswith(".png"):
            mime = "image/png"
        else:
            mime = "image/jpeg"

        prompt = caption if caption else "Опиши подробно, что изображено на этой картинке. Отвечай на русском."

        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{image_base64}"
                                },
                            },
                        ],
                    }
                ],
                "temperature": 0.5,
                "max_tokens": 1024,
            },
            timeout=60,
        )

        data = resp.json()
        if "choices" in data and len(data["choices"]) > 0:
            return data["choices"][0]["message"]["content"]
        else:
            print("Ошибка vision:", data)
            return "Не удалось разобрать изображение."

    except Exception as e:
        print(f"Ошибка анализа фото: {e}")
        return "Ошибка при обработке фото."

async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message:
        return

    connection_id = message.business_connection_id
    chat_id = message.chat.id
    user_name = message.from_user.first_name if message.from_user else "Человек"

    user_text = None

    # === Текст ===
    if message.text:
        user_text = message.text

    # === Голосовое ===
    elif message.voice:
        print(f"🎤 [{user_name}] прислал голосовое...")
        try:
            voice_file = await message.voice.get_file()
            file_path = f"/tmp/{chat_id}_voice.ogg"
            await voice_file.download_to_drive(file_path)
            user_text = await transcribe_voice(file_path)
            print(f"📝 Расшифровано: {user_text}")
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Ошибка голосового: {e}")
            return

    # === Фото ===
    elif message.photo:
        print(f"🖼 [{user_name}] прислал фото...")
        try:
            # Берём самое большое фото
            photo = message.photo[-1]
            photo_file = await photo.get_file()
            file_path = f"/tmp/{chat_id}_photo.jpg"
            await photo_file.download_to_drive(file_path)

            caption = message.caption or ""
            user_text = await analyze_image(file_path, caption)
            print(f"👁 Распознано: {user_text[:100]}...")

            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Ошибка фото: {e}")
            return

    if not user_text:
        return

    print(f"📩 [{user_name} | {chat_id}]: {user_text[:80]}...")

    # История
    chat_histories[chat_id].append({"role": "user", "content": user_text})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]

    messages = [
        {
            "role": "system",
            "content": (
                "Ты полезный ассистент, который отвечает от имени владельца аккаунта. "
                "Отвечай коротко, естественно и на русском языке. "
                "Помни контекст разговора с этим человеком."
            )
        }
    ] + chat_histories[chat_id]

    # Обычный текстовый ответ
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
                "temperature": 0.7,
                "max_tokens": 1024,
            },
            timeout=40,
        )
        data = resp.json()
        if "choices" in data and len(data["choices"]) > 0:
            answer = data["choices"][0]["message"]["content"]
        else:
            answer = f"Ошибка API: {data}"
            print(answer)
    except Exception as e:
        answer = f"Ошибка запроса: {e}"
        print(answer)

    chat_histories[chat_id].append({"role": "assistant", "content": answer})

    # Отправка
    for attempt in range(3):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=answer,
                business_connection_id=connection_id,
                reply_to_message_id=message.message_id,
            )
            print(f"✅ Ответ отправлен → {user_name}")
            break
        except (TimedOut, NetworkError) as e:
            print(f"⚠️ Таймаут (попытка {attempt + 1}/3): {e}")
            if attempt == 2:
                print("❌ Не удалось отправить")
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")
            break

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )

    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))

    print("Бот запущен (текст + голосовые + фото)...")
    app.run_polling(
        allowed_updates=["business_connection", "business_message"],
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
