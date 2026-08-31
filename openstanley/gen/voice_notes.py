"""Voice notes → text. Local-first ASR via faster-whisper.

The model is lazy: nothing loads until the first voice note arrives, and
tests monkeypatch `transcribe` so the suite never downloads weights.

2026-08-31 accuracy overhaul (owner: "the voice to text is so bad — get
the most accurate transcribe"): tiny/beam-1 could not handle Iraqi Arabic
dialect at all. Now medium (env-tunable), beam 5, and a domain
initial_prompt that biases decoding toward the account's actual
vocabulary — Iraqi Arabic mixed with English crypto/AI terms.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from ..core.config import ROOT

MODEL_DIR = ROOT / "data" / "models"
_lock = threading.Lock()
_model = None

# dialect + domain bias for the decoder: this account speaks Iraqi Arabic
# code-switched with English crypto/AI jargon — exactly what tiny-era
# whisper collapsed into generic MSA mush
INITIAL_PROMPT = (
    "Iraqi Arabic dialect mixed with English. Topics: crypto, bitcoin, "
    "ethereum, stablecoins, DeFi, AI agents, Claude, ChatGPT, API, "
    "github, startups, X posts, engagement. Words like شنو, هسه, اكو, "
    "ماكو, خوش, صدك, والله, يعني, هاي, هيچ."
)


def model_name() -> str:
    """Which whisper size to load. medium by default — the accuracy jump
    over tiny for Arabic dialects is massive; still fine on CPU (int8).
    Set OPENSTANLEY_WHISPER_MODEL=small|large-v3|... to trade."""
    return os.environ.get("OPENSTANLEY_WHISPER_MODEL", "medium")


def _get_model():
    """Load once per process. int8 CPU: no GPU dependency."""
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(model_name(), device="cpu",
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
            path, language=lang, beam_size=5, vad_filter=True,
            initial_prompt=INITIAL_PROMPT)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception:  # noqa: BLE001 — a failed transcription is a UX event
        return ""
    finally:
        Path(path).unlink(missing_ok=True)
