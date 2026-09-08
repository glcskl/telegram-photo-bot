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

from image_processor import process_image

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


# Опции настройки изображения
FILTERS = {
    "original": "✨ Оригинал",
    "sepia": "🎞 Сепия",
    "bw": "⚫️ Ч/Б",
    "vintage": "📻 Винтаж",
    "neon": "💡 Неон",
}
FONTS = {
    "mem": "🤣 Мем",
    "official": "📜 Официальный",
    "modern": "🅰️ Современный",
}
POSITIONS = {
    "top": "⬆️ Сверху",
    "center": "🎯 Центр",
    "bottom": "⬇️ Снизу",
    "meme": "🤡 Мем-стиль",
}


def _settings_summary(state: dict) -> str:
    """Человекочитаемая сводка текущих настроек."""
    f = state.get("filter", "original")
    fo = state.get("font", "mem")
    pos = state.get("position", "center")
    return (
        f"🔥 Фильтр: {FILTERS.get(f, f)}\n"
        f"🔤 Шрифт: {FONTS.get(fo, fo)}\n"
        f"📐 Позиция: {POSITIONS.get(pos, pos)}"
    )


def _keyboard(options: dict, prefix: str) -> dict:
    """Строит inline-клавиатуру из словаря {ключ: подпись}."""
    buttons = [
        {"text": label, "callback_data": f"{prefix}:{key}"}
        for key, label in options.items()
    ]
    return {"inline_keyboard": [buttons[i : i + 3] for i in range(0, len(buttons), 3)]}


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
            current.setdefault("filter", "original")
            current.setdefault("font", "mem")
            current.setdefault("position", "center")
            set_store(chat_id, current, user_id)
            tg_send_message(
                chat_id,
                "📸 Фото получено!\n\n"
                "Теперь напиши заголовок.\n"
                "С подзаголовком можно через |:\n"
                "`Скидка 50% | Только сегодня`",
            )
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
                f"Заголовок: **{title}**\n" + (f"Подзаголовок: {subtitle}\n" if subtitle else "") +
                "\nТеперь выбери фильтр:",
                reply_markup=_keyboard(FILTERS, "filter"),
            )
            return "OK"

    # Обработка нажатия кнопки
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        cb_from = cq.get("from", {})
        user_id = cb_from.get("id")
        data = cq["data"]

        state = get_store(chat_id, user_id)
        if not state or "photo" not in state:
            tg_send_message(chat_id, "Начни заново: сначала пришли фото.")
            return "OK"

        try:
            prefix, value = data.split(":", 1)
        except ValueError:
            return "OK"

        # Шаг: выбор фильтра
        if prefix == "filter" and value in FILTERS:
            state["filter"] = value
            set_store(chat_id, state, user_id)
            tg_send_message(
                chat_id,
                f"Фильтр: {FILTERS[value]}\n\nТеперь выбери шрифт:",
                reply_markup=_keyboard(FONTS, "font"),
            )
            return "OK"

        # Шаг: выбор шрифта
        if prefix == "font" and value in FONTS:
            state["font"] = value
            set_store(chat_id, state, user_id)
            tg_send_message(
                chat_id,
                f"Шрифт: {FONTS[value]}\n\nТеперь выбери расположение текста:",
                reply_markup=_keyboard(POSITIONS, "position"),
            )
            return "OK"

        # Шаг: выбор позиции -> финальная сводка
        if prefix == "position" and value in POSITIONS:
            state["position"] = value
            set_store(chat_id, state, user_id)
            tg_send_message(
                chat_id,
                f"{_settings_summary(state)}\n\n"
                f"Заголовок: **{state.get('title', '')}**\n"
                f"{'Подзаголовок: ' + state.get('subtitle', '') if state.get('subtitle') else ''}\n\n"
                "Готово? Сделать фото:",
                reply_markup={"inline_keyboard": [[{"text": "✅ Сделать фото", "callback_data": "render"}] ]},
            )
            return "OK"

        # Финальный рендер
        if data == "render":
            try:
                result = process_image(
                    base64.b64decode(state["photo"]),
                    state.get("title", ""),
                    state.get("subtitle", ""),
                    filter_name=state.get("filter", "original"),
                    font_style=state.get("font", "mem"),
                    position=state.get("position", "center"),
                )
                result.seek(0)
                tg_send_photo(
                    chat_id,
                    result.read(),
                    caption="Готово! 🎉 Отправь фото снова, чтобы сделать ещё одно.",
                )
                clear_store(chat_id, user_id)
            except Exception as e:
                logging.exception("Ошибка обработки фото")
                tg_send_message(chat_id, f"Ошибка обработки: {e}")
            return "OK"

        return "OK"

    return "OK"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
