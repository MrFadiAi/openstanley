"""Voice notes → text. Local-first ASR via faster-whisper.

The model is lazy: nothing loads until the first voice note arrives, and
tests monkeypatch `transcribe` so the suite never downloads weights
(~75MB, cached under data/models/whisper-tiny).
"""
from __future__ import annotations

import threading
from pathlib import Path

from ..core.config import ROOT

MODEL_DIR = ROOT / "data" / "models" / "whisper-tiny"
_lock = threading.Lock()
_model = None


def _get_model():
    """tiny = multilingual (Arabic + English), fast on CPU. Load once."""
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel("tiny", device="cpu",
                                  compute_type="int8",
                                  download_root=str(MODEL_DIR))
        return _model


def transcribe(audio_bytes: bytes, lang: str | None = None) -> str:
    """ogg/opus voice bytes → text. Empty string when nothing intelligible.
    Tests replace this function at the seam — no model, no download."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_bytes)
        path = f.name
    try:
        segments, _info = _get_model().transcribe(
            path, language=lang, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception:  # noqa: BLE001 — a failed transcription is a UX event
        return ""
    finally:
        Path(path).unlink(missing_ok=True)
