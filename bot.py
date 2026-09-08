import os
import asyncio
import logging
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from image_processor import (
    process_dark_overlay,
    process_blur_overlay,
    process_banner,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

# Состояния: храним временные данные пользователей
user_data: dict[int, dict] = {}


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для красивого оформления фото.\n\n"
        "1. Отправь мне фото\n"
        "2. Напиши заголовок\n"
        "3. Выбери стиль оформления\n\n"
        "Команды:\n"
        "/help — помощь"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Как пользоваться:\n\n"
        "1. Отправь фото\n"
        "2. Напиши заголовок (можно с подзаголовком через |)\n"
        "   Пример: Скидка 50% | Только до конца недели\n"
        "3. Выбери стиль:\n"
        "   - Затемнение — тёмная полоса + текст\n"
        "   - Блюр — размытый фон + текст\n"
        "   - Баннер — полоса снизу + текст"
    )


@router.message(F.photo)
async def handle_photo(message: Message):
    photo = message.photo[-1]
    file = await Bot.get_file(photo.file_id)
    photo_bytes: BytesIO = await Bot.download_file(file.file_path)

    buf = BytesIO()
    async for chunk in photo_bytes:
        buf.write(chunk)

    user_data[message.from_user.id] = {"photo": buf.getvalue()}
    await message.answer("Фото получено! Теперь напиши заголовок.\n\nМожно добавить подзаголовок через |:\n`Скидка 50% | Только сегодня`")


@router.message(F.text)
async def handle_text(message: Message):
    uid = message.from_user.id
    if uid not in user_data or "photo" not in user_data[uid]:
        await message.answer("Сначала отправь мне фото.")
        return

    text = message.text.strip()
    if "|" in text:
        parts = text.split("|", 1)
        title = parts[0].strip()
        subtitle = parts[1].strip()
    else:
        title = text
        subtitle = ""

    user_data[uid]["title"] = title
    user_data[uid]["subtitle"] = subtitle

    kb = InlineKeyboardBuilder()
    kb.button(text="Затемнение", callback_data="mode:dark")
    kb.button(text="Блюр", callback_data="mode:blur")
    kb.button(text="Баннер", callback_data="mode:banner")
    kb.adjust(3)

    await message.answer(
        f"Заголовок: **{title}**" + (f"\nПодзаголовок: {subtitle}" if subtitle else ""),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("mode:"))
async def handle_mode(callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_data or "photo" not in user_data[uid]:
        await callback.answer("Что-то пошло не так. Начни заново: отправь фото.")
        return

    mode = callback.data.split(":")[1]
    data = user_data[uid]
    photo_bytes = data["photo"]
    title = data.get("title", "")
    subtitle = data.get("subtitle", "")

    processors = {
        "dark": process_dark_overlay,
        "blur": process_blur_overlay,
        "banner": process_banner,
    }

    processor = processors.get(mode)
    if not processor:
        await callback.answer("Неизвестный режим")
        return

    await callback.answer("Обрабатываю...")

    try:
        result = processor(photo_bytes, title, subtitle)
    except Exception as e:
        logger.exception("Ошибка обработки")
        await callback.message.answer(f"Ошибка при обработке: {e}")
        return

    result.seek(0)
    input_file = BufferedInputFile(result.read(), filename="processed.jpg")
    await callback.message.answer_photo(
        photo=input_file,
        caption=f"Готово! Стиль: {mode}",
    )

    # Чистим
    user_data.pop(uid, None)


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
