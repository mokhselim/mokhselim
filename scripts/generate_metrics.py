#!/usr/bin/env python3
"""
Generate profile metric cards (SVG) from the GitHub GraphQL API.

PRIVACY CONTRACT
----------------
Everything drawn comes from AGGREGATE numbers or LANGUAGE NAMES.
No repository name, description, commit message, file path, or URL is ever
read into a rendering function. Grep this file for `name` if in doubt: the only
`.name` used is `languages.edges[].node.name` (e.g. "Swift").

Requires: GITHUB_TOKEN env var (classic PAT with `repo` + `read:user`).
Stdlib only — no third-party packages.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("METRICS_TOKEN")
if not TOKEN:
    sys.exit("GITHUB_TOKEN / METRICS_TOKEN is not set")

TZ = ZoneInfo(os.environ.get("METRICS_TZ", "Africa/Cairo"))
OUT = Path(os.environ.get("METRICS_OUT", "metrics"))
HABITS_DAYS = 90            # window for weekday / hour charts
RECENT_DAYS = 30            # window for "recently used" languages
LANG_LIMIT = 8
LANG_IGNORED = {
    # generated / boilerplate that inflates mobile repos
    "C++", "C", "CMake", "Objective-C", "Objective-C++", "HTML", "CSS", "SCSS",
    "Shell", "Ruby", "Makefile", "Batchfile", "PowerShell", "Dockerfile",
    "Rich Text Format", "Starlark", "Nix",
    # vendored / compiled blobs that linguist misattributes
    "Assembly", "GLSL", "Metal", "Roff", "XSLT", "XML", "Plist", "YAML", "JSON",
}

# ---- palette (dark surface; values validated for >=3:1 on it) ----------------
SURFACE = "#161b22"
BORDER = "#30363d"
INK = "#e6edf3"
INK_2 = "#9aa4b2"
INK_3 = "#6e7681"
BLUE = "#3987e5"
SEQ = ["#21262d", "#1d3f6e", "#245a9c", "#2f78cc", "#3987e5", "#7db4f2"]  # one hue, dim->bright
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

# ---- GraphQL ------------------------------------------------------------------

def gql(query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json",
                 "User-Agent": "profile-metrics"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.load(r)
    if payload.get("errors"):
        # Some errors are partial (e.g. a single empty repo); log and continue if data exists
        print("graphql warnings:", json.dumps(payload["errors"])[:500], file=sys.stderr)
        if not payload.get("data"):
            sys.exit(1)
    return payload["data"]


VIEWER_Q = """
query($from: DateTime!, $to: DateTime!) {
  viewer {
    id login name createdAt
    followers { totalCount }
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      restrictedContributionsCount
      totalPullRequestContributions
      totalIssueContributions
      totalRepositoriesWithContributedCommits
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date weekday contributionCount } }
      }
    }
  }
}
"""

REPOS_Q = """
query($cursor: String, $since: GitTimestamp!, $author: ID!) {
  viewer {
    repositories(first: 40, after: $cursor, ownerAffiliations: [OWNER, COLLABORATOR],
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        isFork isPrivate stargazerCount pushedAt
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
        defaultBranchRef { target { ... on Commit {
          history(first: 100, since: $since, author: {id: $author}) {
            totalCount nodes { committedDate }
          }
        } } }
      }
    }
  }
}
"""


def fetch() -> dict:
    now = datetime.now(timezone.utc)
    year_ago = now - timedelta(days=365)
    v = gql(VIEWER_Q, {"from": year_ago.isoformat(), "to": now.isoformat()})["viewer"]

    since = (now - timedelta(days=HABITS_DAYS)).isoformat()
    repos, cursor = [], None
    while True:
        d = gql(REPOS_Q, {"cursor": cursor, "since": since, "author": v["id"]})["viewer"]["repositories"]
        repos += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            break
        cursor = d["pageInfo"]["endCursor"]
    return {"viewer": v, "repos": repos, "now": now}


# ---- aggregation --------------------------------------------------------------

def top_langs(counter: Counter):
    """Top languages by bytes, dropping anything under 0.1% (config/boilerplate noise)."""
    total = sum(counter.values()) or 1
    return [(n, v) for n, v in counter.most_common(LANG_LIMIT) if v / total >= 0.001]


def aggregate(raw: dict) -> dict:
    v, repos, now = raw["viewer"], raw["repos"], raw["now"]
    cc = v["contributionsCollection"]

    # calendar -> list of (date, count)
    days = [(d["date"], d["contributionCount"])
            for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    weeks = [[(d["date"], d["weekday"], d["contributionCount"]) for d in w["contributionDays"]]
             for w in cc["contributionCalendar"]["weeks"]]
    counts = dict(days)
    today = now.astimezone(TZ).date()

    def streak_from(day):
        n = 0
        while counts.get(day.isoformat(), 0) > 0:
            n += 1
            day -= timedelta(days=1)
        return n
    current = streak_from(today) or streak_from(today - timedelta(days=1))
    best = run = 0
    for _, c in days:
        run = run + 1 if c > 0 else 0
        best = max(best, run)
    busiest = max((c for _, c in days), default=0)
    active_days = sum(1 for _, c in days if c > 0)

    # languages (bytes) across non-fork repos; "recent" = repos pushed in last RECENT_DAYS
    lang_all, lang_recent, colors = Counter(), Counter(), {}
    recent_cut = now - timedelta(days=RECENT_DAYS)
    for r in repos:
        if r["isFork"]:
            continue
        pushed = datetime.fromisoformat(r["pushedAt"].replace("Z", "+00:00")) if r["pushedAt"] else None
        for e in r["languages"]["edges"]:
            n = e["node"]["name"]
            if n in LANG_IGNORED:
                continue
            colors[n] = e["node"]["color"] or INK_3
            lang_all[n] += e["size"]
            if pushed and pushed >= recent_cut:
                lang_recent[n] += e["size"]

    # habits from real commit timestamps (default branches, last HABITS_DAYS)
    by_weekday, by_hour, habit_commits = Counter(), Counter(), 0
    for r in repos:
        ref = r.get("defaultBranchRef")
        hist = ref and ref.get("target") and ref["target"].get("history")
        if not hist:
            continue
        for c in hist["nodes"]:
            t = datetime.fromisoformat(c["committedDate"].replace("Z", "+00:00")).astimezone(TZ)
            by_weekday[t.weekday()] += 1
            by_hour[t.hour] += 1
            habit_commits += 1

    return {
        "name": v["name"] or v["login"],
        "years": max(1, (now - datetime.fromisoformat(v["createdAt"].replace("Z", "+00:00"))).days // 365),
        "followers": v["followers"]["totalCount"],
        "commits_year": cc["totalCommitContributions"] + cc["restrictedContributionsCount"],
        "contributions_year": cc["contributionCalendar"]["totalContributions"],
        "prs_year": cc["totalPullRequestContributions"],
        "repos_contributed": cc["totalRepositoriesWithContributedCommits"],
        "repos_total": len(repos),
        "repos_private": sum(1 for r in repos if r["isPrivate"]),
        "stars": sum(r["stargazerCount"] for r in repos),
        "streak_current": current, "streak_best": best,
        "busiest_day": busiest, "active_days": active_days,
        "calendar": days, "weeks": weeks,
        "lang_all": top_langs(lang_all), "lang_all_total": sum(lang_all.values()),
        "lang_recent": top_langs(lang_recent), "lang_recent_total": sum(lang_recent.values()),
        "colors": colors,
        "by_weekday": by_weekday, "by_hour": by_hour, "habit_commits": habit_commits,
        "updated": now.astimezone(TZ).strftime("%d %b %Y, %H:%M %Z"),
    }


# ---- SVG helpers --------------------------------------------------------------

def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card(w: int, h: int, title: str, body: str, footer: str = "") -> str:
    foot = (f'<text x="{w-16}" y="{h-12}" text-anchor="end" font-size="10" fill="{INK_3}">{esc(footer)}</text>'
            if footer else "")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">
<style>text{{font-family:{FONT};}}</style>
<rect x="0.5" y="0.5" width="{w-1}" height="{h-1}" rx="8" fill="{SURFACE}" stroke="{BORDER}"/>
<text x="20" y="30" font-size="15" font-weight="600" fill="{INK}">{esc(title)}</text>
{body}
{foot}
</svg>'''


def fmt(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 10000 else f"{n:,}"


def overview_card(m: dict) -> str:
    W, H = 900, 250
    body = []
    body.append(f'<text x="20" y="100" font-size="58" font-weight="700" fill="{INK}" letter-spacing="-1.5">{esc(fmt(m["commits_year"]))}</text>'
                f'<text x="20" y="122" font-size="12" fill="{INK_2}">commits in the past 12 months</text>')
    weekly = [sum(c for _, _, c in w) for w in m["weeks"]]
    if weekly:
        sx0, sx1, sy0, sy1 = 300, 880, 56, 122
        mx = max(weekly) or 1
        n = len(weekly)
        pts = [(sx0 + i * (sx1 - sx0) / max(n - 1, 1), sy1 - (sy1 - sy0) * v / mx) for i, v in enumerate(weekly)]
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        body.append('<defs><linearGradient id="sp" x1="0" y1="0" x2="0" y2="1">'
                    f'<stop offset="0" stop-color="{BLUE}" stop-opacity="0.35"/><stop offset="1" stop-color="{BLUE}" stop-opacity="0"/></linearGradient></defs>'
                    f'<polygon points="{sx0:.1f},{sy1} {line} {sx1:.1f},{sy1}" fill="url(#sp)"/>'
                    f'<polyline points="{line}" fill="none" stroke="{BLUE}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        pi = weekly.index(mx)
        px, py = pts[pi]
        body.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{BLUE}" stroke="{SURFACE}" stroke-width="2"/>'
                    f'<text x="{min(max(px, sx0 + 40), sx1 - 40):.1f}" y="{py - 9:.1f}" text-anchor="middle" font-size="10" fill="{INK_2}">peak week · {mx}</text>'
                    f'<text x="{sx1}" y="{sy1 + 14}" text-anchor="end" font-size="9" fill="{INK_3}">weekly contributions · past 12 months</text>')
    body.append(f'<line x1="20" y1="150" x2="{W-20}" y2="150" stroke="{BORDER}"/>')
    tiles = [
        (fmt(m["contributions_year"]), "contributions"),
        (fmt(m["active_days"]), "active days"),
        (f'{m["streak_current"]}d', "current streak"),
        (f'{m["streak_best"]}d', "best streak"),
        (fmt(m["busiest_day"]), "busiest day"),
        (fmt(m["repos_total"]), "repositories"),
    ]
    tw = (W - 40) / len(tiles)
    for i, (value, label) in enumerate(tiles):
        x = 20 + i * tw
        if i:
            body.append(f'<line x1="{x-14:.0f}" y1="172" x2="{x-14:.0f}" y2="214" stroke="{BORDER}"/>')
        body.append(f'<text x="{x:.0f}" y="196" font-size="24" font-weight="700" fill="{INK}">{esc(value)}</text>'
                    f'<text x="{x:.0f}" y="213" font-size="10.5" fill="{INK_2}">{esc(label)}</text>')
    foot = f'{m["repos_private"]} private repos · {m["followers"]} followers · {m["years"]} yrs on GitHub · includes private work · counts only'
    return card(W, H, "Activity overview", "".join(body), foot)


PLATFORMS = [  # fixed order, fixed hue — never cycled
    ("Native iOS", {"Swift", "Objective-C"}, "#F05138"),
    ("Flutter", {"Dart"}, "#00B4AB"),
    ("Web", {"Astro", "JavaScript", "TypeScript", "Vue", "Svelte", "HTML", "CSS", "MDX"}, "#f1e05a"),
    ("Backend", {"Python", "Go", "Rust", "Java", "Kotlin", "PHP"}, "#3572A5"),
]


def platform_split(lang_counter) -> list[tuple[str, int, str]]:
    out = []
    seen = set()
    for label, langs, color in PLATFORMS:
        v = sum(n for l, n in lang_counter if l in langs)
        seen |= langs
        if v:
            out.append((label, v, color))
    other = sum(n for l, n in lang_counter if l not in seen)
    if other:
        out.append(("Other", other, INK_3))
    total = sum(v for _, v, _ in out) or 1
    return [p for p in out if p[1] / total >= 0.005]   # drop slivers that would round to 0%


def donut(cx: float, cy: float, r: float, thick: float, parts, total: int) -> str:
    import math
    out = []
    start = -math.pi / 2
    gap = 0.035 if len(parts) > 1 else 0            # 2px-ish surface gap between slices
    for _, v, color in parts:
        frac = v / total
        if frac >= 0.999:
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="{thick}"/>')
            break
        a0, a1 = start + gap / 2, start + frac * 2 * math.pi - gap / 2
        if a1 > a0:
            x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            large = 1 if (a1 - a0) > math.pi else 0
            out.append(f'<path d="M{x0:.2f},{y0:.2f} A{r},{r} 0 {large} 1 {x1:.2f},{y1:.2f}" fill="none" stroke="{color}" stroke-width="{thick}" stroke-linecap="butt"/>')
        start += frac * 2 * math.pi
    return "".join(out)


def languages_card(m: dict) -> str:
    W, H = 900, 288
    body = []

    def column(x0: int, w: int, label: str, items, total: int, rows: int):
        body.append(f'<text x="{x0}" y="58" font-size="11" fill="{INK_2}">{esc(label)}</text>')
        if not items or not total:
            body.append(f'<text x="{x0}" y="82" font-size="11" fill="{INK_3}">No data yet</text>')
            return
        for i, (name, size) in enumerate(items[:rows]):
            y = 82 + i * 30
            pct = 100 * size / total
            color = m["colors"].get(name, INK_3)
            body.append(f'<text x="{x0}" y="{y}" font-size="11.5" fill="{INK}">{esc(name)}</text>'
                        f'<text x="{x0+w}" y="{y}" font-size="11.5" text-anchor="end" fill="{INK_2}">{pct:.1f}%</text>'
                        f'<rect x="{x0}" y="{y+7}" width="{w}" height="6" rx="3" fill="{BORDER}"/>'
                        f'<rect x="{x0}" y="{y+7}" width="{max(6, w * pct / 100):.1f}" height="6" rx="3" fill="{color}"/>')

    col_w = 250
    column(20, col_w, "Most used · all repositories", m["lang_all"], m["lang_all_total"], 6)
    body.append(f'<line x1="300" y1="52" x2="300" y2="{H-36}" stroke="{BORDER}"/>')
    column(320, col_w, f"Recently used · last {RECENT_DAYS} days", m["lang_recent"], m["lang_recent_total"], 6)
    body.append(f'<line x1="600" y1="52" x2="600" y2="{H-36}" stroke="{BORDER}"/>')

    # platform donut
    body.append(f'<text x="620" y="58" font-size="11" fill="{INK_2}">What I build · by platform</text>')
    parts = platform_split(m["lang_all"])
    total = sum(v for _, v, _ in parts)
    if parts and total:
        cx, cy, r = 686, 160, 50
        body.append(donut(cx, cy, r, 16, parts, total))
        lead = max(parts, key=lambda p: p[1])
        body.append(f'<text x="{cx}" y="{cy-2}" text-anchor="middle" font-size="18" font-weight="700" fill="{INK}">{100*lead[1]/total:.0f}%</text>'
                    f'<text x="{cx}" y="{cy+13}" text-anchor="middle" font-size="9" fill="{INK_2}">{esc(lead[0])}</text>')
        for i, (label, v, color) in enumerate(parts[:5]):
            ly = 96 + i * 26
            body.append(f'<circle cx="{762}" cy="{ly-4}" r="4" fill="{color}"/>'
                        f'<text x="{774}" y="{ly}" font-size="11.5" fill="{INK}">{esc(label)}</text>'
                        f'<text x="{W-20}" y="{ly}" font-size="11.5" text-anchor="end" fill="{INK_2}">{100*v/total:.0f}%</text>')
    return card(W, H, "Languages & platforms", "".join(body), "share of bytes across all repos · generated boilerplate excluded")


def calendar_card(m: dict) -> str:
    weeks = m["weeks"]
    cell, gap = 13, 3
    step = cell + gap
    W = 40 + len(weeks) * step + 20
    H = 62 + 7 * step + 32
    mx = max((c for w in weeks for _, _, c in w), default=1) or 1
    body = []
    last_month = None
    for wi, week in enumerate(weeks):
        x = 40 + wi * step
        month = datetime.fromisoformat(week[0][0]).month
        if month != last_month:
            if last_month is not None or wi == 0:
                body.append(f'<text x="{x}" y="52" font-size="10" fill="{INK_3}">{datetime.fromisoformat(week[0][0]).strftime("%b")}</text>')
            last_month = month
        for date, wd, c in week:
            y = 60 + wd * step                       # wd: 0 = Sunday, as GitHub reports it
            lvl = 0 if c == 0 else 1 + min(4, int(4 * c / mx))
            body.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3" fill="{SEQ[lvl]}"><title>{esc(date)}: {c}</title></rect>')
    for lbl, row in (("Mon", 1), ("Wed", 3), ("Fri", 5)):
        body.append(f'<text x="18" y="{60 + row*step + 10}" font-size="9" fill="{INK_3}">{lbl}</text>')
    lx = W - 20 - 28 - len(SEQ) * step
    ly = H - 24
    body.append(f'<text x="{lx-6}" y="{ly+10}" text-anchor="end" font-size="10" fill="{INK_3}">Less</text>')
    for i, col in enumerate(SEQ):
        body.append(f'<rect x="{lx + i*step}" y="{ly}" width="{cell}" height="{cell}" rx="3" fill="{col}"/>')
    body.append(f'<text x="{lx + len(SEQ)*step + 2}" y="{ly+10}" font-size="10" fill="{INK_3}">More</text>')
    return card(W, H, f'{fmt(m["contributions_year"])} contributions in the last year', "".join(body))


def habits_card(m: dict) -> str:
    W, H = 900, 210
    body = []

    def columns(x0: int, y0: int, w: int, h: int, label: str, keys, values: Counter, names):
        body.append(f'<text x="{x0}" y="{y0}" font-size="11" fill="{INK_2}">{esc(label)}</text>')
        mx = max((values.get(k, 0) for k in keys), default=0) or 1
        slot = w / len(keys)
        bw = min(24, slot - 2)                      # thin marks, 2px surface gap
        base = y0 + 12 + h
        body.append(f'<line x1="{x0}" y1="{base}" x2="{x0+w}" y2="{base}" stroke="{BORDER}"/>')
        for i, k in enumerate(keys):
            v = values.get(k, 0)
            bh = (h - 18) * v / mx                 # headroom for the peak label
            x = x0 + i * slot + (slot - bw) / 2
            if v:
                body.append(f'<path d="M{x:.1f},{base} v{-max(bh-4,0):.1f} a4,4 0 0 1 4,-4 h{bw-8:.1f} a4,4 0 0 1 4,4 v{max(bh-4,0):.1f} z" fill="{BLUE}"/>')
                if v == mx:                          # selective label: only the peak
                    body.append(f'<text x="{x+bw/2:.1f}" y="{base-bh-5:.1f}" text-anchor="middle" font-size="10" fill="{INK_2}">{v}</text>')
            if names[i]:
                body.append(f'<text x="{x+bw/2:.1f}" y="{base+13}" text-anchor="middle" font-size="9" fill="{INK_3}">{names[i]}</text>')

    if m["habit_commits"]:
        columns(20, 58, 240, 100, "Commits by weekday", list(range(7)), m["by_weekday"],
                ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        columns(300, 58, 580, 100, "Commits by hour of day", list(range(24)), m["by_hour"],
                [f"{h:02d}" if h % 3 == 0 else "" for h in range(24)])
        wd = max(m["by_weekday"], key=m["by_weekday"].get)
        hr = max(m["by_hour"], key=m["by_hour"].get)
        period = "morning" if 5 <= hr < 12 else "afternoon" if 12 <= hr < 18 else "evening" if 18 <= hr < 23 else "night"
        foot = f'{m["habit_commits"]} commits in the last {HABITS_DAYS} days · most active on {["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][wd]}s, in the {period}'
    else:
        body.append(f'<text x="20" y="80" font-size="11" fill="{INK_3}">No commits found in the last {HABITS_DAYS} days</text>')
        foot = ""
    return card(W, H, "Coding habits", "".join(body), foot)


def studio_card() -> str:
    cfg_path = Path("studio.json")
    if not cfg_path.exists():
        return ""
    cfg = json.loads(cfg_path.read_text())
    W, H = 900, 156
    body = []
    arr = cfg.get("arr_usd")
    if arr:
        arr_txt = f"${arr/1000:.0f}K" if arr < 1_000_000 else f"${arr/1_000_000:.1f}M"
        body.append(f'<text x="20" y="96" font-size="58" font-weight="700" fill="{INK}" letter-spacing="-1.5">{esc(arr_txt)}</text>'
                    f'<text x="20" y="118" font-size="12" fill="{INK_2}">annual recurring revenue · portfolio of consumer apps</text>')
    # (value, label, sub, column width)
    tiles = [("100%", "solo-built", "design · code · backend · growth", 200)]
    if cfg.get("apps_live"):
        tiles.append((str(cfg["apps_live"]), "apps live", "App Store & Google Play", 150))
    if cfg.get("platforms"):
        tiles.append((esc(cfg["platforms"]), "platforms", "native iOS · Flutter · Astro", 230))
    if cfg.get("founded_year"):
        yrs = datetime.now().year - int(cfg["founded_year"])
        tiles.append((f"{yrs}+ yrs", "shipping indie apps", f"since {cfg['founded_year']}", 140))
    x = 330
    for i, (value, label, sub, cw) in enumerate(tiles):
        if i:
            body.append(f'<line x1="{x-16}" y1="62" x2="{x-16}" y2="122" stroke="{BORDER}"/>')
        body.append(f'<text x="{x}" y="88" font-size="22" font-weight="700" fill="{INK}">{value}</text>'
                    f'<text x="{x}" y="106" font-size="11" fill="{INK_2}">{esc(label)}</text>'
                    f'<text x="{x}" y="120" font-size="9.5" fill="{INK_3}">{esc(sub)}</text>')
        x += cw
    return card(W, H, "The studio", "".join(body), "self-reported · app names stay private until launch")


def main():
    raw = fetch()
    m = aggregate(raw)
    OUT.mkdir(parents=True, exist_ok=True)
    for fn, svg in (("overview.svg", overview_card(m)), ("languages.svg", languages_card(m)),
                    ("calendar.svg", calendar_card(m)), ("habits.svg", habits_card(m)),
                    ("studio.svg", studio_card())):
        if svg:
            (OUT / fn).write_text(svg, encoding="utf-8")
    # Log only aggregate numbers — never repo names
    print(json.dumps({k: v for k, v in m.items() if k not in ("calendar", "colors", "by_weekday", "by_hour")},
                     default=str, indent=1))


if __name__ == "__main__":
    main()
