"""Configuration loader: data/config.toml + environment for secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:  # py<3.11
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "data" / "config.toml"
ENV_PATH = ROOT / ".env"


def _load_dotenv() -> None:
    """Load ROOT/.env into os.environ (existing env wins). Values never logged."""
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass  # unreadable .env → behave as if absent


@dataclass
class LLMConfig:
    provider: str = "openai-compatible"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = "OPENSTANLEY_LLM_API_KEY"
    temperature: float = 0.85
    max_tokens: int = 1200
    transport: str = "openai"  # openai | anthropic

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass
class XConfig:
    mode: str = "dryrun"  # api | cookie | dryrun
    username: str = ""
    # official api v2
    bearer_token_env: str = "OPENSTANLEY_X_BEARER"
    api_key_env: str = "OPENSTANLEY_X_API_KEY"
    api_secret_env: str = "OPENSTANLEY_X_API_SECRET"
    access_token_env: str = "OPENSTANLEY_X_ACCESS_TOKEN"
    access_secret_env: str = "OPENSTANLEY_X_ACCESS_SECRET"
    # cookie mode (twikit)
    cookies_env: str = "OPENSTANLEY_X_COOKIES"
    import_count: int = 400
    scan_count: int = 800   # deep-scan budget (posts+replies)
    # safety caps (cookie mode) — keep the account looking human
    max_posts_per_day: int = 4
    max_replies_per_day: int = 10
    min_delay_s: int = 5
    max_delay_s: int = 20

    @property
    def cookies(self) -> str:
        return os.environ.get(self.cookies_env, "")


@dataclass
class AgentConfig:
    daily_draft_target: int = 4
    post_times: list = field(default_factory=lambda: ["09:00", "13:00", "18:00"])
    niche_accounts: list = field(default_factory=list)
    evergreen_themes: list = field(default_factory=lambda: [
        "lessons from building things", "contrarian takes in my field",
        "tools and workflows I actually use",
    ])
    timezone: str = "Europe/Amsterdam"
    auto_approve_replies: bool = False
    # v0.4.1 smart slots — approve-path scheduling scored from real metrics
    # (engagement-by-hour, spread vs last post, freshness) instead of the
    # static post_times rotation; post_times stays as the heuristic fallback
    smart_slots: bool = True
    autopilot_interval_min: int = 45
    # v0.3.9 mention inbox — reply budget per mentions-loop run, and the
    # standalone every-30-min cron (only while autopilot is disabled)
    mention_drafts_per_run: int = 3
    mentions_cron: bool = True
    study_hour: int = 3
    digest_weekday: int = 6  # Sunday
    # v0.4.2 daily digest — when the agent reports to its owner
    digest_hour: int = 20
    # v0.3.8 engage quality gate — reply-target scoring before any LLM draft
    engage_gate: dict = field(default_factory=lambda: {
        "weights": {"recency": 0.35, "traction": 0.25, "author": 0.15,
                    "crowding": 0.10, "fit": 0.15},
        "threshold": 55,
        "max_targets": 12,
    })
    # v0.4.0 voice lock — persona-consistency gate on every draft (the
    # Settings UI overrides these via db settings voice_lock_*)
    voice_lock_enabled: bool = True
    voice_lock_threshold: int = 75


@dataclass
class HarnessConfig:
    sample_count: int = 5
    real_llm: bool = False
    suites: list = field(default_factory=lambda: [
        "voice", "algorithm", "bilingual", "tools", "safety",
    ])


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    x: XConfig = field(default_factory=XConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)
    server_host: str = "127.0.0.1"
    server_port: int = 7878


def load_config() -> Config:
    _load_dotenv()
    cfg = Config()
    if CONFIG_PATH.exists() and tomllib is not None:
        raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for section, target in (("llm", cfg.llm), ("x", cfg.x), ("agent", cfg.agent),
                                ("harness", cfg.harness)):
            if section in raw and isinstance(raw[section], dict):
                for k, v in raw[section].items():
                    if hasattr(target, k):
                        setattr(target, k, v)
        for k in ("server_host", "server_port"):
            if k in raw:
                setattr(cfg, k, raw[k])
    # env override — keeps tests hermetic regardless of data/config.toml
    mode_env = os.environ.get("OPENSTANLEY_X_MODE")
    if mode_env in ("api", "cookie", "dryrun"):
        cfg.x.mode = mode_env
    return cfg
