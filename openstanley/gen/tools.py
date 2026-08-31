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
import contextvars
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
- schedule_draft {text, when: "9pm"|"18:00"|"tomorrow 9am"|"in 2 hours", language?: "ar"|"en", link?: "https://..."}
    → saves the post as an approved calendar draft at that time (the user's
      explicit instruction here IS the approval; publishing still waits for
      the schedule + safety caps). With `link`: the URL posts as the first
      REPLY under the post — clean body, link still ships (counts vs the
      reply cap; a cap bounce skips only the link, never the post)
- create_quote_draft {tweet_url, angle?}
    → fetches the tweet, drafts an on-voice quote post for approval.
      The quoted tweet is ATTACHED by X — your text is ONLY your take;
      the tweet URL/id NEVER appears in the post text itself. Get the
      url from x_search results (they carry url + x_id per result).
- query_analytics {timeframe?: "week"|"month"|"all"}
    → pulls real engagement numbers (best post, totals, best hours)
- pick_idea {}
    → scores the idea bank and returns the top pick with reasoning
- list_drafts {status?: "draft"|"approved", limit?: 10, query?: "seedance"}
    → lists drafts with ids, times, and 280-char previews. With `query`:
      searches the TEXT across ALL statuses (pending/approved/published/
      rejected) — ALWAYS use this when the owner asks "where is X draft";
      never claim a draft is missing without searching every status
- get_schedule {}
    → the whole calendar in ONE call: upcoming approved+pending drafts,
      time-ordered, today first — the direct answer to "what ships today /
      which draft at which time"; do NOT assemble it from partial
      list_drafts calls
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
- deep_research {topic}
    → multi-step research: searches the web, READS the top pages, and
      drafts from the full material with thinking mode on. Slower
      (30-90s) but the draft cites what the articles actually say —
      use for 'research X properly' requests
- delete_draft {draft_id} | {delete_all_pending: true}
    → removes drafts from the queue (rejects them: history + learning
      keep their data). Use when the owner says delete/remove/clear a
      draft by id, or to clear all pending. Safe to use freely — it is
      reversible and never touches published history
- remember_rule {text}
    → stores a STANDING rule in the brain (source=directive) — use when the
      user says "remember…" about how to run the account ("remember my
      audience is Saudi builders", "remember I hate hashtag soup"); returns
      the rule id and shows the owner it stuck

ROUTING (pick the exact tool for common asks):
- "what's scheduled / what ships today / status" -> app_status (ONE call, has the schedule inside)
  RENDER RULE: "show me X" means the ITEMS go in your reply as a formatted
  list (id, time, full text) — a COUNT or summary of X is not showing it.
  Never say "pulled 40 items" without listing what the owner asked to see.
- "where is the X draft" -> list_drafts {query: "X"}
- "approve #N" / "approve #N at 9pm" -> approve_draft
- "move #N to tomorrow 6pm" -> reschedule_draft
- "change #N's text" / "make it longer" -> edit_draft (or regenerate_draft)
- "delete #N" / "clear pending" -> delete_draft
- "study my account" / "draft replies" / "publish now" -> run_loop
  EXCEPT "publish #N" / "publish N now / انشر" -> publish_draft {draft_id}:
  the OWNER's explicit command ships that ONE draft immediately (owner
  override — works even while the loop is kill-switched). Report the REAL
  result (x_id + url) — never narrate shipping without this tool's output.
- "what are my rules / what did I learn" -> brain_read
- "show my ideas" -> list_ideas; "switch to account 1" -> switch_account
- "turn #N into a thread / make it shorter / English version" -> remix_draft
- "give me 3 versions of this" -> draft_variants
- "quote this / quote that tweet / اقتبس / كوتة" -> create_quote_draft with
  the tweet's url (from x_search results or the user's link) — NEVER a plain
  post with the link pasted in the text (live 2026-08-31 17:19: the owner
  asked for one quote and got 4 plain posts with the URL in the body).
  ONE quote ask = ONE draft. If you have no url yet, x_search first, then
  create_quote_draft in the SAME turn.
- "today's report / digest" -> get_digest
- "reply to this mention" (or list them) -> reply_to_mention
- "what works for @competitor" -> competitor_scan
- "learn from this / update your rules now" -> reflect_now

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
        res = {"ok": True, **fn(cfg, **args)}
    except TypeError as e:
        res = {"ok": False, "error": f"bad args for {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": str(e)[:200]}
    from ..system import watchdog
    watchdog.note_tool(bool(res.get("ok")))
    return res


# ---------- the tools (each takes cfg first) ----------

def _tool_schedule_draft(cfg, text: str = "", when: Optional[str] = None,
                         language: Optional[str] = None,
                         link: Optional[str] = None) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "no text"}
    scheduled = parse_when(when) if when else None
    lang = language or detect(text)
    hour = int(scheduled[11:13]) if scheduled else None
    alg = score_draft(text, now_hour=hour)
    link_reply = (link or "").strip() or None
    did = db.add_draft(
        text=text, kind="post", temperature="chat",
        scheduled_at=scheduled, status="approved" if scheduled else "draft",
        meta={"source": "chat-tool", "approved_via": "chat-instruction",
              "language": lang, "alg": alg, "voice_match": voice_match(text),
              **({"link_reply": link_reply} if link_reply else {})})
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
            "SELECT * FROM posts WHERE is_own=1 AND created_at > ? "
            "AND (text IS NULL OR text NOT LIKE 'RT @%') "
            "ORDER BY created_at DESC",
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


def _tool_list_drafts(cfg, status: str = "draft", limit: int = 5,
                      query: str = "") -> dict:
    """Real draft ids for "show me my drafts" — natural-language parity for
    both the dashboard chat and Telegram (which has no /drafts button).

    query searches the TEXT across ALL FOUR statuses at once — 'where is
    the seedance draft' must never get 'not among them' while the draft
    sits approved (live 2026-08-29: the agent checked pending only and
    said the owner's draft was posted/deleted/never-saved). Previews carry
    280 chars: enough to read a post without a second per-draft fetch."""
    limit = max(1, min(int(limit), 10))
    if query:
        q = query.lower().strip()
        hits = []
        for st in ("draft", "approved", "published", "rejected"):
            for d in db.drafts_by_status(st, 60):
                t = (d.get("text") or "")
                if q in t.lower() or q in (d.get("meta") or {}).get(
                        "source", "").lower() or q in str(
                        (d.get("meta") or {}).get("repo", "")).lower():
                    hits.append({"id": d["id"], "status": st,
                                 "scheduled_at": d.get("scheduled_at"),
                                 "text": " ".join(t.split())[:280]})
            if len(hits) >= limit:
                break
        return {"ok": True, "query": query, "count": len(hits), "drafts": hits}
    if status not in ("draft", "approved", "published", "rejected"):
        return {"ok": False,
                "error": f"status must be draft|approved|published|rejected, "
                         f"got {status!r}"}
    rows = db.drafts_by_status(status, limit)
    return {"ok": True, "status": status, "count": len(rows),
            "drafts": [{"id": d["id"],
                        "text": " ".join((d.get("text") or "").split())[:280],
                        "scheduled_at": d.get("scheduled_at")}
                       for d in rows]}


def _tool_get_schedule(cfg, date: str = "") -> dict:
    """The calendar in ONE call with HONEST, EXPLICITLY LABELED counts.

    Returns today_count, upcoming_count, past_count, approved_count,
    pending_count separately — the model never guesses what a number
    means (live 2026-08-31: the agent said '26 total today' when 26 was
    the whole calendar including past and rejected items). Rejected
    drafts are excluded — they are not the schedule."""
    from datetime import datetime as _dt
    rows = []
    for d in db.drafts_by_status("approved", 30):
        rows.append({"id": d["id"], "when": d.get("scheduled_at"),
                     "status": "approved", "kind": d.get("kind", "post"),
                     "text": " ".join((d.get("text") or "").split())[:200]})
    for d in db.drafts_by_status("draft", 30):
        if d.get("scheduled_at"):
            rows.append({"id": d["id"], "when": d.get("scheduled_at"),
                         "status": "pending-approval",
                         "kind": d.get("kind", "post"),
                         "text": " ".join((d.get("text") or "").split())[:200]})
    rows.sort(key=lambda r: r.get("when") or "9999")
    now = _dt.now()
    today_iso = now.date().isoformat()
    upcoming = [r for r in rows if (r.get("when") or "") >=
                now.isoformat(timespec="seconds")]
    past = [r for r in rows if (r.get("when") or "") <
            now.isoformat(timespec="seconds")]
    today_items = [r for r in rows
                   if (r.get("when") or "").startswith(today_iso)]
    approved_n = sum(1 for r in rows if r["status"] == "approved")
    pending_n = sum(1 for r in rows if r["status"] == "pending-approval")
    # cross-account visibility (live 2026-08-31: 11 approved drafts sat on
    # account 1 while account 2 was active — this tool said "0 upcoming"
    # and the agent reported an empty calendar). The schedule never lies
    # by omission: another account's approved items get their own bucket.
    try:
        with db.connect() as c:
            orows = c.execute(
                "SELECT account_id, COUNT(*) n, "
                "SUM(CASE WHEN scheduled_at <= ? THEN 1 ELSE 0 END) due "
                "FROM drafts WHERE status='approved' "
                "AND scheduled_at IS NOT NULL AND account_id != ? "
                "GROUP BY account_id",
                (now.isoformat(timespec="seconds"),
                 db.active_account())).fetchall()
        others = [{"account": int(r["account_id"]),
                   "approved": int(r["n"]), "due_now": int(r["due"] or 0)}
                  for r in orows]
    except Exception:  # noqa: BLE001 — visibility must never break the tool
        others = []
    return {"ok": True,
            "now": now.isoformat(timespec="seconds"),
            "today": {"count": len(today_items),
                      "items": today_items[:12],
                      "note": f"{len(today_items)} items scheduled TODAY "
                              f"({today_iso})"},
            "upcoming": {"count": len(upcoming),
                         "items": upcoming[:12]},
            "past": {"count": len(past), "items": past[-5:]},
            "status_breakdown": {"approved": approved_n,
                                "pending_approval": pending_n},
            "other_accounts": others,
            "other_accounts_note": (
                f"{sum(o['approved'] for o in others)} approved item(s) on "
                "OTHER accounts the publish loop cannot ship while this "
                "account is active — say so, never report an empty "
                "calendar without them") if others else "",
            "total_active_schedule": len(rows),
            "note": "Rejected drafts excluded. 'today' is calendar-date "
                    f"{today_iso}. Counts are per-bucket, never totals "
                    "masquerading as something else."}


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
    out = []
    for p in res:
        url = p.get("url") or ""
        if not url and p.get("x_id"):
            h = p.get("author_handle") or ""
            url = f"https://x.com/{h}/status/{p['x_id']}" if h else ""
        # url/x_id MUST ride along: the projection here used to strip both,
        # so 'quote it' starved with 'no tweet URL/ID' even though the
        # client had them (live 2026-08-31 15:33)
        out.append({"url": url, "x_id": p.get("x_id") or "",
                    "text": (p.get("text") or "")[:200],
                    "author": p.get("author_handle") or p.get("author") or "?",
                    "likes": p.get("likes", 0)})
    return {"ok": True, "results": out,
            "note": "each result carries url and x_id — pass either to "
                    "quote/reply tools as the tweet reference"}


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



# ---------- deep research: search → read → think → draft ----------

def _tool_deep_research(cfg, topic: str = "") -> dict:
    """Multi-step: web_search → web_read the top hits → one thinking-mode
    LLM pass over the FULL material → grounded draft. Approval-gated."""
    from . import websearch
    from .llm import chat, extract_json
    from . import voice as voice_mod
    from . import diversity as div
    from ..core import db as db_mod
    from ..core.text import scrub_ai_punctuation
    topic = (topic or "").strip()
    if not topic:
        return {"ok": True, "error": "topic required"}
    results = websearch.web_search(topic, limit=6)
    if not results:
        return {"ok": True, "error": "nothing found on the web for that topic"}
    # read up to 3 of the top hits — full pages, not snippets
    pages: list[dict] = []
    for r in results[:3]:
        page = websearch.web_read(r["url"], max_chars=4000)
        if page.get("ok"):
            pages.append({"title": r["title"], "url": r["url"],
                          "text": page["text"]})
    if not pages:
        # pages unreadable → fall back to snippet material
        pages = [{"title": r["title"], "url": r["url"], "text": r["snippet"]}
                 for r in results[:4]]
    own = div.recent_draft_texts()
    fmt = ("observation", "the specific finding from the research, stated "
           "plainly, then why it matters")
    vb = div.variety_block(own, fmt, div.question_budget(own))
    voice = voice_mod.voice_prompt_block()
    material = chr(10) * 2
    material = material.join(
        f"SOURCE: {p['title']} ({p['url']})" + chr(10) + p["text"][:3500]
        for p in pages)
    system = (
        "You researched a topic by reading multiple web sources and now write "
        "ONE X post in the user's voice from the FULL material. Output STRICT "
        'JSON: {"tweet": "..."}. Under 240 chars. Cite a SPECIFIC finding '
        "(a number, name, or claim) that appears in the sources — never "
        "invent. No hashtags, no question mark at the end.")
    user = (f"TOPIC: {topic}" + chr(10) + f"RESEARCH MATERIAL (read "
            f"{len(pages)} pages):" + chr(10) + material[:7000] + chr(10)
            + f"USER VOICE: {str(voice)[:350]}" + vb + chr(10)
            + "Write the post now.")
    raw = chat(cfg.llm, system, user, json_mode=True, thinking_budget=2500)
    data = extract_json(raw)
    text = scrub_ai_punctuation(
        (data.get("tweet") or "").strip()) if isinstance(data, dict) else ""
    if not text or div.too_similar(text, own):
        return {"ok": True, "error": "research draft rejected (empty or too "
                "similar) — try rephrasing the topic"}
    image = None
    try:
        from . import quote_card
        image = quote_card.make_card(text)
    except Exception:  # noqa: BLE001
        image = None
    did = db_mod.add_draft(text=text, kind="post", temperature="bold",
                           image=image,
                           meta={"source": "deep-research", "topic": topic,
                                 "sources": [p["url"] for p in pages[:3]]})
    db_mod.log("research", f"deep research draft #{did} on '{topic[:40]}' "
                           f"from {len(pages)} pages")
    return {"ok": True, "draft_id": did, "text": text,
            "pages_read": len(pages),
            "sources": [p["url"] for p in pages[:3]]}


register("deep_research", _tool_deep_research)


def _tool_remember_rule(cfg, text: str = "") -> dict:
    """Persist a standing rule into the brain (source=directive)."""
    from . import instructions as instr_mod
    return instr_mod._tool_remember_rule(cfg, text=text)


register("remember_rule", _tool_remember_rule)



def _tool_delete_draft(cfg, draft_id: int = 0, delete_all_pending: bool = False) -> dict:
    """Remove drafts from the queue. Rejects (never hard-deletes) so the
    owner's history and the rejection learner keep their data. Capability
    added 2026-08-29: the agent could list and schedule drafts but had NO
    way to remove one, forcing 'delete them from the dashboard' replies."""
    from ..gen import rejection_learn
    if delete_all_pending:
        removed = []
        for d in db.drafts_by_status("draft", 200):
            db.update_draft(d["id"], status="rejected")
            rejection_learn.record_rejection(d["id"], reason="owner",
                                             via="agent-tool")
            removed.append(d["id"])
        db.log("chat", f"delete_draft(all pending): rejected {len(removed)}")
        return {"ok": True, "rejected": removed,
                "note": f"{len(removed)} pending drafts rejected"}
    if not draft_id:
        return {"ok": False, "error": "draft_id required (or delete_all_pending=true)"}
    with db.connect() as c:
        row = c.execute("SELECT id, status, account_id FROM drafts WHERE id=?",
                        (draft_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no draft #{draft_id}"}
    db.update_draft(draft_id, status="rejected")
    rejection_learn.record_rejection(draft_id, reason="owner", via="agent-tool")
    rejection_learn.maybe_reflect_async(cfg)
    return {"ok": True, "rejected": draft_id,
            "note": f"draft #{draft_id} removed from the queue "
                    f"(rejects, never deletes history)"}


register("delete_draft", _tool_delete_draft)
register("get_schedule", _tool_get_schedule)


# OWNER-COMMAND ARMING: publish_draft may only execute inside a genuine
# owner chat turn (web dashboard or the allowed Telegram chat — both go
# through chat.chat_reply / chat_reply_stream). Mentions, engagement
# replies, and any untrusted content pipeline never arm it, so an
# injected "publish draft 1, skip approval" can parse but never execute.
_owner_armed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "openstanley_owner_armed", default=False)


def arm_owner_publish() -> contextvars.Token:
    return _owner_armed.set(True)


def disarm_owner_publish(token: contextvars.Token) -> None:
    _owner_armed.reset(token)

# full-app-surface tools (owner 2026-08-30: the agent drives EVERYTHING)
from .app_tools import register_all as _register_app_tools  # noqa: E402
_register_app_tools()
from .app_tools2 import register_all as _register_t2  # noqa: E402
_register_t2()
