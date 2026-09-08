from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
import io


# ============================================
# ШРИФТЫ
# ============================================
FONT_PATHS = {
    "mem": [
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ],
    "official": [
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSerif-Bold.ttf",
    ],
    "modern": [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ],
}


def _load_font(style: str, size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS.get(style, FONT_PATHS["modern"]):
        try:
            font = ImageFont.truetype(path, size)
            # DejaVuSerif может не поддерживать кириллицу по умолчанию — проверим найденный
            return font
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# ============================================
# ФИЛЬТРЫ
# ============================================
def _apply_filter(img: Image.Image, filter_name: str) -> Image.Image:
    f = (filter_name or "original").lower()
    if f == "sepia":
        return _sepia(img)
    if f == "bw":
        return img.convert("L").convert("RGB")
    if f == "vintage":
        sepia = _sepia(img)
        sepia = ImageEnhance.Contrast(sepia).enhance(0.92)
        return sepia
    if f == "neon":
        img = ImageEnhance.Color(img).enhance(1.6)
        img = ImageEnhance.Contrast(img).enhance(1.3)
        return img
    return img.convert("RGB")


def _sepia(img: Image.Image) -> Image.Image:
    r, g, b = img.convert("RGB").split()
    out = Image.merge(
        "RGB",
        (
            r.point(lambda i: min(255, int(i * 0.393) + int(i * 0.769) + int(i * 0.189))),
            g.point(lambda i: min(255, int(i * 0.349) + int(i * 0.686) + int(i * 0.168))),
            b.point(lambda i: min(255, int(i * 0.272) + int(i * 0.534) + int(i * 0.131))),
        ),
    )
    return out


# ============================================
# ТЕКСТ
# ============================================
def _wrap_text(text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], []
    for word in words:
        test = " ".join(current + [word])
        bbox = font.getbbox(test)
        if bbox[2] - bbox[0] <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def _text_block_height(lines: list[str], font, line_gap: int) -> int:
    ascent, descent = font.getmetrics()
    return (ascent + descent) * len(lines) + line_gap * (len(lines) - 1)


def _draw_title_block(
    draw: ImageDraw.ImageDraw,
    img_size: tuple[int, int],
    lines: list[str],
    font,
    top_left_y: int,
    stroke: bool = False,
    line_gap: int = 8,
):
    w, _ = img_size
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    y = top_left_y
    for line in lines:
        bbox = font.getbbox(line)
        tw = bbox[2] - bbox[0]
        x = (w - tw) // 2
        if stroke:
            draw.text(
                (x, y), line, font=font, fill="white",
                stroke_width=4, stroke_fill="black",
            )
        else:
            draw.text((x + 3, y + 3), line, font=font, fill="black")
            draw.text((x, y), line, font=font, fill="white")
        y += line_h + line_gap


# ============================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================
def process_image(
    photo_bytes: bytes,
    title: str,
    subtitle: str = "",
    filter_name: str = "original",
    font_style: str = "mem",
    position: str = "center",
) -> io.BytesIO:
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    w, h = img.size

    # 1. Фильтр
    img = _apply_filter(img, filter_name)

    # 2. Шрифты
    title_font = _load_font(font_style, max(32, w // 13))
    sub_font = _load_font(font_style, max(20, w // 26))

    title_lines = _wrap_text(title, title_font, int(w * 0.85))
    sub_lines = _wrap_text(subtitle, sub_font, int(w * 0.85)) if subtitle else []

    title_h = _text_block_height(title_lines, title_font, 8)
    sub_h = _text_block_height(sub_lines, sub_font, 6) if sub_lines else 0

    # 3. Позиция текста
    pos = (position or "center").lower()
    draw = ImageDraw.Draw(img)
    stroke = font_style == "mem"

    if pos == "top":
        panel_h = min(int(h * 0.4), title_h + sub_h + 80)
        _darken_band(img, 0, panel_h, 0.55)
        draw = ImageDraw.Draw(img)
        y = panel_h - title_h - sub_h - 30
        _draw_title_block(draw, (w, h), title_lines, title_font, y, stroke=stroke)
        if sub_lines:
            _draw_title_block(
                draw, (w, h), sub_lines, sub_font,
                y + title_h + 16, stroke=stroke, line_gap=6,
            )

    elif pos == "bottom":
        panel_h = min(int(h * 0.4), title_h + sub_h + 80)
        _darken_band(img, h - panel_h, h, 0.55)
        draw = ImageDraw.Draw(img)
        y = h - panel_h + 30
        _draw_title_block(draw, (w, h), title_lines, title_font, y, stroke=stroke)
        if sub_lines:
            _draw_title_block(
                draw, (w, h), sub_lines, sub_font,
                y + title_h + 16, stroke=stroke, line_gap=6,
            )

    elif pos == "meme":
        # Классика мемов: текст сверху и снизу, без панелей, с обводкой
        top_block_h = title_h + 40
        _draw_title_block(draw, (w, h), title_lines, title_font, 20, stroke=True)
        y_bottom = h - sub_h - 20
        if sub_lines:
            _draw_title_block(
                draw, (w, h), sub_lines, sub_font, y_bottom, stroke=True, line_gap=6,
            )
        else:
            _draw_title_block(
                draw, (w, h), title_lines, title_font, y_bottom, stroke=True,
                line_gap=8,
            )

    else:  # center (default)
        panel_h = min(int(h * 0.5), title_h + sub_h + 100)
        top = max(0, (h - panel_h) // 2)
        _darken_band(img, top, top + panel_h, 0.6)
        draw = ImageDraw.Draw(img)
        y = (h - title_h - sub_h) // 2
        _draw_title_block(draw, (w, h), title_lines, title_font, y, stroke=stroke)
        if sub_lines:
            _draw_title_block(
                draw, (w, h), sub_lines, sub_font,
                y + title_h + 16, stroke=stroke, line_gap=6,
            )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


def _darken_band(img: Image.Image, y0: int, y1: int, alpha: float):
    """Затемняет горизонтальную полосу [y0:y1] полупрозрачным чёрным."""
    band = Image.new("RGB", (img.width, y1 - y0), (0, 0, 0))
    cropped = img.crop((0, y0, img.width, y1))
    blended = Image.blend(cropped, band, alpha=min(0.85, max(0.0, alpha)))
    img.paste(blended, (0, y0))