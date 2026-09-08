import os
import base64
import json
import logging
import threading
import time
from urllib.request import Request, urlopen

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

from image_processor import (
    process_dark_overlay,
    process_blur_overlay,
    process_banner,
)

load_dotenv()
app = Flask(__name__)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Нет BOT_TOKEN. Задай переменную окружения BOT_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

# URL сервиса для self-ping и авто-регистрации webhook (задаётся на Render)
EXTERNAL_URL = os.getenv("EXTERNAL_URL", "").rstrip("/")

# Имя бота для упоминаний в группах (без @)
BOT_USERNAME = os.getenv("BOT_USERNAME", "ph_editot_bot")

# Upstash Redis (REST API — подходит для serverless)
REDIS_URL = os.getenv("REDIS_URL")
REDIS_TOKEN = os.getenv("REDIS_TOKEN")


# ============================================
# SELF-PING MECHANISM (предотвращает засыпание)
# ============================================
def _tg_request(method, payload):
    resp = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=15)
    return resp.json()


def register_webhook():
    """Автоматически регистрирует webhook при старте на Render."""
    webhook_url = f"{EXTERNAL_URL}/webhook/{TOKEN}"
    try:
        result = _tg_request(
            "setWebhook",
            {
                "url": webhook_url,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            },
        )
        if result.get("ok"):
            logging.info(f"[Webhook] Зарегистрирован: {webhook_url}")
        else:
            logging.error(f"[Webhook] Ошибка: {result.get('description')}")
    except Exception as e:
        logging.error(f"[Webhook] Ошибка: {e}")


def self_ping_worker():
    """Фоновый поток: каждые 10 минут пингует себя, чтобы Render не засыпал."""
    time.sleep(10)
    if EXTERNAL_URL:
        register_webhook()

    time.sleep(30)
    while True:
        try:
            if EXTERNAL_URL:
                resp = requests.get(f"{EXTERNAL_URL}/health", timeout=30)
                logging.info(
                    f"[Keep-Alive] Self-ping: {resp.status_code}"
                    if resp.status_code == 200
                    else f"[Keep-Alive] Self-ping статус: {resp.status_code}"
                )
        except Exception as e:
            logging.error(f"[Keep-Alive] Ошибка self-ping: {e}")
        time.sleep(600)


if EXTERNAL_URL:
    ping_thread = threading.Thread(target=self_ping_worker, daemon=True)
    ping_thread.start()
    logging.info(f"[Keep-Alive] Запущен self-ping для {EXTERNAL_URL}")


def redis_setex(key, value, ttl):
    """Сохранить значение с TTL (сек) в Upstash Redis."""
    # SETEX key seconds value: команда в URL, значение как тело POST
    # POST url/setex/key/ttl   body=value
    endpoint = f"{REDIS_URL}/setex/{key}/{ttl}"
    req = Request(
        endpoint,
        method="POST",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
        data=value.encode("utf-8"),
    )
    try:
        urlopen(req, timeout=10)
    except Exception as e:
        logging.error(f"Redis setex error: {e}")


def redis_get(key):
    """Получить значение из Upstash Redis. GET url/key"""
    req = Request(
        f"{REDIS_URL}/get/{key}",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
    )
    try:
        resp = urlopen(req, timeout=10)
        body = json.loads(resp.read().decode())
        return body.get("result")
    except Exception as e:
        logging.error(f"Redis get error: {e}")
        return None


def redis_del(key):
    """Удалить ключ из Upstash Redis. POST url/del/key"""
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
    redis_setex(f"user:{chat_id}", json.dumps(data, ensure_ascii=False), ttl=3600)


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
def root():
    return "OK", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "telegram-photo-bot",
            "self_ping_enabled": bool(EXTERNAL_URL),
        }
    ), 200


@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json()
    chat_id = None

    # Команда /start
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]

        # В групповых чатах бот отвечает только на триггер «кот» или @упоминание
        chat_type = msg.get("chat", {}).get("type", "private")
        if chat_type in ("group", "supergroup"):
            entity_mentions = [
                ent.get("text", "")
                for ent in msg.get("entities", [])
                if ent.get("type") == "mention"
            ]
            lowercase_text = (
                f"{msg.get('text', '')} {msg.get('caption', '')}".lower()
            )
            bot_mention = f"@{BOT_USERNAME}".lower()
            triggered = (
                "кот" in lowercase_text
                or bot_mention in lowercase_text
                or any(bot_mention in m.lower() for m in entity_mentions)
            )
            if not triggered:
                return "OK"

        # Команда /start — приветствие, только если не в группе (в группе нужен триггер)
        if "text" in msg and msg["text"] == "/start":
            tg_send_message(chat_id, "Привет! Отправь фото, я красиво оформлю заголовок.")
            return "OK"

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
