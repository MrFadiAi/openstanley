"""OpenStanley's hands — the chat tool registry (function-calling style).

The chat LLM is told about these tools in its system prompt and emits fenced
action blocks:

    ```action
    {"tool": "schedule_draft", "args": {"text": "...", "when": "9pm"}}
    ```

The backend parses the blocks out of the reply, executes them locally, and
feeds results back (a short follow-up LLM turn). Tools NEVER post directly —
schedule_draft only queues a draft whose text came from an explicit user
instruction; the publish loop still owns actual sending and safety caps.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from ..core import db
from .algorithm import score_draft, improvement_hints
from .lang import detect
from .llm import LLMError
from .style_scan import voice_match

TOOL_REGISTRY: dict[str, Callable[..., dict]] = {}


def register(name: str, fn: Callable[..., dict]) -> None:
    TOOL_REGISTRY[name] = fn


TOOLS_PROMPT = """TOOLS — you can act, not just advise. When an action is clearly what the
user wants, emit ONE fenced block per action at the END of your reply:

```action
{"tool": "<name>", "args": { ... }}
```

Available tools:
- schedule_draft {text, when: "9pm"|"18:00"|"tomorrow 9am"|"in 2 hours", language?: "ar"|"en"}
    → saves the post as an approved calendar draft at that time (the user's
      explicit instruction here IS the approval; publishing still waits for
      the schedule + safety caps)
- create_quote_draft {tweet_url, angle?}
    → fetches the tweet, drafts an on-voice quote post for approval
- query_analytics {timeframe?: "week"|"month"|"all"}
    → pulls real engagement numbers (best post, totals, best hours)
- pick_idea {}
    → scores the idea bank and returns the top pick with reasoning
- list_drafts {status?: "draft"|"approved", limit?: 5}
    → lists queued drafts with ids and previews — answer "show me my drafts"
      with REAL ids, then suggest approving one
- scan_account {}
    → runs the deep style scan of the connected account
- regenerate_draft {draft_id}
    → re-rolls an existing draft hotter
- web_search {query, limit?: 6}
    → searches the open web (DuckDuckGo, no API) for news/trends; returns
      real titles + snippets — use for "what's trending about X" questions,
      then cite what you actually found in the draft
- x_search {query, limit?: 10}
    → searches X THROUGH THE COOKIE SESSION (no paid API) for live posts
      about a topic — use for "what are people saying about X on X"
- x_trends {limit?: 10}
    → X trending topics right now (cookie session, no API)
- trend_post {topic, source?: "web"|"x"}
    → one-shot: searches the live web/X for the topic, then drafts an
      on-voice post FROM the real findings (approval-gated as always)
- github_drafts {user?, count?: 2}
    → drafts one post per the user's LATEST pushed GitHub repos, grounded
      in the repo description + newest commits (their own real work)
- web_read {url}
    → opens ANY url and returns the readable page text (reader mode) —
      follow up a web_search hit, read the full article/docs before
      drafting from it

Rules: never invent results — the system executes and appends real results.
Keep the prose reply short; let the action carry the work. If the user asks
something informational (best post, analytics), still answer briefly in prose
AND emit the tool so numbers are real."""


# ---------- time parsing ----------

def parse_when(text: str, now: Optional[datetime] = None) -> Optional[str]:
    """Natural-ish time → ISO string. Returns None if unparseable."""
    now = now or datetime.now()
    raw = (text or "").strip()
    if not raw:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", raw):  # ISO passthrough
        return (raw.replace("t", "T").replace(" ", "T"))[:16] + ":00"

    t = raw.lower()

    m = re.match(r"in (\d+)\s*(min|mins|minutes|hour|hours|hr|hrs|h)\b", t)
    if m:
        n = int(m.group(1))
        delta = timedelta(minutes=n) if m.group(2).startswith("min") else timedelta(hours=n)
        return (now + delta).isoformat(timespec="seconds")

    m = re.match(r"(today|tonight|tomorrow)?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if m:
        day_word, hh, mm, ap = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        if day_word == "tonight" and not ap and hh < 12:
            hh += 12
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day_word == "tomorrow":
            target += timedelta(days=1)
        elif target <= now and day_word not in ("today", "tonight"):
            target += timedelta(days=1)
        if day_word in ("today", "tonight") and target <= now:
            return None
        return target.isoformat(timespec="seconds")
    return None


# ---------- action parsing ----------

ACTION_RE = re.compile(r"```action\s*(\{.*?\})\s*```", re.DOTALL)


def parse_actions(reply: str) -> list[dict]:
    """Extract action blocks from an LLM reply. Tolerant to prose around them
    AND to both arg shapes — models emit {"tool","args":{...}} or flat
    {"tool","topic":...}; flat fields merge into args (user report 2026-08-25:
    trend_post got 'topic required' despite the model naming the topic)."""
    actions = []
    for m in ACTION_RE.finditer(reply):
        raw = m.group(1).strip().rstrip(",")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("tool") in TOOL_REGISTRY:
            args = dict(obj.get("args") or {})
            for k, v in obj.items():
                if k not in ("tool", "args"):
                    args.setdefault(k, v)
            actions.append({"tool": obj["tool"], "args": args})
    return actions


def strip_actions(reply: str) -> str:
    return ACTION_RE.sub("", reply).rstrip().rstrip("`").rstrip()


def execute_tool(cfg, name: str, args: dict) -> dict:
    """Run a registered tool locally. Always returns a JSON-safe dict."""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown tool {name}"}
    try:
        return {"ok": True, **fn(cfg, **args)}
    except TypeError as e:
        return {"ok": False, "error": f"bad args for {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


# ---------- the tools (each takes cfg first) ----------

def _tool_schedule_draft(cfg, text: str = "", when: Optional[str] = None,
                         language: Optional[str] = None) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "no text"}
    scheduled = parse_when(when) if when else None
    lang = language or detect(text)
    hour = int(scheduled[11:13]) if scheduled else None
    alg = score_draft(text, now_hour=hour)
    did = db.add_draft(
        text=text, kind="post", temperature="chat",
        scheduled_at=scheduled, status="approved" if scheduled else "draft",
        meta={"source": "chat-tool", "approved_via": "chat-instruction",
              "language": lang, "alg": alg, "voice_match": voice_match(text)})
    db.log("chat", f"tool schedule_draft → #{did} at {scheduled}")
    return {"draft_id": did, "scheduled_at": scheduled,
            "note": "queued on the calendar" if scheduled
                    else "saved as draft (no time given)"}


def _parse_tweet_url(url: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(r"x\.com/(\w+)/status/(\d+)", url or "")
    if m:
        return m.group(2), m.group(1)
    if re.fullmatch(r"\d{10,}", (url or "").strip()):
        return url.strip(), None
    return None, None


def _tool_create_quote_draft(cfg, tweet_url: str = "", angle: str = "") -> dict:
    x_id, author = _parse_tweet_url(tweet_url)
    if not x_id:
        return {"ok": False, "error": "could not parse a tweet id from that URL"}
    from ..x.client import build_client
    from ..core.config import load_config
    client = build_client(load_config())
    tweet = asyncio.run(client.get_tweet(x_id))
    if not tweet or not tweet.get("text"):
        tweet = {"x_id": x_id, "text": "(preview unavailable)", "author": author or ""}
    from . import drafts as drafts_mod
    did = drafts_mod.generate_quote_draft(cfg, tweet, angle=angle)
    return {"draft_id": did,
            "quoted": f"@{tweet.get('author', '')}: {tweet.get('text', '')[:100]}"}


def _tool_query_analytics(cfg, timeframe: str = "week") -> dict:
    days = {"week": 7, "month": 30, "all": 3650}.get(timeframe)
    if days is None:
        raise ValueError(f"timeframe must be week|month|all, got {timeframe!r}")
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE is_own=1 AND created_at > ? ORDER BY created_at DESC",
            (cutoff,)).fetchall()
    posts = [dict(r) for r in rows]
    if not posts:
        return {"timeframe": timeframe, "posts": 0, "note": "no data in range"}
    best = max(posts, key=lambda p: (p.get("engagement") or 0))
    hours: dict[int, float] = {}
    for p in posts:
        ca = p.get("created_at") or ""
        if "T" in ca:
            try:
                hours[int(ca[11:13])] = hours.get(int(ca[11:13]), 0) + (p.get("engagement") or 0)
            except ValueError:
                pass
    best_hours = sorted(hours, key=hours.get, reverse=True)[:3]
    return {
        "timeframe": timeframe, "posts": len(posts),
        "total_impressions": sum(p.get("impressions") or 0 for p in posts),
        "total_engagement": sum((p.get("likes") or 0) + 3 * (p.get("reposts") or 0)
                                + 8 * (p.get("replies") or 0) for p in posts),
        "best_post": {"text": (best.get("text") or "")[:140],
                      "likes": best.get("likes"), "replies": best.get("replies")},
        "best_hours": best_hours,
    }


def _tool_pick_idea(cfg) -> dict:
    ideas = db.fresh_ideas(10)
    if not ideas:
        return {"ok": False, "error": "idea bank empty — run the study loop"}
    top = ideas[0]
    alg = score_draft(f"{top['title']}. {top['angle']}")
    return {"top": {"title": top["title"], "angle": top["angle"],
                    "format": top.get("format"), "idea_score": top.get("score")},
            "algorithm_preview": {"score": alg["score"], "grade": alg["grade"],
                                  "risks": improvement_hints(alg)[:3]},
            "alternatives": [i["title"] for i in ideas[1:4]]}


def _tool_list_drafts(cfg, status: str = "draft", limit: int = 5) -> dict:
    """Real draft ids for "show me my drafts" — natural-language parity for
    both the dashboard chat and Telegram (which has no /drafts button)."""
    if status not in ("draft", "approved", "published", "rejected"):
        return {"ok": False,
                "error": f"status must be draft|approved|published|rejected, "
                         f"got {status!r}"}
    limit = max(1, min(int(limit), 10))
    rows = db.drafts_by_status(status, limit)
    return {"status": status, "count": len(rows),
            "drafts": [{"id": d["id"],
                        "text": " ".join((d.get("text") or "").split())[:120],
                        "scheduled_at": d.get("scheduled_at")}
                       for d in rows]}


def _tool_scan_account(cfg) -> dict:
    from . import style_scan as scan_mod
    from ..x.client import build_client
    from ..core.config import load_config
    client = build_client(load_config())
    profile = asyncio.run(scan_mod.scan_account(cfg, client, max_posts=200))
    return {"posts_scanned": profile["stats"]["posts_scanned"],
            "languages": profile["stats"]["language_mix"],
            "summary": (profile.get("human_summary") or "")[:300]}


def _tool_regenerate_draft(cfg, draft_id: int = 0) -> dict:
    from . import drafts as drafts_mod
    try:
        new_id = drafts_mod.regenerate(int(draft_id))
    except LLMError as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"new_draft_id": new_id}


register("schedule_draft", _tool_schedule_draft)
register("create_quote_draft", _tool_create_quote_draft)
register("query_analytics", _tool_query_analytics)
register("pick_idea", _tool_pick_idea)
register("list_drafts", _tool_list_drafts)
register("scan_account", _tool_scan_account)
register("regenerate_draft", _tool_regenerate_draft)

# Tools run inside a worker thread (server: asyncio.to_thread) — the
# asyncio.run() calls above get a fresh loop there safely.



# ---------- live search tools (web + X-without-API) ----------

def _tool_web_search(cfg, query: str = "", limit: int = 6) -> dict:
    from . import websearch
    res = websearch.web_search(query, limit=int(limit))
    if not res:
        return {"ok": True, "results": [],
                "note": "no results (or search unreachable) — say so honestly"}
    return {"ok": True, "results": res}


def _tool_x_search(cfg, query: str = "", limit: int = 10) -> dict:
    from . import websearch
    res = websearch.x_search(cfg, query, limit=int(limit))
    return {"ok": True, "results": [
        {"text": (p.get("text") or "")[:200],
         "author": p.get("author_handle") or p.get("author") or "?",
         "likes": p.get("likes", 0)} for p in res]}


def _tool_x_trends(cfg, limit: int = 10) -> dict:
    from . import websearch
    return {"ok": True, "trends": websearch.x_trends(cfg, limit=int(limit))}


_TREND_POST_SYSTEM = (
    "You write ONE X post in the user's voice from LIVE search findings. "
    "Output STRICT JSON: {\"text\": \"...\"}. Under 240 chars, concrete, "
    "reference the actual finding (a number, a name, a launch), no hashtags."
)


def _tool_trend_post(cfg, topic: str = "", source: str = "web") -> dict:
    """Search live (web or X), then draft from the real findings."""
    from . import websearch
    from .llm import chat, extract_json
    from . import voice as voice_mod
    from ..core import db as db_mod
    topic = (topic or "").strip()
    if not topic:
        return {"ok": True, "error": "topic required"}
    if source == "x":
        found = websearch.x_search(cfg, topic, limit=8)
        material = chr(10).join(
            f"@{p.get('author_handle','?')}: {(p.get('text') or '')[:200]}" for p in found)
        where = "X (live posts)"
    else:
        found = websearch.web_search(topic, limit=6)
        material = chr(10).join(
            f"{r['title']}: {r['snippet']}" for r in found)
        where = "the web (DuckDuckGo)"
    if not material:
        return {"ok": True, "error": f"nothing found on {where} for '{topic}'"}
    voice = voice_mod.voice_prompt_block()  # ACTIVE account voice+style
    user = (f"TOPIC: {topic}" + chr(10) +
            f"LIVE FINDINGS ({where}):" + chr(10) + material[:2400] + chr(10) +
            f"USER VOICE: {str(voice)[:300]}" + chr(10) +
            "Write the post now, grounded in a specific finding.")
    raw = chat(cfg.llm, _TREND_POST_SYSTEM, user, json_mode=True)
    data = extract_json(raw)
    text = (data.get("text") or "").strip() if isinstance(data, dict) else ""
    if not text:
        return {"ok": True, "error": "draft generation failed — try again"}
    # eligible trend posts get the auto hook-card, same as the create loop
    image = None
    try:
        from . import quote_card
        image = quote_card.make_card(text)
    except Exception:  # noqa: BLE001 — never lose the draft over art
        image = None
    did = db_mod.add_draft(text=text, kind="post", temperature="bold",
                           image=image,
                           meta={"source": "trend-post", "topic": topic,
                                 "search_source": where})
    return {"ok": True, "draft_id": did, "text": text, "image": image,
            "sources": [r.get("url") or r.get("author") for r in found[:3]]}


register("web_search", _tool_web_search)
register("x_search", _tool_x_search)
register("x_trends", _tool_x_trends)
register("trend_post", _tool_trend_post)



# ---------- GitHub: draft from the user's own repos ----------

def _tool_github_drafts(cfg, user: str = "", count: int = 2) -> dict:
    from . import github_posts as gh
    from ..core import db as db_mod
    user = (user or "").strip() or gh.github_handle(cfg)
    if not user:
        return {"ok": True, "error": "no github user configured — pass user="
                                     '"your-gh-name" or add it to the strategy file'}
    ids = gh.run(cfg, user, int(count))
    if not ids:
        return {"ok": True, "ids": [], "note": "no drafts — repos missing "
                "descriptions or drafts too similar to recent ones"}
    return {"ok": True, "draft_ids": ids, "repos": len(ids)}


register("github_drafts", _tool_github_drafts)



# ---------- web_read: the agent's way into any page ----------

def _tool_web_read(cfg, url: str = "") -> dict:
    from . import websearch
    if not (url or "").strip():
        return {"ok": True, "error": "url required"}
    res = websearch.web_read(url.strip())
    if not res.get("ok"):
        return {"ok": True, "error": res.get("error", "unreadable")}
    return {"ok": True, "title": res["title"], "url": res["url"],
            "text": res["text"][:4000]}


register("web_read", _tool_web_read)
