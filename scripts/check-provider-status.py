#!/usr/bin/env python3
"""Poll provider status pages, persist state, alert on degradation changes.

For each provider, we extract a single normalized state out of their
status-page API and compare it against the last-known state stored in
`provider-status/state.json`. When the state changes (e.g.
`operational` → `degraded`), we post a Discord webhook message.

Providers fall into three buckets:
  * Statuspage.io (most): /api/v2/summary.json with `.status.indicator`
    (none/minor/major/critical) + `.status.description`.
  * Google Cloud (Gemini, etc.): /incidents.json — flat list; we look at
    unresolved entries.
  * Custom (Railway, ManyChat): no JSON API. We probe the HTML root and
    record only HTTP reachability.

Designed to run as a GitHub Action every 10 min. Idempotent: re-running
without a state change is a no-op. The DISCORD_WEBHOOK env var is
optional — without it, the script still updates state but skips alerts.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# (display_name, kind, url, optional_extra) tuples.
# `kind` decides the parser.
PROVIDERS: list[tuple[str, str, str]] = [
    ("Cloudflare",    "statuspage", "https://www.cloudflarestatus.com/api/v2/summary.json"),
    ("OpenAI",        "statuspage", "https://status.openai.com/api/v2/summary.json"),
    ("Clerk",         "statuspage", "https://status.clerk.com/api/v2/summary.json"),
    ("Calendly",      "statuspage", "https://www.calendlystatus.com/api/v2/summary.json"),
    ("GitHub",        "statuspage", "https://www.githubstatus.com/api/v2/summary.json"),
    ("Google Cloud",  "google",     "https://status.cloud.google.com/incidents.json"),
    ("Railway",       "http",       "https://status.railway.com"),
    ("ManyChat",      "http",       "https://status.manychat.com"),
]

STATE_FILE = Path("provider-status/state.json")

# Map Statuspage indicators → severity rank (higher = worse).
SP_RANK = {"none": 0, "minor": 1, "major": 2, "critical": 3, "maintenance": 0, "unknown": 0}


def fetch(url: str, timeout: int = 10) -> tuple[int, str]:
    """Return (status_code, body). Network errors return (0, '')."""
    req = urllib.request.Request(url, headers={"User-Agent": "turi-status-monitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def check_statuspage(url: str) -> dict:
    code, body = fetch(url)
    if code != 200 or not body:
        return {"indicator": "unknown", "description": f"status page unreachable ({code})", "code": code}
    try:
        d = json.loads(body)
        s = d.get("status", {}) or {}
        return {
            "indicator": s.get("indicator", "unknown"),
            "description": s.get("description", "?"),
            "code": code,
        }
    except json.JSONDecodeError:
        return {"indicator": "unknown", "description": "non-JSON response", "code": code}


def check_google(url: str) -> dict:
    code, body = fetch(url)
    if code != 200 or not body:
        return {"indicator": "unknown", "description": f"unreachable ({code})", "code": code}
    try:
        items = json.loads(body)
    except json.JSONDecodeError:
        return {"indicator": "unknown", "description": "non-JSON response", "code": code}

    # Treat unresolved high-severity items as outages.
    unresolved = [
        it for it in items
        if not it.get("end") and it.get("severity") in ("high", "medium")
    ]
    if not unresolved:
        return {"indicator": "none", "description": "All services normal", "code": code}
    titles = ", ".join(i.get("external_desc", i.get("public_description", "incident"))[:60] for i in unresolved[:3])
    sev = "major" if any(i.get("severity") == "high" for i in unresolved) else "minor"
    return {"indicator": sev, "description": f"{len(unresolved)} active: {titles}", "code": code}


def check_http(url: str) -> dict:
    code, _ = fetch(url)
    if code == 200:
        return {"indicator": "none", "description": "Status page reachable", "code": code}
    return {"indicator": "minor", "description": f"Status page returned {code}", "code": code}


def check_provider(name: str, kind: str, url: str) -> dict:
    if kind == "statuspage":
        out = check_statuspage(url)
    elif kind == "google":
        out = check_google(url)
    else:
        out = check_http(url)
    out["url"] = url
    out["checked_at"] = datetime.now(timezone.utc).isoformat()
    return out


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def post_discord(webhook: str, embeds: list[dict]) -> None:
    if not webhook or not embeds:
        return
    body = json.dumps({"embeds": embeds}).encode()
    req = urllib.request.Request(
        webhook, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"WARN discord post failed: {e}", file=sys.stderr)


COLOR = {"none": 0x22C55E, "minor": 0xEAB308, "major": 0xEF4444, "critical": 0x991B1B, "unknown": 0x6B7280}
EMOJI = {"none": "✅", "minor": "⚠️", "major": "🛑", "critical": "🚨", "unknown": "❓"}


def build_embed(name: str, prev: dict, curr: dict) -> dict:
    p_ind = (prev or {}).get("indicator", "unknown")
    c_ind = curr["indicator"]
    direction = "improved" if SP_RANK[c_ind] < SP_RANK[p_ind] else "degraded"
    return {
        "title": f"{EMOJI.get(c_ind, '❓')} {name} — {direction}",
        "description": f"**{p_ind}** → **{c_ind}**\n{curr.get('description','')}",
        "color": COLOR.get(c_ind, COLOR["unknown"]),
        "fields": [
            {"name": "URL", "value": curr.get("url", ""), "inline": False},
            {"name": "Checked at", "value": curr.get("checked_at", ""), "inline": True},
        ],
        "footer": {"text": "turi-status provider monitor"},
    }


def main() -> int:
    webhook = os.environ.get("NOTIFICATION_DISCORD_WEBHOOK_URL", "").strip()
    prev_state = load_state()
    next_state: dict = {}
    embeds: list[dict] = []
    summary_lines: list[str] = []

    for name, kind, url in PROVIDERS:
        curr = check_provider(name, kind, url)
        next_state[name] = curr
        prev = prev_state.get(name)
        prev_ind = (prev or {}).get("indicator", "unknown")
        curr_ind = curr["indicator"]

        summary_lines.append(f"  {EMOJI.get(curr_ind, '?')} {name:15s} {curr_ind:10s} — {curr['description'][:80]}")

        # Alert only on state changes (not on every check). First-run also alerts
        # ONLY for non-operational states so we don't spam on initial inventory.
        if prev_ind != curr_ind:
            if prev is None and curr_ind == "none":
                continue  # first-time-seeing-as-healthy is not noteworthy
            embeds.append(build_embed(name, prev or {}, curr))

    print("=== provider status snapshot ===")
    for line in summary_lines:
        print(line)

    save_state(next_state)
    if embeds:
        print(f"\n=== posting {len(embeds)} change(s) to Discord ===")
        # Discord caps 10 embeds per request
        for i in range(0, len(embeds), 10):
            post_discord(webhook, embeds[i:i + 10])
    elif not prev_state:
        print("\n(first run — established baseline, no alerts fired)")
    else:
        print("\n(no state changes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
