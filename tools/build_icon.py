"""Generate deterministic Windows app icons.

Two kinds:

- The two gates and the combined entry share one shape (rounded tile, document, gate, badge)
  and differ in only three places: tile color, arrow, badge. Easy to tell apart on the desktop:
  blue = issue, green = intake, blue-and-green double arrow = both gates in one.
- The desktop entry's `app.ico` is a GW letter mark and shares no shape with the three above.
  A desktop icon really only has about 32px to work with, and more than two elements blur
  into a blob -- so it is just the tile and two letters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


SIZE = 1024
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class Variant:
    stem: str
    tile_top: RGB
    tile_bottom: RGB
    badge_fill: RGBA
    badge_outline: RGBA
    kind: str  # outbound / inbound / version


OUTBOUND = Variant(
    stem="outbound_gate",
    tile_top=(20, 104, 214),
    tile_bottom=(11, 35, 91),
    badge_fill=(35, 196, 112, 255),
    badge_outline=(181, 255, 219, 255),
    kind="outbound",
)
INBOUND = Variant(
    stem="inbound_gate",
    tile_top=(22, 150, 112),
    tile_bottom=(7, 54, 58),
    badge_fill=(245, 170, 40, 255),
    badge_outline=(255, 227, 168, 255),
    kind="inbound",
)
GATE = Variant(
    stem="gate",
    tile_top=(30, 132, 190),
    tile_bottom=(22, 48, 96),
    badge_fill=(22, 163, 74, 255),
    badge_outline=(187, 247, 208, 255),
    kind="gate",
)


def rounded_mask(box: tuple[int, int, int, int], radius: int) -> Image.Image:
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def vertical_gradient(top: RGB, bottom: RGB) -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE))
    pixels = image.load()
    for y in range(SIZE):
        ratio = y / (SIZE - 1)
        color = tuple(round(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        for x in range(SIZE):
            pixels[x, y] = (*color, 255)
    return image


def build(variant: Variant = OUTBOUND) -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # Soft shadow under the rounded app tile.
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((62, 82, 962, 982), radius=210, fill=(0, 15, 50, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    canvas.alpha_composite(shadow)

    tile_box = (54, 48, 970, 964)
    tile_mask = rounded_mask(tile_box, 210)
    tile = vertical_gradient(variant.tile_top, variant.tile_bottom)
    tile.putalpha(tile_mask)
    canvas.alpha_composite(tile)

    # A restrained top-left highlight makes it feel like a real app tile.
    highlight = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.ellipse((-170, -250, 750, 610), fill=(98, 209, 255, 54))
    highlight.putalpha(Image.composite(highlight.getchannel("A"), Image.new("L", (SIZE, SIZE), 0), tile_mask))
    canvas.alpha_composite(highlight)

    # Document shadow and body.
    doc_shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    dsd = ImageDraw.Draw(doc_shadow)
    dsd.rounded_rectangle((186, 172, 643, 807), radius=62, fill=(0, 17, 55, 115))
    doc_shadow = doc_shadow.filter(ImageFilter.GaussianBlur(22))
    canvas.alpha_composite(doc_shadow)

    draw = ImageDraw.Draw(canvas)
    document = [(166, 145), (514, 145), (664, 295), (664, 780), (166, 780)]
    draw.polygon(document, fill=(249, 252, 255, 255))
    draw.rounded_rectangle((166, 145, 664, 780), radius=58, fill=(249, 252, 255, 255))
    # Restore the crisp folded corner after the rounded body.
    draw.polygon([(514, 145), (664, 295), (514, 295)], fill=(180, 220, 247, 255))
    draw.line([(514, 145), (514, 295), (664, 295)], fill=(113, 179, 227, 255), width=22)

    # Document content lines remain visible at 32 px without becoming noise.
    for y, width in ((380, 322), (468, 270), (556, 306), (644, 218)):
        draw.rounded_rectangle((238, y, 238 + width, y + 28), radius=14, fill=(115, 170, 218, 255))

    # Gate: solid high-contrast portal in front of the document.
    gate_shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gsd = ImageDraw.Draw(gate_shadow)
    gsd.rounded_rectangle((560, 275, 879, 387), radius=40, fill=(0, 12, 38, 125))
    gsd.rounded_rectangle((580, 314, 674, 834), radius=34, fill=(0, 12, 38, 125))
    gsd.rounded_rectangle((786, 314, 880, 834), radius=34, fill=(0, 12, 38, 125))
    gate_shadow = gate_shadow.filter(ImageFilter.GaussianBlur(18))
    canvas.alpha_composite(gate_shadow)

    draw = ImageDraw.Draw(canvas)
    cyan = (48, 214, 235, 255)
    cyan_light = (128, 244, 255, 255)
    draw.rounded_rectangle((542, 254, 862, 366), radius=38, fill=cyan)
    draw.rounded_rectangle((562, 330, 655, 829), radius=30, fill=cyan)
    draw.rounded_rectangle((769, 330, 862, 829), radius=30, fill=cyan)
    draw.rounded_rectangle((570, 270, 834, 302), radius=16, fill=cyan_light)

    # Arrow: outbound leaves through the gate, inbound comes back in,
    # version swaps two ways at once (new version in, old version out to the archive).
    arrow_fill = {
        "outbound": (36, 117, 205, 255),
        "inbound": (28, 148, 118, 255),
        "version": (150, 96, 224, 255),
        "gate": (56, 160, 224, 255),
    }[variant.kind]
    if variant.kind == "outbound":
        draw.rounded_rectangle((382, 680, 690, 744), radius=32, fill=arrow_fill)
        draw.polygon([(690, 625), (810, 712), (690, 799)], fill=arrow_fill)
    elif variant.kind == "inbound":
        draw.rounded_rectangle((502, 680, 810, 744), radius=32, fill=arrow_fill)
        draw.polygon([(502, 625), (382, 712), (502, 799)], fill=arrow_fill)
    else:
        # Both gates in one: top row right = issue, bottom row left = intake, two directions in one picture.
        draw.rounded_rectangle((392, 636, 672, 684), radius=24, fill=arrow_fill)
        draw.polygon([(668, 596), (772, 660), (668, 724)], fill=arrow_fill)
        draw.rounded_rectangle((492, 748, 772, 796), radius=24, fill=(28, 148, 118, 255))
        draw.polygon([(496, 708), (392, 772), (496, 836)], fill=(28, 148, 118, 255))

    # Badge: green check for outbound, amber "filed" arrow for inbound.
    badge_shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bsd = ImageDraw.Draw(badge_shadow)
    bsd.ellipse((670, 650, 946, 926), fill=(0, 18, 34, 130))
    badge_shadow = badge_shadow.filter(ImageFilter.GaussianBlur(20))
    canvas.alpha_composite(badge_shadow)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse(
        (652, 630, 928, 906),
        fill=variant.badge_fill,
        outline=variant.badge_outline,
        width=18,
    )
    if variant.kind == "gate":
        # "1" = one entry point, one ledger
        draw.line([(752, 706), (800, 682)], fill=(255, 255, 255, 255), width=30, joint="curve")
        draw.line([(800, 682), (800, 846)], fill=(255, 255, 255, 255), width=34)
        draw.line([(742, 848), (858, 848)], fill=(255, 255, 255, 255), width=30)
    elif variant.kind == "version":
        # History arrow = old versions can be recovered. The arc's gap is at the top right, the arrowhead fills it.
        draw.arc((706, 684, 874, 852), start=25, end=310, fill=(255, 255, 255, 255), width=34)
        draw.polygon([(824, 690), (896, 716), (836, 764)], fill=(255, 255, 255, 255))
    elif variant.kind == "outbound":
        draw.line([(715, 767), (774, 824), (876, 699)], fill=(255, 255, 255, 255), width=38, joint="curve")
    else:
        draw.line([(790, 692), (790, 820)], fill=(255, 255, 255, 255), width=38)
        draw.line(
            [(724, 762), (790, 828), (856, 762)],
            fill=(255, 255, 255, 255),
            width=38,
            joint="curve",
        )

    return canvas


# ---------------------------------------------------------------- desktop entry icon
# The GW letter mark of the "Gateway" desktop entry. Deliberately no gate, document or badge:
# what is really legible on the desktop and taskbar is 32px, and at that size one element is the limit.

APP_TOP: RGB = (37, 62, 108)
APP_BOTTOM: RGB = (22, 38, 72)
APP_FONTS = ("bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf")


def _display_font(size: int) -> ImageFont.FreeTypeFont:
    for name in APP_FONTS:
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    raise RuntimeError("No usable display font found: " + ", ".join(APP_FONTS))


def build_app_icon(text: str = "GW") -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    tile_box = (32, 32, 992, 992)
    tile = vertical_gradient(APP_TOP, APP_BOTTOM)
    tile.putalpha(rounded_mask(tile_box, 224))
    canvas.alpha_composite(tile)

    # Tracking tightened by 8px: Bahnschrift's default letter spacing falls apart at small sizes.
    draw = ImageDraw.Draw(canvas)
    font = _display_font(560)
    widths = [draw.textlength(ch, font=font) for ch in text]
    track = -8
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = 512 - (sum(widths) + track * (len(text) - 1)) / 2
    y = 500 - (bottom + top) / 2
    for ch, width in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=(255, 255, 255, 255))
        x += width + track
    return canvas


def save_app_icon() -> tuple[Path, Path]:
    image = build_app_icon()
    preview = ASSETS / "app_icon.png"
    icon = ASSETS / "app.ico"
    image.save(preview, "PNG", optimize=True)
    image.save(
        icon,
        "ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return preview, icon


def save(variant: Variant) -> tuple[Path, Path]:
    image = build(variant)
    preview = ASSETS / f"{variant.stem}_icon.png"
    icon = ASSETS / f"{variant.stem}.ico"
    image.save(preview, "PNG", optimize=True)
    image.save(
        icon,
        "ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return preview, icon


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for variant in (OUTBOUND, INBOUND, GATE):
        for path in save(variant):
            print(path)
    for path in save_app_icon():
        print(path)


if __name__ == "__main__":
    main()
