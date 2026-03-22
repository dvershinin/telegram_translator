#!/usr/bin/env python3
"""Generate The Stack podcast artwork using DALL-E 3 + PIL text overlay."""

import argparse
import base64
import os
import sys
from io import BytesIO
from pathlib import Path

import openai
from PIL import Image, ImageDraw, ImageFont

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS = PROJECT_ROOT / "podcasts" / "assets"

# Output paths
RAW_OUTPUT = ASSETS / "the_stack_artwork_raw.png"
OUTPUT_3000_PNG = ASSETS / "the_stack_artwork_3000.png"
OUTPUT_3000_JPG = ASSETS / "the_stack_artwork.jpg"
OUTPUT_1024_PNG = ASSETS / "the_stack_artwork.png"
FULLPAGE_PNG = ASSETS / "the_stack_fullpage.png"
FULLPAGE_JPG = ASSETS / "the_stack_fullpage.jpg"

# Colors
BG_COLOR = (10, 15, 10)
TITLE_COLOR = (230, 255, 240)
SUBTITLE_COLOR = (180, 220, 200)

# Fonts (macOS system fonts)
MENLO = "/System/Library/Fonts/Menlo.ttc"

# Fullpage canvas (Apple Podcasts Full Page Show Art)
CANVAS_W, CANVAS_H = 2048, 2732

DALLE_PROMPT = (
    "A dark background illustration for a technology podcast cover. Abstract circuit "
    "board traces and chip layout patterns rendered in glowing green and cyan neon lines "
    "on a very dark charcoal-black background. The traces flow organically across the "
    "image forming a subtle circular composition in the center. Include elements "
    "suggesting technology: PCB traces, solder pads, IC chip outlines, and subtle "
    "binary digits faintly visible in the background. The color palette is strictly "
    "dark background with green, cyan, and teal glowing traces. No text whatsoever. "
    "No letters, no words, no numbers. Minimalist, technical, clean. The bottom 30% "
    "of the image should be darker and emptier to leave room for a text overlay."
)


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
    char_widths = []
    for ch in text:
        bbox = font.getbbox(ch)
        char_widths.append(bbox[2] - bbox[0])
    total_width = sum(char_widths) + spacing * (len(text) - 1)

    x = center_x - total_width // 2
    for i, ch in enumerate(text):
        draw.text((x, y), ch, font=font, fill=fill)
        x += char_widths[i] + spacing


def generate_base_image() -> Image.Image:
    """Generate base artwork via DALL-E 3 API.

    Returns:
        PIL Image at 1024x1024.
    """
    client = openai.OpenAI()
    print("Generating image via DALL-E 3 (this may take 15-30 seconds)...")
    response = client.images.generate(
        model="dall-e-3",
        prompt=DALLE_PROMPT,
        size="1024x1024",
        quality="hd",
        style="vivid",
        response_format="b64_json",
        n=1,
    )
    image_data = base64.b64decode(response.data[0].b64_json)
    img = Image.open(BytesIO(image_data)).convert("RGB")

    # Log revised prompt (DALL-E 3 rewrites prompts internally)
    revised = response.data[0].revised_prompt
    if revised:
        print(f"Revised prompt: {revised}")

    # Save raw output
    img.save(RAW_OUTPUT, "PNG")
    print(f"Saved raw: {RAW_OUTPUT}")
    return img


def create_square_artwork(base: Image.Image) -> Image.Image:
    """Upscale to 3000x3000 and overlay text.

    Args:
        base: 1024x1024 base image from DALL-E.

    Returns:
        3000x3000 PIL Image with text overlay.
    """
    # Upscale
    artwork = base.resize((3000, 3000), Image.LANCZOS)
    draw = ImageDraw.Draw(artwork)

    # "THE STACK" — Menlo Bold
    font_title = ImageFont.truetype(MENLO, size=220, index=1)  # Bold
    draw_spaced_text(
        draw, "THE STACK", font_title, 1500, y=2150, spacing=25, fill=TITLE_COLOR
    )

    # "DAILY TECH" — Menlo Regular
    font_sub = ImageFont.truetype(MENLO, size=80, index=0)  # Regular
    draw_spaced_text(
        draw, "DAILY TECH", font_sub, 1500, y=2420, spacing=15, fill=SUBTITLE_COLOR
    )

    return artwork


def create_fullpage_artwork(source_3000: Image.Image) -> Image.Image:
    """Create Apple Podcasts Full Page Show Art (2048x2732) from 3000x3000 source.

    Args:
        source_3000: 3000x3000 artwork (text region will be cropped out).

    Returns:
        2048x2732 PIL Image.
    """
    # Crop top portion (above text overlay region)
    globe = source_3000.crop((0, 0, 3000, 2150))  # 3000x2150

    # Scale to canvas width
    scale = CANVAS_W / globe.width  # 2048/3000 ≈ 0.683
    new_h = int(globe.height * scale)  # ~1469
    globe_resized = globe.resize((CANVAS_W, new_h), Image.LANCZOS)

    # Create canvas
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)
    canvas.paste(globe_resized, (0, 0))

    # Gradient fade: blend bottom of art into background over 400px
    fade_height = 400
    fade_start = new_h - fade_height
    mask = Image.new("L", (1, fade_height))
    for y_off in range(fade_height):
        mask.putpixel((0, y_off), int(255 * (y_off / fade_height)))
    mask = mask.resize((CANVAS_W, fade_height), Image.BILINEAR)

    bg_strip = Image.new("RGB", (CANVAS_W, fade_height), BG_COLOR)
    art_strip = canvas.crop((0, fade_start, CANVAS_W, fade_start + fade_height))
    blended = Image.composite(bg_strip, art_strip, mask)
    canvas.paste(blended, (0, fade_start))

    # Draw text at fullpage safe area
    draw = ImageDraw.Draw(canvas)
    safe_center_x = CANVAS_W // 2  # 1024

    # "THE STACK" — scaled for fullpage
    font_title = ImageFont.truetype(MENLO, size=110, index=1)  # Bold
    draw_spaced_text(
        draw, "THE STACK", font_title, safe_center_x, y=780, spacing=18, fill=TITLE_COLOR
    )

    # "DAILY TECH"
    font_sub = ImageFont.truetype(MENLO, size=45, index=0)  # Regular
    draw_spaced_text(
        draw, "DAILY TECH", font_sub, safe_center_x, y=910, spacing=12, fill=SUBTITLE_COLOR
    )

    return canvas


def main() -> None:
    """Generate all artwork variants for The Stack podcast."""
    parser = argparse.ArgumentParser(description="Generate The Stack artwork")
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="Skip DALL-E generation, use existing raw image",
    )
    args = parser.parse_args()

    # Step 1: Get base image
    if args.skip_generate:
        if not RAW_OUTPUT.exists():
            print(f"Error: {RAW_OUTPUT} not found. Run without --skip-generate first.")
            sys.exit(1)
        print(f"Loading existing raw image: {RAW_OUTPUT}")
        base = Image.open(RAW_OUTPUT).convert("RGB")
    else:
        base = generate_base_image()

    # Step 2: Create 3000x3000 square artwork with text
    artwork_3000 = create_square_artwork(base)
    artwork_3000.save(OUTPUT_3000_PNG, "PNG")
    artwork_3000.save(OUTPUT_3000_JPG, "JPEG", quality=95)
    print(f"Saved: {OUTPUT_3000_PNG}")
    print(f"Saved: {OUTPUT_3000_JPG}")

    # Step 3: Create 1024x1024 variant with text
    artwork_1024 = create_square_artwork(base)
    # Re-draw at 1024 scale (text is baked in at 3000, so downscale the 3000 version)
    artwork_1024 = artwork_3000.resize((1024, 1024), Image.LANCZOS)
    artwork_1024.save(OUTPUT_1024_PNG, "PNG")
    print(f"Saved: {OUTPUT_1024_PNG}")

    # Step 4: Create fullpage art (2048x2732)
    fullpage = create_fullpage_artwork(artwork_3000)
    fullpage.save(FULLPAGE_PNG, "PNG")
    fullpage.save(FULLPAGE_JPG, "JPEG", quality=95)
    print(f"Saved: {FULLPAGE_PNG}")
    print(f"Saved: {FULLPAGE_JPG}")

    print(f"\nAll artwork generated. Main file: {OUTPUT_3000_JPG}")
    print(f"Square: {artwork_3000.size}, Fullpage: {fullpage.size}")


if __name__ == "__main__":
    main()
