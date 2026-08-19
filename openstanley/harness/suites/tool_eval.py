"""Tool correctness eval — scripted chat prompts select the right tool.

Each scenario feeds a natural chat request through the tool prompt and asserts
the parsed action names the correct tool with correctly parsed arguments.
Actions are ONLY parsed and validated — never executed — so this suite can
never touch X or mutate state.
"""
from __future__ import annotations

from datetime import datetime

from ..base import EvalContext
from ...gen.tools import TOOLS_PROMPT, parse_actions, parse_when

SCENARIOS = [
    {
        "name": "schedule for 9pm",
        "prompt": "Schedule this for 9pm: 'shipped the ugly version today'",
        "tool": "schedule_draft",
        "arg_check": "when_21h",
    },
    {
        "name": "quote a tweet",
        "prompt": "Quote https://x.com/naval/status/1732798956375705112 with my angle",
        "tool": "create_quote_draft",
        "arg_check": "tweet_url",
    },
    {
        "name": "best post this week",
        "prompt": "What was my best post this week?",
        "tool": "query_analytics",
        "arg_check": "timeframe_week",
    },
    {
        "name": "pick an idea",
        "prompt": "Pick the best idea from the bank",
        "tool": "pick_idea",
        "arg_check": "none",
    },
    {
        "name": "scan the account",
        "prompt": "Scan my account style",
        "tool": "scan_account",
        "arg_check": "none",
    },
]

SYSTEM = ("You are OpenStanley, the account owner's AI Head of Content.\n"
          + TOOLS_PROMPT)


def _check_args(scenario: dict, args: dict) -> tuple[bool, str]:
    check = scenario["arg_check"]
    if check == "when_21h":
        iso = parse_when(str(args.get("when") or ""), now=datetime.now().replace(
            hour=8, minute=0, second=0, microsecond=0))  # pin 08:00 → 9pm is later today
        return iso is not None and iso[11:13] == "21", f"when→{iso}"
    if check == "tweet_url":
        url = str(args.get("tweet_url") or "")
        return "x.com/naval/status/" in url, f"tweet_url→{url[:60]}"
    if check == "timeframe_week":
        return args.get("timeframe") in ("week", None), f"timeframe→{args.get('timeframe')}"
    return True, "no args expected"


def run(ctx: EvalContext) -> dict:
    results = []
    for sc in SCENARIOS:
        reply = ctx.llm(ctx.cfg.llm, system=ctx.brain_prefix() + SYSTEM,
                        user=sc["prompt"], temperature=0.4, json_mode=False)
        actions = parse_actions(reply)
        tool_ok = bool(actions) and actions[0]["tool"] == sc["tool"]
        arg_ok, arg_note = _check_args(sc, actions[0]["args"] if actions else {})
        results.append({"scenario": sc["name"], "prompt": sc["prompt"][:60],
                        "expected": sc["tool"],
                        "got": actions[0]["tool"] if actions else None,
                        "args_ok": arg_ok, "arg_note": arg_note,
                        "passed": tool_ok and arg_ok})
    score = round(100 * sum(1 for r in results if r["passed"])
                  / max(1, len(results)), 1)
    return {
        "score": score,
        "details": {"scenarios": results,
                    "note": "actions parsed + validated only — never executed"},
    }
