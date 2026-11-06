"""Build StockSeek icons from the user's selected raster artwork.

Development-only dependency: Pillow. No generation API or visible application.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ASSETS = Path(__file__).resolve().parent / 'assets'
BACKGROUND = '#112B54'
FOREGROUND = '#FAFAF3'


def extract_mark(source: Path) -> Image.Image:
    """Keep the central white artwork; exclude the surrounding presentation margin."""
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
    width, height = image.size
    # The selected source has a central logo and peripheral generator branding.
    region = image.crop((round(width * .18), round(height * .18),
                         round(width * .82), round(height * .82)))
    red = region.getchannel('R')
    bounds = red.point(lambda value: 255 if value > 160 else 0).getbbox()
    if bounds is None:
        raise ValueError('No light logo artwork found in the central source region')
    left, top, right, bottom = bounds
    box = (max(0, left - 3), max(0, top - 3), min(region.width, right + 3), min(region.height, bottom + 3))
    alpha = red.crop(box).point(lambda value: max(0, min(255, round((value - 55) * 255 / 190))))
    mark = Image.new('RGBA', alpha.size, FOREGROUND)
    mark.putalpha(alpha)
    return mark


def make_icon(mark: Image.Image, size: int = 1024) -> Image.Image:
    icon = Image.new('RGBA', (size, size))
    mask = Image.new('L', icon.size)
    draw = ImageDraw.Draw(mask)
    inset = round(size * .047)
    draw.rounded_rectangle((inset, inset, size - inset - 1, size - inset - 1),
                           radius=round(size * .205), fill=255)
    tile = Image.new('RGBA', icon.size, BACKGROUND)
    tile.putalpha(mask)
    icon.alpha_composite(tile)
    artwork = mark.copy()
    artwork.thumbnail((round(size * .64), round(size * .64)), Image.Resampling.LANCZOS)
    icon.alpha_composite(artwork, ((size - artwork.width) // 2, (size - artwork.height) // 2))
    return icon


def generate(source: Path, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    mark = extract_mark(source)
    mark.save(destination / 'stockseek-mark.png')
    icon = make_icon(mark)
    icon.save(destination / 'app.png')
    icon.save(destination / 'app.ico', format='ICO', sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])
    icon.save(destination / 'app.icns', format='ICNS')
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        icon.resize((size, size), Image.Resampling.LANCZOS).save(destination / f'icon-{size}.png')
    print(f'StockSeek icons generated from {source.name}; central artwork {mark.width}×{mark.height}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ASSETS / 'source/stockseek-original.png')
    parser.add_argument('--output-dir', type=Path, default=ASSETS)
    options = parser.parse_args()
    generate(options.source, options.output_dir)
