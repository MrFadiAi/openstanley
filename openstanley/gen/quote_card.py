"""Typographic hook-cards — offline media for text posts.

A dark card with the post's hook set large (Newsreader where available,
fallback serif), a thin purple accent bar, generous margins. Generated
locally with Pillow: no network, no API, no stock-photo licensing — the
media boost without any dependency. Arabic text is SKIPPED (PIL cannot
shape RTL scripts without extra machinery; a broken-looking card is
worse than no card).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..core.config import ROOT

CARD_DIR = ROOT / "data" / "media"
BG = (10, 10, 15)          # app canvas — cards match the brand
INK = (236, 236, 241)
MUTED = (124, 108, 255)    # accent family
ACCENT = (124, 108, 255)
W, H = 1200, 675           # 16:9
FONT_SIZES = (44, 36, 30)  # tried in order until the text fits

_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def _is_renderable(text: str) -> bool:
    """No Arabic (PIL can't shape it), no URLs, not too long."""
    if _ARABIC_RE.search(text):
        return False
    if "http" in text:
        return False
    return 0 < len(text) <= 220


def _font(size: int):
    from PIL import ImageFont
    for candidate in ("C:/Windows/Fonts/georgiaz.ttf",
                      "C:/Windows/Fonts/times.ttf",
                      "C:/Windows/Fonts/arial.ttf"):
        p = Path(candidate)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_card(text: str, out_dir: Path | None = None) -> str | None:
    """Render `text` as a branded card → stored under data/media with the
    standard media_ naming. Returns the stored name, or None when the text
    isn't renderable (Arabic / URL / too long)."""
    text = " ".join((text or "").split()).strip().strip('"“”')
    if not _is_renderable(text):
        return None
    from PIL import Image, ImageDraw

    out_dir = out_dir or CARD_DIR
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # accent bar + dot — quiet brand mark, nothing louder
    d.rectangle([0, 0, 8, H], fill=ACCENT)
    d.ellipse([72, 92, 88, 108], fill=ACCENT)

    margin_l, max_w = 110, W - 190
    lines: list[str] = []
    for size in FONT_SIZES:
        font = _font(size)
        lines = _wrap(d, text, font, max_w)
        if len(lines) <= 6:
            break
    else:
        return None  # doesn't fit even at the smallest size

    line_h = font.size + 14
    total_h = line_h * len(lines)
    y = (H - total_h) // 2 + 20
    for ln in lines:
        d.text((margin_l, y), ln, font=font, fill=INK)
        y += line_h
    d.text((margin_l - 36, H - 84), "@orbexai", font=_font(24), fill=MUTED)

    name = f"media_card_{hashlib.sha1(text.encode()).hexdigest()[:10]}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    img.save(out_dir / name, "PNG")
    return name
