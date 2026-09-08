from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageOps
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


# ============================================
# СТИЛИ ТЕКСТА
# ============================================
def _text_color(img: Image.Image, title_lines: list, sub_lines: list, font) -> str:
    """Авто-цвет: контрастная пара к среднему цвету фото."""
    thumb = img.copy()
    thumb.thumbnail((64, 64))
    px = list(thumb.getdata())
    luma = sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in px) / max(1, len(px))
    return "black" if luma > 150 else "white"


def _draw_title_line(
    img: Image.Image,
    x: int,
    y: int,
    line: str,
    font,
    fill: str,
    style: str,
    line_h: int,
):
    """Рисует одну строку текста в заданном стиле."""
    if style == "shadow":
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.text((x + 5, y + 7), line, font=font, fill=(0, 0, 0, 200))
        layer = layer.filter(ImageFilter.BoxBlur(6))
        img.paste(layer, (0, 0), layer)
        ImageDraw.Draw(img).text((x, y), line, font=font, fill=fill)
    elif style == "plate":
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        bbox = font.getbbox(line)
        tw = bbox[2] - bbox[0]
        pad_x, pad_y = 16, 8
        d.rounded_rectangle(
            [x - pad_x, y - pad_y, x + tw + pad_x, y + line_h + pad_y],
            radius=12,
            fill=(0, 0, 0, 170),
        )
        d.text((x, y), line, font=font, fill=fill)
        img.paste(layer, (0, 0), layer)
    elif style == "outline":
        draw = ImageDraw.Draw(img)
        draw.text(
            (x, y), line, font=font, fill=fill,
            stroke_width=5, stroke_fill="black",
        )
    else:  # plain / auto
        ImageDraw.Draw(img).text((x, y), line, font=font, fill=fill)


def _draw_title_block(
    img: Image.Image,
    lines: list[str],
    font,
    top_left_y: int,
    style: str = "outline",
    line_gap: int = 8,
    color: str = None,
):
    w, _ = img.size
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    y = top_left_y
    for line in lines:
        bbox = font.getbbox(line)
        tw = bbox[2] - bbox[0]
        x = (w - tw) // 2
        _draw_title_line(
            img, x, y, line, font,
            fill=color or "white", style=style, line_h=line_h,
        )
        y += line_h + line_gap


# ============================================
# РАМКИ
# ============================================
def _linear_gradient_mask(w: int, h: int, invert: bool) -> Image.Image:
    """Вертикальный градиент 0..255 (внизу сильнее если invert=True)."""
    grad = Image.linear_gradient("L").resize((w, h))
    if invert:
        grad = ImageOps.invert(grad)
    return grad


def _add_gradient(img: Image.Image, position: str) -> Image.Image:
    """Градиентная плашка: тёмная к краю по позиции текста."""
    w, h = img.size
    strip = int(h * 0.35)
    black = Image.new("RGB", (w, strip), (0, 0, 0))

    def apply(y0: int):
        region = img.crop((0, y0, w, y0 + strip))
        # маска: внизу strip чёрный (тёмная область к краю кадра)
        mask = _linear_gradient_mask(w, strip, invert=True)
        if y0 == 0:
            mask = ImageOps.invert(mask)  # сверху к краю
        merged = Image.composite(black, region, mask)
        img.paste(merged, (0, y0))

    if position == "top":
        apply(0)
    elif position == "meme":
        apply(0)
        apply(h - strip)
    else:
        apply(h - strip)
    return img


def _add_vignette(img: Image.Image) -> Image.Image:
    """Затемняет углы (виньетка)."""
    w, h = img.size
    sw, sh = max(64, w // 20), max(64, h // 20)
    mask = Image.new("L", (sw, sh), 0)
    px = mask.load()
    cx, cy = (sw - 1) / 2, (sh - 1) / 2
    for y in range(sh):
        for x in range(sw):
            d = ((x - cx) / cx) ** 2 + ((y - cy) / cy) ** 2
            d = min(1.0, d)
            a = int(255 * min(1.0, max(0.0, (d - 0.55) / 0.45)))
            px[x, y] = a
    mask = mask.resize((w, h), Image.BILINEAR)
    black = Image.new("RGB", (w, h), (0, 0, 0))
    img.paste(Image.composite(black, img, mask), (0, 0))
    return img


def _make_polaroid(img: Image.Image, title: str, subtitle: str, font_style: str) -> io.BytesIO:
    """Белая рамка-поляроид с подписью внизу."""
    w, h = img.size
    pad = max(36, w // 14)
    cap_h = 100
    canvas = Image.new("RGB", (w + pad * 2, h + pad * 2 + cap_h), "white")
    canvas.paste(img, (pad, pad))

    caption = title.strip()
    if subtitle.strip():
        caption = f"{title.strip()} | {subtitle.strip()}"
    if caption:
        font = _load_font(font_style, max(24, w // 20))
        lines = _wrap_text(caption, font, int(canvas.width * 0.92))
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        draw = ImageDraw.Draw(canvas)
        y = h + pad * 2 + (cap_h - line_h * len(lines)) // 2
        for line in lines:
            bbox = font.getbbox(line)
            tw = bbox[2] - bbox[0]
            draw.text(((canvas.width - tw) // 2, y), line, font=font, fill=(80, 80, 80))
            y += line_h + 4

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


def _make_meme_frame(
    img: Image.Image,
    title: str,
    subtitle: str,
    font_style: str,
    text_style: str,
) -> io.BytesIO:
    """Классические мемы: белые бары сверху и снизу, текст на них."""
    w, h = img.size
    bar = max(int(h * 0.16), 60)
    canvas = Image.new("RGB", (w, h + bar * 2), "white")
    canvas.paste(img, (0, bar))

    font = _load_font("mem", max(40, w // 8))
    ascent, descent = font.getmetrics()
    line_h = ascent + descent

    title_lines = _wrap_text(title, font, int(w * 0.9))
    sub_lines = _wrap_text(subtitle, font, int(w * 0.9)) if subtitle else []
    fill = "black"
    if text_style == "auto":
        fill = _text_color(img, [], [], font)

    # Верхний бар
    y = (bar - line_h * len(title_lines)) // 2
    for line in title_lines:
        bbox = font.getbbox(line)
        tw = bbox[2] - bbox[0]
        _draw_title_line(
            canvas, (w - tw) // 2, y, line, font,
            fill=fill, style=text_style, line_h=line_h,
        )
        y += line_h + 6

    # Нижний бар: подзаголовок или заголовок
    bottom = sub_lines or title_lines
    y = h + bar + (bar - line_h * len(bottom)) // 2
    for line in bottom:
        bbox = font.getbbox(line)
        tw = bbox[2] - bbox[0]
        _draw_title_line(
            canvas, (w - tw) // 2, y, line, font,
            fill=fill, style=text_style, line_h=line_h,
        )
        y += line_h + 6

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


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
    frame: str = "none",
    text_style: str = "outline",
) -> io.BytesIO:
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")

    # 1. Фильтр
    img = _apply_filter(img, filter_name)

    # 2. Рамки, которые сами рисуют текст
    if frame == "polaroid":
        return _make_polaroid(img, title, subtitle, font_style)
    if frame == "meme_frame":
        return _make_meme_frame(img, title, subtitle, font_style, text_style)

    # Накладные рамки (до текста)
    if frame == "gradient":
        img = _add_gradient(img, position)
    if frame == "vignette":
        img = _add_vignette(img)

    w, h = img.size

    # 3. Шрифты
    title_font = _load_font(font_style, max(32, w // 13))
    sub_font = _load_font(font_style, max(20, w // 26))

    title_lines = _wrap_text(title, title_font, int(w * 0.85))
    sub_lines = _wrap_text(subtitle, sub_font, int(w * 0.85)) if subtitle else []

    title_h = _text_block_height(title_lines, title_font, 8)
    sub_h = _text_block_height(sub_lines, sub_font, 6) if sub_lines else 0

    # Авто-цвет
    color = None
    if text_style == "auto":
        color = _text_color(img, title_lines, sub_lines, title_font)

    # 4. Позиция текста
    pos = (position or "center").lower()
    style = ("outline" if text_style == "auto" else text_style)

    if pos == "top":
        panel_h = min(int(h * 0.4), title_h + sub_h + 80)
        _darken_band(img, 0, panel_h, 0.55)
        y = panel_h - title_h - sub_h - 30
        _draw_title_block(img, title_lines, title_font, y, style=style, color=color)
        if sub_lines:
            _draw_title_block(
                img, sub_lines, sub_font,
                y + title_h + 16, style=style, line_gap=6, color=color,
            )

    elif pos == "bottom":
        panel_h = min(int(h * 0.4), title_h + sub_h + 80)
        _darken_band(img, h - panel_h, h, 0.55)
        y = h - panel_h + 30
        _draw_title_block(img, title_lines, title_font, y, style=style, color=color)
        if sub_lines:
            _draw_title_block(
                img, sub_lines, sub_font,
                y + title_h + 16, style=style, line_gap=6, color=color,
            )

    elif pos == "meme":
        # Классика мемов: текст сверху и снизу, без панелей, с обводкой
        _draw_title_block(img, title_lines, title_font, 20, style=style, color=color)
        y_bottom = h - sub_h - 20 if sub_lines else h - title_h - 20
        block = sub_lines or title_lines
        _draw_title_block(
            img, block, sub_font if sub_lines else title_font,
            y_bottom, style=style, line_gap=6, color=color,
        )

    else:  # center (default)
        panel_h = min(int(h * 0.5), title_h + sub_h + 100)
        top = max(0, (h - panel_h) // 2)
        _darken_band(img, top, top + panel_h, 0.6)
        y = (h - title_h - sub_h) // 2
        _draw_title_block(img, title_lines, title_font, y, style=style, color=color)
        if sub_lines:
            _draw_title_block(
                img, sub_lines, sub_font,
                y + title_h + 16, style=style, line_gap=6, color=color,
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