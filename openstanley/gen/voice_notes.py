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
    """Which whisper size to load. large-v3-turbo by default — near
    large-v3 accuracy at small-model speed (owner twice: 'the voice to
    text is so bad' → tiny was hopeless for Iraqi Arabic; medium helped;
    turbo is the ceiling before pure large-v3). Env-tunable."""
    return os.environ.get("OPENSTANLEY_WHISPER_MODEL", "large-v3-turbo")


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


REPAIR_SYSTEM = (
    "You repair RAW whisper transcripts of IRAQI ARABIC dialect "
    "code-switched with English (crypto/AI topics). Fix ONLY transcription "
    "artifacts: whisper normalizes Iraqi speech into stiff Modern Standard "
    "Arabic — restore the dialect words the speaker actually said (شنو، "
    "هسه، اكو، ماكو، خوش، هيج، سوة، دلة...), rejoin broken word splits, fix "
    "punctuation, keep English tech terms as spoken. NEVER translate, NEVER "
    "add or drop content, NEVER answer or continue the message, NEVER "
    "'improve' the ideas. If a word is uncertain keep the original. "
    "Output ONLY the repaired transcript, nothing else.")


def repair_dialect(text: str, cfg) -> str:
    """One LLM pass to undo whisper's MSA bias on Iraqi speech. Failure →
    the raw transcript unchanged (repair is an enhancement, never a
    dependency)."""
    if not text.strip():
        return text
    from .llm import chat as llm_chat
    from .llm import LLMError
    try:
        out = llm_chat(cfg.llm, system=REPAIR_SYSTEM,
                       user=f"RAW TRANSCRIPT:\n{text}\n\nRepaired:",
                       temperature=0.1)
        out = out.strip().strip('"')
        # sanity: the repair must stay near the source length — a repair
        # that doubles or halves the text invented or lost content
        if out and 0.4 <= len(out) / max(1, len(text)) <= 2.5:
            return out
        return text
    except LLMError:
        return text
