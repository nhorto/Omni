"""Fast public lookups: live scores from ESPN's JSON API, readable page text, RSS search.

Codex also has its own web search; these tools exist because one HTTP GET is
much faster than a search-and-read loop for the questions Nick asks most.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser

from . import tool

AGENT = "Mozilla/5.0 (X11; Linux x86_64) Omni/0.2"
LEAGUES = {
    "ncaaf": ("football/college-football", {"groups": "80"}),
    "nfl": ("football/nfl", {}),
    "nba": ("basketball/nba", {}),
    "wnba": ("basketball/wnba", {}),
    "ncaab": ("basketball/mens-college-basketball", {"groups": "50"}),
    "ncaaw": ("basketball/womens-college-basketball", {"groups": "50"}),
    "mlb": ("baseball/mlb", {}),
    "nhl": ("hockey/nhl", {}),
    "mls": ("soccer/usa.1", {}),
    "epl": ("soccer/eng.1", {}),
}


def get(url: str, timeout: float = 8, limit: int = 3_000_000, browser: bool = True) -> bytes:
    # ESPN's API CDN rejects browser-looking agents that lack a browser's other headers; plain clients pass.
    headers = {"User-Agent": AGENT, "Accept-Language": "en-US,en;q=0.8"} if browser else {"Accept": "application/json"}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(limit)


def scoreboard(league: str, date: str | None = None, team: str | None = None) -> dict:
    if league not in LEAGUES:
        raise ValueError("league must be one of " + ", ".join(LEAGUES))
    path, params = LEAGUES[league]
    params = dict(params)
    if date:
        params["dates"] = date.replace("-", "")
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?" + urllib.parse.urlencode(params)
    data = json.loads(get(url, browser=False))
    games = []
    for event in data.get("events", []):
        competition = event["competitions"][0]
        teams = {c["homeAway"]: c for c in competition["competitors"]}
        home, away = teams.get("home", {}), teams.get("away", {})
        line = {
            "away": _team(away), "home": _team(home),
            "status": competition["status"]["type"].get("shortDetail", ""),
            "state": competition["status"]["type"].get("state", ""),
            "start": event.get("date"),
            "tv": ", ".join(n for b in competition.get("broadcasts", []) for n in b.get("names", [])),
        }
        if team:
            names = " ".join(str(v) for side in (away, home) for v in (side.get("team", {}).get("displayName"),
                                                                            side.get("team", {}).get("abbreviation")))
            if team.lower() not in names.lower():
                continue
        games.append(line)
    day = (data.get("day") or {}).get("date") or date or datetime.now().strftime("%Y-%m-%d")
    return {"source": url, "league": league, "date": day, "games": games}


def _team(side: dict) -> str:
    if not side:
        return ""
    team = side.get("team", {})
    rank = (side.get("curatedRank") or {}).get("current")
    prefix = f"#{rank} " if rank and rank < 26 else ""
    score = side.get("score")
    return f"{prefix}{team.get('displayName', '?')}" + (f" {score}" if score not in (None, "") else "")


@tool("Live and final scores for a league on a date (default today), optionally one team. "
      "Leagues: ncaaf (FBS), nfl, nba, wnba, ncaab, ncaaw, mlb, nhl, mls, epl.",
      league={"type": "string", "enum": list(LEAGUES)},
      date={"type": "string", "description": "YYYY-MM-DD; omit for today"},
      team={"type": "string", "description": "Optional team name or abbreviation filter"})
def sports_scores(ctx, league: str, date: str = "", team: str = "") -> dict:
    return scoreboard(league, date or None, team or None)


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()
        if not self.skip:
            self.parts.append(data)


def readable(markup: str) -> tuple[str, str]:
    parser = _Text()
    parser.feed(markup)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return parser.title, text


@tool("Fetch a public web page and return its readable text.", url={"type": "string"},
      max_chars={"type": "integer", "minimum": 500, "maximum": 40000})
def web_fetch(ctx, url: str, max_chars: int = 12000) -> dict:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http(s) URLs")
    raw = get(url, timeout=10)
    if raw[:1] in (b"{", b"["):
        return {"url": url, "text": raw.decode("utf-8", "replace")[:max_chars]}
    title, text = readable(raw.decode("utf-8", "replace"))
    return {"url": url, "title": title, "text": text[:max_chars]}


@tool("Search the web (Bing RSS). Prefer your built-in web search when you have it; use this as a fallback.",
      query={"type": "string"})
def web_search(ctx, query: str) -> list[dict]:
    root = ET.fromstring(get("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss"})))
    results = []
    for item in root.findall("./channel/item")[:8]:
        summary = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))
        results.append({"title": html.unescape(item.findtext("title") or "").strip(), "url": (item.findtext("link") or "").strip(),
                        "published": (item.findtext("pubDate") or "").strip(), "snippet": " ".join(summary.split())[:400]})
    if not results:
        raise RuntimeError("No results")
    return results
