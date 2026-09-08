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


def tg_edit_message(chat_id, message_id, text=None, reply_markup=None):
    """Редактирует существующее сообщение (текст и/или клавиатуру)."""
    method = "editMessageText" if text else "editMessageReplyMarkup"
    payload = {"chat_id": chat_id, "message_id": message_id}
    if text:
        payload["text"] = text
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = tg_request(method, payload)
    if res and not res.get("ok"):
        # "message is not modified" — содержимое не изменилось, это нормально
        if "not modified" in (res.get("description") or ""):
            return {"ok": True}
    return res


def show_panel(chat_id, user_id, state, view="root"):
    """Показывает/обновляет панель настроек для раздела view. Возвращает id сообщения."""
    is_group = chat_id < 0
    text, kb = _panel_view(state, view, is_group)
    msg_id = state.get("panel_msg_id")
    if msg_id:
        res = tg_edit_message(chat_id, msg_id, text=text, reply_markup=kb)
        if res and res.get("ok"):
            state["panel_view"] = view
            return msg_id
    res = tg_send_message(chat_id, text, reply_markup=kb)
    if res and res.get("ok") and res["result"].get("message_id"):
        new_id = res["result"]["message_id"]
        state["panel_msg_id"] = new_id
        state["panel_view"] = view
        set_store(chat_id, state, user_id)
        return new_id
    return None


# Опции настройки изображения
FILTERS = {
    "original": "Оригинал",
    "sepia": "Сепия",
    "bw": "Ч/Б",
    "vintage": "Винтаж",
    "neon": "Неон",
}
FONTS = {
    "mem": "Мем",
    "official": "Официальный",
    "modern": "Современный",
}
POSITIONS = {
    "top": "Сверху",
    "center": "Центр",
    "bottom": "Снизу",
    "meme": "Мем-стиль",
}


def _option_row(options: dict, prefix: str, current: str) -> list:
    """Ряд кнопок выбора, текущая помечена галочкой. callback: set:<раздел>:<ключ>"""
    return [
        {
            "text": f"{label} ✓" if key == current else label,
            "callback_data": f"set:{prefix}:{key}",
        }
        for key, label in options.items()
    ]


def _nav_row() -> list:
    """Кнопки «Хелп» и «Назад» для разделов."""
    return [
        {"text": "Хелп", "callback_data": "view:help"},
        {"text": "Назад", "callback_data": "view:root"},
    ]


def _text_line(state: dict) -> str:
    """Строка текущего текста."""
    if state.get("text_none"):
        return "Без текста"
    title = state.get("title", "")
    subtitle = state.get("subtitle", "")
    if not title:
        return "Текст не задан"
    return f"{title} | {subtitle}" if subtitle else title


def _settings_line(state: dict) -> str:
    """Одна строка: фильтр | шрифт | позиция."""
    return (
        f"{FILTERS.get(state.get('filter', 'original'))} | "
        f"{FONTS.get(state.get('font', 'mem'))} | "
        f"{POSITIONS.get(state.get('position', 'center'))}"
    )


def _help_text(is_group: bool) -> str:
    """Справка про бота для режима чата (группа или ЛС)."""
    base = (
        "Что умеет бот:\n"
        "Накладывает текст на фото с выбором оформления.\n\n"
        "Как пользоваться:\n"
        "1. Отправь фото\n"
        "2. Введи текст (можно «Заголовок | Подзаголовок»)\n"
        "   или нажми «Без текста» — чтобы сделать фото без надписи\n"
        "3. В меню настройки открой разделы:\n"
        "   - Фильтры: Оригинал, Сепия, Ч/Б, Винтаж, Неон\n"
        "   - Шрифты: Мем, Официальный, Современный\n"
        "   - Позиция: Сверху, Центр, Снизу, Мем-стиль\n"
        "   - Текст: изменить или убрать текст\n"
        "4. Нажми «Готово» — получишь фото\n\n"
    )
    if is_group:
        return (
            base
            + f"В групповом чате бот работает по триггеру:\n"
            f"напиши «кот» или упомяни @{BOT_USERNAME} — и дальше\n"
            f"отправляй фото. Без триггера бот молчит в группе.\n\n"
            f"Команды: /help, /reset"
        )
    return base + "Команды: /help, /reset"


def _panel_view(state: dict, view: str, is_group: bool) -> tuple[str, dict]:
    """Возвращает (текст, клавиатура) для экрана панели по имени view."""
    if view == "help":
        return (
            _help_text(is_group),
            {"inline_keyboard": [[{"text": "Понятно", "callback_data": "view:root"}]]},
        )

    if view == "root":
        text = (
            "Настройки.\n\n"
            f"Текст: {_text_line(state)}\n"
            f"{_settings_line(state)}\n\n"
            "Выбери раздел для настройки или жми «Готово»:"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "Фильтры", "callback_data": "view:filter"},
                    {"text": "Шрифты", "callback_data": "view:font"},
                ],
                [
                    {"text": "Текст", "callback_data": "view:text"},
                    {"text": "Позиция", "callback_data": "view:position"},
                ],
                [
                    {"text": "Хелп", "callback_data": "view:help"},
                    {"text": "Готово", "callback_data": "render"},
                ],
            ]
        }
        return text, kb

    if view in ("filter", "font", "position"):
        options = {"filter": FILTERS, "font": FONTS, "position": POSITIONS}[view]
        labels = {
            "filter": "Фильтр",
            "font": "Шрифт",
            "position": "Позиция текста",
        }
        cur = state.get(view, {"filter": "original", "font": "mem", "position": "center"}[view])
        text = (
            f"Раздел: {labels[view]}\n\n"
            f"Текущий: {options.get(cur, cur)}\n"
            "Нажми свой вариант (галочка = выбран):"
        )
        kb = {
            "inline_keyboard": [
                _option_row(options, view, cur),
                _nav_row(),
            ]
        }
        return text, kb

    # view == "text"
    text = (
        "Раздел: Текст\n\n"
        f"Текущий: {_text_line(state)}\n"
        "Можно изменить или убрать текст:"
    )
    toggle_label = "Убрать текст" if not state.get("text_none") else "Показать текст"
    kb = {
        "inline_keyboard": [
            [{"text": "Изменить текст", "callback_data": "txt:edit"}],
            [{"text": toggle_label, "callback_data": "txt:none"}],
            _nav_row(),
        ]
    }
    return text, kb


def _settings_summary(state: dict) -> str:
    """Краткая сводка для подписи результата."""
    return _settings_line(state)


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
        is_group = chat_id < 0

        # Команды /start и /help доступны всегда (в том числе в группе)
        if "text" in msg and msg["text"].strip() == "/start":
            tg_send_message(
                chat_id,
                "Привет! Я накладываю текст на фото.\n"
                "Отправь фото — дальше будет просто.\n\n"
                "Подробнее: /help",
            )
            return "OK"

        if "text" in msg and msg["text"].strip() == "/help":
            tg_send_message(chat_id, _help_text(is_group))
            return "OK"

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
                tg_send_message(chat_id, "Пришли фото, я сделаю красивое оформление.")
                return "OK"

            # Сохраняем признак активной сессии в хранилище при каждом обновлении
            state = state or {}
            state.setdefault("active", True)
        else:
            state = get_store(chat_id, user_id)
            state.setdefault("active", True)

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
            current["panel_msg_id"] = None
            current["text_mode"] = "initial"
            current["text_none"] = False
            set_store(chat_id, current, user_id)
            tg_send_message(
                chat_id,
                "Фото получено!\n\n"
                "Теперь введи текст (например `Название | Описание`)\n"
                "или нажми «Без текста», если текст не нужен:",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Без текста", "callback_data": "txt:none"}],
                        [{"text": "Хелп", "callback_data": "view:help"}],
                    ]
                },
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
            state["text_none"] = False
            mode = state.get("text_mode", "initial")
            state["text_mode"] = None
            set_store(chat_id, state, user_id)

            # Если текст редактировался — возвращаемся в раздел Текст, иначе в корень
            show_panel(chat_id, user_id, state, view="text" if mode == "edit" else "root")
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

        if data == "render":
            try:
                result = process_image(
                    base64.b64decode(state["photo"]),
                    state.get("title", "") if not state.get("text_none") else "",
                    state.get("subtitle", "") if not state.get("text_none") else "",
                    filter_name=state.get("filter", "original"),
                    font_style=state.get("font", "mem"),
                    position=state.get("position", "center"),
                )
                result.seek(0)
                tg_send_photo(
                    chat_id,
                    result.read(),
                    caption=f"Готово! ({_settings_summary(state)})\n\nОтправь фото снова, чтобы сделать ещё одно.",
                )
                clear_store(chat_id, user_id)
            except Exception as e:
                logging.exception("Ошибка обработки фото")
                tg_send_message(chat_id, f"Ошибка обработки: {e}")
            return "OK"

        # Навигация между разделами
        if data.startswith("view:"):
            view = data.split(":", 1)[1]
            show_panel(chat_id, user_id, state, view=view)
            return "OK"

        # Редактировать текст
        if data == "txt:edit":
            state["text_mode"] = "edit"
            set_store(chat_id, state, user_id)
            tg_send_message(
                chat_id,
                "Введи новый текст для фото.\n"
                "С подзаголовком можно через |:\n"
                "`Название | Описание`",
            )
            return "OK"

        # Включить/выключить текст
        if data == "txt:none":
            state["text_none"] = not state.get("text_none", False)
            if state["text_none"]:
                state["title"] = ""
                state["subtitle"] = ""
                state["text_mode"] = None
            set_store(chat_id, state, user_id)
            if state.get("panel_msg_id"):
                show_panel(chat_id, user_id, state, view="text")
            else:
                show_panel(chat_id, user_id, state, view="root")
            return "OK"

        # Выбор значения внутри раздела (set:filter:X и т.п.)
        for prefix, options in (("filter", FILTERS), ("font", FONTS), ("position", POSITIONS)):
            if data.startswith(f"set:{prefix}:"):
                value = data.split(":", 2)[2]
                if value in options:
                    state[prefix] = value
                    set_store(chat_id, state, user_id)
                    show_panel(chat_id, user_id, state, view=prefix)
                return "OK"

        return "OK"

    return "OK"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
