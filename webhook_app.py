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


def state_key(chat_id, user_id):
    """Ключ сессии: изолирует каждого пользователя даже в общем групповом чате."""
    return f"user:{chat_id}:{user_id}"


def get_store(chat_id, user_id=None):
    """Получить данные пользователя (photo, title, subtitle) из Redis."""
    key = state_key(chat_id, user_id) if user_id else f"user:{chat_id}"
    raw = redis_get(key)
    if not raw:
        return {}
    return json.loads(raw)


def set_store(chat_id, data, user_id=None):
    """Сохранить данные пользователя в Redis (TTL 3 часа)."""
    key = state_key(chat_id, user_id) if user_id else f"user:{chat_id}"
    redis_setex(key, json.dumps(data, ensure_ascii=False), ttl=10800)


def clear_store(chat_id, user_id=None):
    key = state_key(chat_id, user_id) if user_id else f"user:{chat_id}"
    redis_del(key)


def tg_request(method, payload):
    try:
        resp = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=20)
        result = resp.json()
        if not result.get("ok"):
            logging.error(
                f"[TG:{method}] ошибка: {result.get('description')} payload={payload}"
            )
        return result
    except Exception as e:
        logging.error(f"[TG:{method}] exception: {e}")
        return None


def tg_send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("sendMessage", payload)


def tg_send_photo(chat_id, photo_bytes, caption=""):
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": ("photo.jpg", photo_bytes, "image/jpeg")},
        )
        result = resp.json()
        if not result.get("ok"):
            logging.error(f"[TG:sendPhoto] ошибка: {result.get('description')}")
        return result
    except Exception as e:
        logging.error(f"[TG:sendPhoto] exception: {e}")
        return None


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


def _is_supported_image(data: bytes) -> bool:
    """Проверка формата изображения по сигнатуре (JPEG/PNG/WebP)."""
    return (
        data.startswith(b"\xff\xd8\xff")  # JPEG
        or data.startswith(b"\x89PNG\r\n\x1a\n")  # PNG
        or data.startswith(b"RIFF") and data[8:12] == b"WEBP"  # WebP
    )


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
        from_user = msg.get("from", {})
        user_id = from_user.get("id")
        chat_id = msg["chat"]["id"]
        text_content = f"{msg.get('text', '')} {msg.get('caption', '')}"
        bot_mention = f"@{BOT_USERNAME}".lower()

        # В групповых чатах бот работает по шаблону:
        #  - ждёт триггер «кот» или @упоминание -> открывает сессию
        #  - пока сессия активна — обрабатывает фото/заголовки
        #  - после результата сессия закрывается, снова ждёт «кот»
        chat_type = msg.get("chat", {}).get("type", "private")

        # Сброс сессии по команде /reset
        if "text" in msg and msg["text"].strip() == "/reset":
            if user_id is not None:
                clear_store(chat_id, user_id)
            tg_send_message(chat_id, "Сессия сброшена. Скажи «кот», чтобы начать заново.")
            return "OK"

        if chat_type in ("group", "supergroup"):
            lowercase_text = text_content.lower()
            triggered = (
                ("кот" in lowercase_text or bot_mention in lowercase_text)
                or any(
                    ent.get("type") == "mention"
                    and bot_mention in msg.get("text", "")[ent.get("offset", 0): ent.get("offset", 0) + ent.get("length", 0)].lower()
                    for ent in msg.get("entities", [])
                )
            )
            state = get_store(chat_id, user_id)
            session_active = bool(state and state.get("active"))

            # Нет триггера и нет активной сессии -> молчим
            if not session_active and not triggered:
                return "OK"

            # Триггер есть, сессия ещё не открыта -> открываем
            if triggered and not session_active:
                set_store(chat_id, {"active": True}, user_id)
                tg_send_message(chat_id, "Мяу! Пришли фото, я сделаю красивое оформление.")
                return "OK"

            # Сохраняем признак активной сессии в хранилище при каждом обновлении
            state = state or {}
            state.setdefault("active", True)
        else:
            state = get_store(chat_id, user_id)
            state.setdefault("active", True)

        # Команда /start — приветствие, только если не в группе (в группе нужен триггер)
        if "text" in msg and msg["text"] == "/start":
            tg_send_message(chat_id, "Привет! Отправь фото, я красиво оформлю заголовок.")
            return "OK"

        # Обработка фото
        if "photo" in msg:
            photo = msg["photo"][-1]
            file_id = photo["file_id"]
            # Скачиваем файл
            f = tg_request("getFile", {"file_id": file_id})
            if not f or not f.get("ok"):
                tg_send_message(chat_id, "Не удалось получить фото. Попробуй ещё раз.")
                return "OK"
            file_path = f["result"]["file_path"]
            file_size = f["result"].get("file_size", 0)
            if file_size > 20 * 1024 * 1024:
                tg_send_message(chat_id, "Фото слишком большое (лимит 20 МБ).")
                return "OK"
            photo_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
            photo_bytes = requests.get(photo_url).content
            if len(photo_bytes) > 20 * 1024 * 1024:
                tg_send_message(chat_id, "Фото слишком большое (лимит 20 МБ).")
                return "OK"
            # Валидация: это должно быть изображение JPEG/PNG/WebP
            if not _is_supported_image(photo_bytes):
                tg_send_message(chat_id, "Это не похоже на изображение (JPEG/PNG/WebP). Попробуй другое фото.")
                return "OK"
            current = get_store(chat_id, user_id) or {}
            current.setdefault("active", True)
            current["photo"] = base64.b64encode(photo_bytes).decode()
            set_store(chat_id, current, user_id)
            tg_send_message(chat_id, "Фото получено! Теперь напиши заголовок.\nМожно с подзаголовком через |")
            return "OK"

        # Обработка текста (заголовок)
        if "text" in msg:
            state = get_store(chat_id, user_id)
            if not state or "photo" not in state:
                tg_send_message(chat_id, "Сначала отправь мне фото.")
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
            set_store(chat_id, state, user_id)

            tg_send_message(
                chat_id,
                f"Заголовок: **{title}**" + (f"\nПодзаголовок: {subtitle}" if subtitle else ""),
                reply_markup=build_mode_keyboard(),
            )
            return "OK"

    # Обработка нажатия кнопки
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        cb_from = cq.get("from", {})
        user_id = cb_from.get("id")
        data = cq["data"]
        mode = data.split(":")[1]

        state = get_store(chat_id, user_id)
        if not state or "photo" not in state:
            tg_send_message(chat_id, "Что-то пошло не так. Начни заново с фото.")
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
            tg_send_photo(chat_id, result.read(), caption=f"Готово! Стиль: {mode}")
            clear_store(chat_id, user_id)
        except Exception as e:
            logging.exception("Ошибка обработки фото")
            tg_send_message(chat_id, f"Ошибка обработки: {e}")

        return "OK"

    return "OK"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
