from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io
import textwrap


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """Пытаемся найти шрифт, иначе дефолтный."""
    font_paths = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str = "white",
    shadow_color: str = "black",
    shadow_offset: int = 3,
):
    x, y = xy
    # Тень
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=shadow_color)
    draw.text((x, y), text, font=font, fill=fill)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Перенос текста по словам под ширину."""
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = font.getbbox(test_line)
        if bbox[2] - bbox[0] <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
    if current_line:
        lines.append(" ".join(current_line))
    return lines or [""]


def process_dark_overlay(
    photo_bytes: bytes, title: str, subtitle: str = ""
) -> io.BytesIO:
    """Затемнение по центру + белый текст с тенью."""
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    w, h = img.size

    # Затемнённая полоса по центру
    overlay_h = int(h * 0.45)
    overlay = Image.new("RGB", (w, overlay_h), (0, 0, 0))
    overlay = overlay.convert("RGB")

    # Полупрозрачность через blend
    top = max(0, (h - overlay_h) // 2)
    cropped = img.crop((0, top, w, top + overlay_h))
    blended = Image.blend(cropped, overlay, alpha=0.6)
    img.paste(blended, (0, top))

    draw = ImageDraw.Draw(img)
    title_font = _get_font(max(28, w // 15))
    sub_font = _get_font(max(18, w // 28))

    # Заголовок
    title_lines = _wrap_text(title, title_font, int(w * 0.85))
    line_h = title_font.getbbox("Ay")[3] - title_font.getbbox("Ay")[1] + 8
    total_h = line_h * len(title_lines)

    y_start = (h - total_h) // 2
    for i, line in enumerate(title_lines):
        bbox = title_font.getbbox(line)
        tw = bbox[2] - bbox[0]
        x = (w - tw) // 2
        _draw_text_with_shadow(draw, (x, y_start + i * line_h), line, title_font)

    # Подзаголовок
    if subtitle:
        sub_lines = _wrap_text(subtitle, sub_font, int(w * 0.85))
        sub_h = sub_font.getbbox("Ay")[3] - sub_font.getbbox("Ay")[1] + 6
        y_sub = y_start + total_h + 12
        for i, line in enumerate(sub_lines):
            bbox = sub_font.getbbox(line)
            tw = bbox[2] - bbox[0]
            x = (w - tw) // 2
            _draw_text_with_shadow(
                draw, (x, y_sub + i * sub_h), line, sub_font, fill="#cccccc"
            )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


def process_blur_overlay(
    photo_bytes: bytes, title: str, subtitle: str = ""
) -> io.BytesIO:
    """Блюр фона + чёткий текст по центру."""
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    w, h = img.size

    # Блюр всего изображения как фон
    bg = img.filter(ImageFilter.GaussianBlur(radius=18))

    draw = ImageDraw.Draw(bg)
    title_font = _get_font(max(32, w // 13))
    sub_font = _get_font(max(20, w // 25))

    # Заголовок
    title_lines = _wrap_text(title, title_font, int(w * 0.85))
    line_h = title_font.getbbox("Ay")[3] - title_font.getbbox("Ay")[1] + 10
    total_h = line_h * len(title_lines)

    y_start = (h - total_h) // 2
    for i, line in enumerate(title_lines):
        bbox = title_font.getbbox(line)
        tw = bbox[2] - bbox[0]
        x = (w - tw) // 2
        _draw_text_with_shadow(draw, (x, y_start + i * line_h), line, title_font)

    if subtitle:
        sub_lines = _wrap_text(subtitle, sub_font, int(w * 0.85))
        sub_h = sub_font.getbbox("Ay")[3] - sub_font.getbbox("Ay")[1] + 6
        y_sub = y_start + total_h + 14
        for i, line in enumerate(sub_lines):
            bbox = sub_font.getbbox(line)
            tw = bbox[2] - bbox[0]
            x = (w - tw) // 2
            _draw_text_with_shadow(
                draw, (x, y_sub + i * sub_h), line, sub_font, fill="#dddddd"
            )

    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


def process_banner(
    photo_bytes: bytes, title: str, subtitle: str = ""
) -> io.BytesIO:
    """Фото как фон + цветной блок с текстом снизу."""
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    w, h = img.size

    draw = ImageDraw.Draw(img)

    title_font = _get_font(max(28, w // 16))
    sub_font = _get_font(max(18, w // 28))

    # Вычисляем высоту блока
    title_lines = _wrap_text(title, title_font, int(w * 0.85))
    line_h = title_font.getbbox("Ay")[3] - title_font.getbbox("Ay")[1] + 8
    title_h = line_h * len(title_lines)

    sub_lines = _wrap_text(subtitle, sub_font, int(w * 0.85)) if subtitle else []
    sub_h = sub_font.getbbox("Ay")[3] - sub_font.getbbox("Ay")[1] + 6 if subtitle else 0
    sub_block_h = sub_h * len(sub_lines) if sub_lines else 0

    padding = 30
    block_h = title_h + sub_block_h + padding * 2 + (12 if subtitle else 0)
    block_top = h - block_h

    # Полупрозрачный блок
    overlay = Image.new("RGBA", (w, int(block_h)), (0, 0, 0, 200))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(overlay, (0, block_top), overlay)
    img = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Заголовок
    y = block_top + padding
    for i, line in enumerate(title_lines):
        bbox = title_font.getbbox(line)
        tw = bbox[2] - bbox[0]
        x = (w - tw) // 2
        _draw_text_with_shadow(draw, (x, y + i * line_h), line, title_font)

    # Подзаголовок
    y_sub = y + title_h + 12
    for i, line in enumerate(sub_lines):
        bbox = sub_font.getbbox(line)
        tw = bbox[2] - bbox[0]
        x = (w - tw) // 2
        draw.text((x, y_sub + i * sub_h), line, font=sub_font, fill="#cccccc")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf
