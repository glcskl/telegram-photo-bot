import os
import base64
import json
import logging
from urllib.request import Request, urlopen

import requests
from dotenv import load_dotenv
from flask import Flask, request

from image_processor import (
    process_dark_overlay,
    process_blur_overlay,
    process_banner,
)

load_dotenv()
app = Flask(__name__)

TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

# Upstash Redis (REST API — подходит для serverless)
REDIS_URL = os.getenv("REDIS_URL")
REDIS_TOKEN = os.getenv("REDIS_TOKEN")


def redis_set(key, value, ttl=None):
    """Сохранить значение в Upstash Redis."""
    endpoint = f"{REDIS_URL}/set/{key}?value=upstash_placeholder"
    # Upstash REST API: значение передаётся в теле, токен в URL path
    if ttl:
        endpoint += f"&EX={ttl}"
    try:
        # Записываем через POST с JSON телом
        req = Request(
            endpoint,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"value" if False else "value": value}).encode(),
        )
        urlopen(req, timeout=10)
    except Exception as e:
        logging.error(f"Redis set error: {e}")


def redis_get(key):
    """Получить значение из Upstash Redis."""
    req = Request(
        f"{REDIS_URL}/get/{key}",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
    )
    try:
        resp = urlopen(req, timeout=10)
        body = json.loads(resp.read().decode())
        # Формат Upstash: {"result": value} или {"result": null}
        return body.get("result")
    except Exception as e:
        logging.error(f"Redis get error: {e}")
        return None


def redis_del(key):
    """Удалить ключ из Upstash Redis."""
    req = Request(
        f"{REDIS_URL}/del/{key}",
        method="POST",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
    )
    try:
        urlopen(req, timeout=10)
    except Exception as e:
        logging.error(f"Redis del error: {e}")


def get_store(chat_id):
    """Получить данные пользователя (photo, title, subtitle) из Redis."""
    raw = redis_get(f"user:{chat_id}")
    if not raw:
        return {}
    return json.loads(raw)


def set_store(chat_id, data):
    """Сохранить данные пользователя в Redis (TTL 1 час)."""
    redis_set(f"user:{chat_id}", json.dumps(data, ensure_ascii=False), ttl=3600)


def clear_store(chat_id):
    redis_del(f"user:{chat_id}")


def tg_send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)


def tg_send_photo(chat_id, photo_bytes, caption=""):
    requests.post(
        f"{TELEGRAM_API}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption},
        files={"photo": ("photo.jpg", photo_bytes, "image/jpeg")},
    )


def build_mode_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "Затемнение", "callback_data": "mode:dark"},
                {"text": "Блюр", "callback_data": "mode:blur"},
                {"text": "Баннер", "callback_data": "mode:banner"},
            ]
        ]
    }


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json()
    chat_id = None

    # Команда /start
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]

        # Обработка фото
        if "photo" in msg:
            photo = msg["photo"][-1]
            file_id = photo["file_id"]
            # Скачиваем файл
            f = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
            file_path = f["result"]["file_path"]
            photo_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
            photo_bytes = requests.get(photo_url).content
            set_store(chat_id, {"photo": base64.b64encode(photo_bytes).decode()})
            tg_send_message(chat_id, "Фото получено! Теперь напиши заголовок.\nМожно с подзаголовком через |")
            return "OK"

        # Обработка текста (заголовок)
        if "text" in msg:
            uid = chat_id
            state = get_store(uid)
            if not state or "photo" not in state:
                tg_send_message(uid, "Сначала отправь мне фото.")
                return "OK"

            text = msg["text"]
            if "|" in text:
                parts = text.split("|", 1)
                title = parts[0].strip()
                subtitle = parts[1].strip()
            else:
                title = text
                subtitle = ""

            state["title"] = title
            state["subtitle"] = subtitle
            set_store(uid, state)

            tg_send_message(
                uid,
                f"Заголовок: **{title}**" + (f"\nПодзаголовок: {subtitle}" if subtitle else ""),
                reply_markup=build_mode_keyboard(),
            )
            return "OK"

        if "text" in msg and msg["text"] == "/start":
            tg_send_message(chat_id, "Привет! Отправь фото, я красиво оформлю заголовок.")
            return "OK"

    # Обработка нажатия кнопки
    if "callback_query" in update:
        cq = update["callback_query"]
        uid = cq["message"]["chat"]["id"]
        data = cq["data"]
        mode = data.split(":")[1]

        state = get_store(uid)
        if not state or "photo" not in state:
            tg_send_message(uid, "Что-то пошло не так. Начни заново с фото.")
            return "OK"

        photo_bytes = base64.b64decode(state["photo"])
        title = state.get("title", "")
        subtitle = state.get("subtitle", "")

        processors = {
            "dark": process_dark_overlay,
            "blur": process_blur_overlay,
            "banner": process_banner,
        }

        try:
            processor = processors.get(mode)
            result = processor(photo_bytes, title, subtitle)
            result.seek(0)
            tg_send_photo(uid, result.read(), caption=f"Готово! Стиль: {mode}")
            clear_store(uid)
        except Exception as e:
            tg_send_message(uid, f"Ошибка обработки: {e}")

        return "OK"

    return "OK"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
