#!/usr/bin/env python3
"""Generate Apple Podcasts Full Page Show Art (2048x2732) from square artwork."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS = PROJECT_ROOT / "podcasts" / "assets"
SOURCE = ASSETS / "crosswire_artwork_3000.png"
OUTPUT_PNG = ASSETS / "crosswire_fullpage.png"
OUTPUT_JPG = ASSETS / "crosswire_fullpage.jpg"

# Canvas
CANVAS_W, CANVAS_H = 2048, 2732
BG_COLOR = (21, 29, 37)  # #161e26

# Fonts
HELVETICA = "/System/Library/Fonts/Helvetica.ttc"
HELVETICA_NEUE = "/System/Library/Fonts/HelveticaNeue.ttc"

# Art Safe Area: (430, 350) to (1617, 1205)
SAFE_CENTER_X = (430 + 1617) // 2  # 1023


def draw_spaced_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    center_x: int,
    y: int,
    spacing: int = 0,
    fill: tuple = (255, 255, 255),
) -> None:
    """Draw text centered at (center_x, y) with extra letter spacing.

    Args:
        draw: ImageDraw instance.
        text: Text to render.
        font: Loaded font.
        center_x: Horizontal center position.
        y: Vertical position (top of text).
        spacing: Extra pixels between characters.
        fill: Text color RGB tuple.
    """
    # Calculate total width with spacing
    char_widths = []
    for ch in text:
        bbox = font.getbbox(ch)
        char_widths.append(bbox[2] - bbox[0])
    total_width = sum(char_widths) + spacing * (len(text) - 1)

    x = center_x - total_width // 2
    for i, ch in enumerate(text):
        draw.text((x, y), ch, font=font, fill=fill)
        x += char_widths[i] + spacing


def main() -> None:
    """Compose the full page artwork."""
    # Load source
    src = Image.open(SOURCE)
    assert src.size == (3000, 3000), f"Unexpected source size: {src.size}"

    # Create canvas
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)

    # Extract globe region: full width, y=100 to y=2250 (stop before source text)
    globe = src.crop((0, 100, 3000, 2250))  # 3000x2150

    # Scale to canvas width
    scale = CANVAS_W / globe.width  # 2048/3000 ≈ 0.683
    new_h = int(globe.height * scale)  # ~1570
    globe_resized = globe.resize((CANVAS_W, new_h), Image.LANCZOS)

    # Paste at top
    canvas.paste(globe_resized, (0, 0))

    # Gradient fade: blend bottom of globe into background over 400px
    fade_height = 400
    fade_start = new_h - fade_height
    # Build a 1-pixel-wide gradient mask, then stretch to full width
    mask = Image.new("L", (1, fade_height))
    for y_off in range(fade_height):
        mask.putpixel((0, y_off), int(255 * (y_off / fade_height)))
    mask = mask.resize((CANVAS_W, fade_height), Image.BILINEAR)
    # Composite: blend globe strip with solid background using gradient mask
    bg_strip = Image.new("RGB", (CANVAS_W, fade_height), BG_COLOR)
    globe_strip = canvas.crop((0, fade_start, CANVAS_W, fade_start + fade_height))
    blended = Image.composite(bg_strip, globe_strip, mask)
    canvas.paste(blended, (0, fade_start))

    # Draw text
    draw = ImageDraw.Draw(canvas)

    # "CROSSWIRE" — bold, large, centered on globe
    font_title = ImageFont.truetype(HELVETICA, size=110, index=1)  # Bold
    draw_spaced_text(
        draw, "CROSSWIRE", font_title, SAFE_CENTER_X, y=780, spacing=18
    )

    # "DAILY BRIEFING" — light weight, smaller
    font_sub = ImageFont.truetype(HELVETICA_NEUE, size=45, index=7)  # Light
    draw_spaced_text(
        draw, "DAILY BRIEFING", font_sub, SAFE_CENTER_X, y=910, spacing=12
    )

    # Save
    canvas.save(OUTPUT_PNG, "PNG")
    canvas.save(OUTPUT_JPG, "JPEG", quality=95)
    print(f"Saved: {OUTPUT_PNG}")
    print(f"Saved: {OUTPUT_JPG}")
    print(f"Size: {canvas.size}, Mode: {canvas.mode}")


if __name__ == "__main__":
    main()
