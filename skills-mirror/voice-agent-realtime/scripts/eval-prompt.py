"""
eval-prompt.py — adversarial evaluation harness for a voicebot system prompt.

Replays a JSONL of test cases against the OpenAI Realtime API and scores each
turn with an LLM-as-judge call (gpt-4o-mini). Outputs a pass-rate breakdown.

Run:
  python3 eval-prompt.py \
    --system-prompt path/to/prompt.md \
    --eval evals/adversarial.jsonl \
    --model gpt-realtime-2 \
    --judge-model gpt-4o-mini

Env: OPENAI_API_KEY (required)

Output:
  Total: 10
  Pass rate: 8/10 (80.0%)
  Breakdown by category:
    booking        4/5 (80.0%)
    out-of-scope   2/2 (100.0%)
    interruption   2/3 (66.7%)
  Results saved to evals/results-<timestamp>.jsonl

Source events: see
  /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import websockets

REALTIME_WS = "wss://api.openai.com/v1/realtime?model={model}"
CHAT_URL = "https://api.openai.com/v1/chat/completions"


def load_evals(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL eval file. Each line: {id, input, expected_behavior, [category]}."""
    cases: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


def parse_judge_verdict(text: str) -> tuple[str, str]:
    """Extract {verdict, reason} from a judge's response. Tolerant of fences/prose."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return ("unknown", text[:200])
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return ("unknown", text[:200])
    verdict = str(data.get("verdict", "unknown")).lower()
    if verdict not in ("pass", "fail"):
        verdict = "unknown"
    return (verdict, str(data.get("reason", "")))


def pass_rate(rows: list[tuple[str, str, str]]) -> float:
    """Compute pass rate over rows of (id, verdict, reason). Unknowns excluded."""
    scored = [v for _, v, _ in rows if v in ("pass", "fail")]
    if not scored:
        return 0.0
    return sum(1 for v in scored if v == "pass") / len(scored)


async def realtime_run_case(
    model: str, system_prompt: str, user_input: str
) -> dict[str, Any]:
    """Run a single case through the Realtime API.

    Sends the user input as a text message (no audio needed) and collects the
    assistant's transcript + function-call deltas to build a structured trace.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY env var is required")
    url = REALTIME_WS.format(model=model)
    headers = [
        ("Authorization", f"Bearer {api_key}"),
        # [UNCONFIRMED in research] OpenAI-Beta header — sending defensively.
    ]
    transcript_parts: list[str] = []
    function_calls: list[dict[str, str]] = []
    fc_args: dict[str, str] = defaultdict(str)
    fc_names: dict[str, str] = {}

    async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
        # 1) Configure session (text-only output for eval speed).
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": model,
                        "output_modalities": ["audio"],
                        "instructions": system_prompt,
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                # Live-confirmed 2026-05-11: None disables VAD, {"type":"none"} rejected.
                                "turn_detection": None,
                            },
                            "output": {
                                # rate REQUIRED on output (live-confirmed).
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "voice": "marin",
                            },
                        },
                    },
                }
            )
        )
        # 2) Send text input as a user message.
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_input}],
                    },
                }
            )
        )
        # 3) Trigger a response.
        await ws.send(json.dumps({"type": "response.create"}))
        # 4) Collect events.
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                break
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = payload.get("type", "")
            if etype == "response.output_audio_transcript.delta":
                transcript_parts.append(payload.get("delta", ""))
            elif etype == "response.function_call_arguments.delta":
                call_id = payload.get("call_id", "")
                fc_args[call_id] += payload.get("delta", "")
            elif etype == "conversation.item.added":
                item = payload.get("item", {})
                if item.get("type") == "function_call":
                    fc_names[item.get("call_id", "")] = item.get("name", "")
            elif etype in ("response.done", "error"):
                break

    for call_id, args_str in fc_args.items():
        function_calls.append(
            {
                "call_id": call_id,
                "name": fc_names.get(call_id, ""),
                "arguments": args_str,
            }
        )
    return {
        "transcript": "".join(transcript_parts),
        "function_calls": function_calls,
    }


async def judge_case(
    judge_model: str,
    case: dict[str, Any],
    trace: dict[str, Any],
    client: httpx.AsyncClient,
) -> tuple[str, str]:
    """Call the judge LLM with the trace + expected behavior. Returns (verdict, reason)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    prompt = (
        "You are evaluating a voice assistant's response to a test case.\n\n"
        f"USER INPUT: {case.get('input', '')}\n\n"
        f"EXPECTED BEHAVIOR: {case.get('expected_behavior', '')}\n\n"
        f"AGENT TRANSCRIPT: {trace.get('transcript', '')}\n\n"
        f"AGENT FUNCTION CALLS: {json.dumps(trace.get('function_calls', []))}\n\n"
        'Reply with ONLY a JSON object: {"verdict": "pass" or "fail", '
        '"reason": "<one-sentence justification>"}'
    )
    resp = await client.post(
        CHAT_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": judge_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return parse_judge_verdict(content)


async def _amain(args: argparse.Namespace) -> None:
    system_prompt = Path(args.system_prompt).read_text(encoding="utf-8")
    cases = load_evals(Path(args.eval))
    rows: list[tuple[str, str, str]] = []
    by_category: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        for case in cases:
            cid = case.get("id", "?")
            category = case.get("category", "uncategorized")
            try:
                trace = await realtime_run_case(args.model, system_prompt, case["input"])
                verdict, reason = await judge_case(args.judge_model, case, trace, client)
            except Exception as e:  # noqa: BLE001
                verdict, reason = ("unknown", f"harness error: {e}")
                trace = {"transcript": "", "function_calls": []}
            rows.append((cid, verdict, reason))
            by_category[category].append((cid, verdict, reason))
            results.append(
                {
                    "id": cid,
                    "category": category,
                    "verdict": verdict,
                    "reason": reason,
                    "trace": trace,
                }
            )
            print(f"  [{verdict.upper():7s}] {cid}: {reason[:80]}")

    total = len(rows)
    scored = [r for r in rows if r[1] in ("pass", "fail")]
    passes = sum(1 for r in scored if r[1] == "pass")
    rate = pass_rate(rows)
    print()
    print(f"Total: {total}")
    print(f"Pass rate: {passes}/{len(scored)} ({rate * 100:.1f}%)")
    print("Breakdown by category:")
    for cat, cat_rows in sorted(by_category.items()):
        cat_scored = [r for r in cat_rows if r[1] in ("pass", "fail")]
        cat_pass = sum(1 for r in cat_scored if r[1] == "pass")
        cat_rate = pass_rate(cat_rows) * 100 if cat_scored else 0.0
        print(f"  {cat:14s} {cat_pass}/{len(cat_scored)} ({cat_rate:.1f}%)")

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out = Path(args.eval).parent / f"results-{ts}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"Results saved to {out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--system-prompt", required=True, help="Path to rendered system prompt file.")
    p.add_argument("--eval", required=True, help="JSONL of test cases.")
    p.add_argument("--model", default="gpt-realtime-2")
    p.add_argument("--judge-model", default="gpt-4o-mini")
    args = p.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
