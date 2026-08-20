"""Typographic hook-cards — offline media for text posts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")

from openstanley.gen import quote_card as qc   # noqa: E402


def test_card_renders_and_saves(tmp_path):
    name = qc.make_card("ship fast, talk to users, repeat", out_dir=tmp_path)
    assert name and name.startswith("media_card_") and name.endswith(".png")
    f = tmp_path / name
    assert f.exists() and f.stat().st_size > 5000
    from PIL import Image
    with Image.open(f) as img:
        assert img.size == (1200, 675)


def test_arabic_text_skipped(tmp_path):
    assert qc.make_card("النص العربي يتخطى البطاقة", out_dir=tmp_path) is None


def test_urls_and_oversize_skipped(tmp_path):
    assert qc.make_card("see https://example.com/x", out_dir=tmp_path) is None
    assert qc.make_card("x " * 200, out_dir=tmp_path) is None


def test_deterministic_name_for_same_text(tmp_path):
    a = qc.make_card("same words make the same card", out_dir=tmp_path)
    b = qc.make_card("same words make the same card", out_dir=tmp_path)
    assert a == b
