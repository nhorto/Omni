"""Background public web research, separate from Omi's visible browser session."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCOREBOARD = "https://www.espn.com/college-football/scoreboard"


def _score_question(query: str) -> bool:
    return bool(re.search(r"\b(college football|ncaaf|fbs)\b", query, re.I)
                and re.search(r"\b(scores?|results?)\b", query, re.I))


def _scoreboard_snapshot() -> str:
    executable = shutil.which("playwright")
    if not executable or not Path("/usr/bin/chromium").is_file():
        raise RuntimeError("Background browser is unavailable")
    session = "omi-research-" + uuid.uuid4().hex[:12]
    env = dict(os.environ, PLAYWRIGHT_MCP_EXECUTABLE_PATH="/usr/bin/chromium")
    command = [executable, "cli", f"-s={session}"]
    try:
        opened = subprocess.run(command + ["open", SCOREBOARD, "--raw"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=30)
        if opened.returncode:
            raise RuntimeError((opened.stderr or opened.stdout)[-500:] or "Scoreboard did not open")
        page = subprocess.run(command + ["eval", "() => document.querySelector('main')?.innerText || ''", "--raw"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=25)
        if page.returncode:
            raise RuntimeError((page.stderr or page.stdout)[-500:] or "Scoreboard could not be read")
        content = json.loads(page.stdout)
        if not isinstance(content, str) or "College Football Scoreboard" not in content:
            raise RuntimeError("Could not verify the scoreboard content")
        content = content.split("Sponsored Headlines", 1)[0][:16000]
        today = datetime.now().astimezone().strftime("%A, %B %-d, %Y")
        if today not in content:
            return ("Source: " + SCOREBOARD + "\nFetched: " + datetime.now(timezone.utc).isoformat()
                    + "\nThe scoreboard did not list games under today's local date (" + today + ").")
        content = content.split(today, 1)[1]
        content = re.split(r"\n(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), [A-Z][a-z]+ \d{1,2}, \d{4}\n", content, maxsplit=1)[0]
        return ("Source: " + SCOREBOARD + "\nFetched: " + datetime.now(timezone.utc).isoformat()
                + "\nGames for " + today + ":\n" + content)
    finally:
        try:
            subprocess.run(command + ["close", "--raw"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _rss_search(query: str) -> str:
    address = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss"})
    request = urllib.request.Request(address, headers={"User-Agent": "Mozilla/5.0 (Omi background research)"})
    with urllib.request.urlopen(request, timeout=12) as response:
        data = response.read(300_000)
    root = ET.fromstring(data)
    items = root.findall("./channel/item")[:6]
    if not items:
        raise RuntimeError("Web search returned no results")
    lines = ["Search results for: " + query]
    for item in items:
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        summary = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))
        date = (item.findtext("pubDate") or "").strip()
        if urllib.parse.urlparse(link).scheme != "https":
            continue
        lines.append(f"- {title}\n  URL: {link}\n  Published: {date or 'unknown'}\n  Snippet: {' '.join(summary.split())[:450]}")
    if len(lines) == 1:
        raise RuntimeError("Web search returned no usable public results")
    return "\n".join(lines)


def search(query: str) -> str:
    query = query.strip()
    if not 3 <= len(query) <= 300:
        raise ValueError("Research query must contain 3–300 characters")
    if _score_question(query):
        try:
            return _scoreboard_snapshot()
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            # Search snippets provide a source link even when the scoreboard is unavailable.
            return _rss_search(query)
    return _rss_search(query)
