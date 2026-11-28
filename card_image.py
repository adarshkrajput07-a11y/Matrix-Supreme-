import io
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)
    except Exception:
        try:
            return ImageFont.load_default(size)
        except TypeError:
            return ImageFont.load_default()


def _fit(d, text, fnt, max_w):
    text = text or ""
    if d.textlength(text, font=fnt) <= max_w:
        return text
    while text and d.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def make_card_image(art_bytes, title, artist, duration_text):
    """Now-playing card: blurred background, rounded cover, title, artist, bar."""
    W, H = 1280, 720
    art = None
    if art_bytes:
        try:
            art = Image.open(io.BytesIO(art_bytes)).convert("RGB")
        except Exception:
            art = None
    if art is None:
        art = Image.new("RGB", (640, 360), (45, 45, 75))
    if art.size == (480, 360):            # strip YouTube's black bars
        art = art.crop((0, 45, 480, 315))

    bg = art.resize((W, H)).filter(ImageFilter.GaussianBlur(45)).convert("RGBA")
    bg = Image.alpha_composite(bg, Image.new("RGBA", (W, H), (70, 20, 140, 150)))

    side = min(art.size)
    left, top = (art.width - side) // 2, (art.height - side) // 2
    cover = art.crop((left, top, left + side, top + side)).resize((460, 460))
    mask = Image.new("L", (460, 460), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 459, 459), 44, fill=255)
    bg.paste(cover, (80, 130), mask)
    glow = ImageDraw.Draw(bg)
    glow.rounded_rectangle((78, 128, 542, 592), 46, outline=(196, 150, 255), width=5)

    d = ImageDraw.Draw(bg)
    x, right = 600, W - 70
    f_title, f_artist, f_time = _font(52, True), _font(36), _font(30)
    d.text((x, 215), _fit(d, title, f_title, right - x), font=f_title,
           fill=(255, 255, 255))
    d.text((x, 295), _fit(d, artist, f_artist, right - x), font=f_artist,
           fill=(225, 205, 255))

    y = 430
    d.rounded_rectangle((x, y, right, y + 10), 5, fill=(150, 110, 210))
    d.ellipse((x - 4, y - 9, x + 24, y + 19), fill=(214, 176, 255))
    d.text((x, y + 32), "00:00", font=f_time, fill=(235, 235, 240))
    d.text((right - d.textlength(duration_text, font=f_time), y + 32),
           duration_text, font=f_time, fill=(235, 235, 240))

    out = io.BytesIO()
    bg.convert("RGB").save(out, "JPEG", quality=90)
    out.seek(0)
    out.name = "card.jpg"
    return out
