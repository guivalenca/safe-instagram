"""Draws the app icon: Instagram's glyph proportions, in dark purple, with a glassy raised look.

    .venv/bin/python tools/make_icon.py   # writes static/icon-{180,192,512}.png
"""

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 1024  # working size
STATIC = Path(__file__).resolve().parent.parent / "static"


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def background():
    """Deep purple, lit softly from the upper left, with a faint magenta warmth bottom-left."""
    n = 256
    img = Image.new("RGB", (n, n))
    px = img.load()
    deep, mid, glow, warm = (24, 12, 48), (58, 28, 104), (112, 70, 186), (96, 34, 112)
    for y in range(n):
        for x in range(n):
            u, v = x / n, y / n
            c = mix(mid, deep, min(1, (u + v) / 1.7))  # diagonal falloff
            g = max(0, 1 - math.hypot(u - 0.22, v - 0.12) / 0.75)  # upper-left light
            c = mix(c, glow, g**2 * 0.55)
            w = max(0, 1 - math.hypot(u - 0.05, v - 1.0) / 0.7)  # bottom-left warmth
            c = mix(c, warm, w**2 * 0.6)
            px[x, y] = tuple(int(k) for k in c)
    return img.resize((S, S), Image.BICUBIC)


def glyph_mask():
    """Frame, lens and flash dot, measured off the real icon (in % of the icon)."""
    G = S * 2
    m = Image.new("L", (G, G), 0)
    d = ImageDraw.Draw(m)
    u = G / 100
    fw = 6.8  # frame stroke, centred on 16.5..83.5 with a corner radius of 18
    d.rounded_rectangle(
        ((16.5 - fw / 2) * u, (16.5 - fw / 2) * u, (83.5 + fw / 2) * u, (83.5 + fw / 2) * u),
        radius=(18 + fw / 2) * u,
        outline=255,
        width=round(fw * u),
    )
    lw, lr = 6.4, 15.4  # lens stroke and radius
    d.ellipse(((50 - lr - lw / 2) * u,) * 2 + ((50 + lr + lw / 2) * u,) * 2, outline=255, width=round(lw * u))
    dr = 4.3
    d.ellipse(((68.8 - dr) * u, (31.0 - dr) * u, (68.8 + dr) * u, (31.0 + dr) * u), fill=255)
    return m.resize((S, S), Image.LANCZOS)


def shifted(mask, dx, dy):
    out = Image.new("L", mask.size, 0)
    out.paste(mask, (dx, dy))
    return out


def solid(color, alpha):
    layer = Image.new("RGBA", (S, S), color + (0,))
    layer.putalpha(alpha)
    return layer


def scale(mask, k):
    return mask.point(lambda p: int(p * k))


def vertical(top, bottom):
    g = Image.new("RGB", (1, 256))
    for y in range(256):
        g.putpixel((0, y), tuple(int(c) for c in mix(top, bottom, y / 255)))
    return g.resize((S, S), Image.BICUBIC)


def build():
    img = background().convert("RGBA")
    m = glyph_mask()

    # Soft sheen across the top of the tile, like the real icon's glass.
    sheen = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sheen).ellipse((-0.2 * S, -0.75 * S, 1.2 * S, 0.42 * S), fill=255)
    sheen = scale(sheen.filter(ImageFilter.GaussianBlur(S * 0.06)), 0.10)
    img = Image.alpha_composite(img, solid((255, 255, 255), sheen))

    # The glyph floats: a soft shadow below it.
    shadow = scale(shifted(m, 0, int(S * 0.018)).filter(ImageFilter.GaussianBlur(S * 0.022)), 0.55)
    img = Image.alpha_composite(img, solid((10, 4, 24), shadow))

    # Glyph body: white, a touch lilac towards the bottom.
    body = vertical((255, 255, 255), (226, 216, 244)).convert("RGBA")
    body.putalpha(m)
    img = Image.alpha_composite(img, body)

    # Bevel: light catches the top-left edges, the bottom-right edges sit in shade.
    e = max(2, int(S * 0.006))
    light = ImageChops.subtract(m, shifted(m, e, e)).filter(ImageFilter.GaussianBlur(S * 0.0025))
    img = Image.alpha_composite(img, solid((255, 255, 255), scale(light, 0.9)))
    dark = ImageChops.subtract(m, shifted(m, -e, -e)).filter(ImageFilter.GaussianBlur(S * 0.004))
    img = Image.alpha_composite(img, solid((150, 128, 190), scale(dark, 0.75)))

    # Glassy highlight on the upper half of the glyph.
    hl = vertical((255, 255, 255), (0, 0, 0)).convert("L").point(lambda p: max(0, p - 140) * 2)
    img = Image.alpha_composite(img, solid((255, 255, 255), scale(ImageChops.multiply(m, hl), 0.35)))
    return img.convert("RGB")


if __name__ == "__main__":
    icon = build()
    for size in (180, 192, 512):
        icon.resize((size, size), Image.LANCZOS).save(STATIC / f"icon-{size}.png")
    print("wrote", ", ".join(f"icon-{s}.png" for s in (180, 192, 512)))
