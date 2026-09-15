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
        "lang_all": lang_all.most_common(LANG_LIMIT), "lang_all_total": sum(lang_all.values()),
        "lang_recent": lang_recent.most_common(LANG_LIMIT), "lang_recent_total": sum(lang_recent.values()),
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
    tiles = [
        ("Commits", fmt(m["commits_year"]), "past 12 months"),
        ("Contributions", fmt(m["contributions_year"]), "all types"),
        ("Repositories", fmt(m["repos_total"]), f'{m["repos_private"]} private'),
        ("Active days", fmt(m["active_days"]), "past 12 months"),
        ("Current streak", f'{m["streak_current"]}d', "consecutive days"),
        ("Best streak", f'{m["streak_best"]}d', "past 12 months"),
        ("Busiest day", fmt(m["busiest_day"]), "contributions"),
        ("Followers", fmt(m["followers"]), f'{m["years"]} yrs on GitHub'),
    ]
    W, H, cols = 440, 232, 4
    tw = (W - 40) / cols
    body = []
    for i, (label, value, sub) in enumerate(tiles):
        x = 20 + (i % cols) * tw
        y = 62 + (i // cols) * 82
        body.append(f'<text x="{x:.0f}" y="{y}" font-size="11" fill="{INK_2}">{esc(label)}</text>'
                    f'<text x="{x:.0f}" y="{y+26}" font-size="24" font-weight="700" fill="{INK}">{esc(value)}</text>'
                    f'<text x="{x:.0f}" y="{y+42}" font-size="10" fill="{INK_3}">{esc(sub)}</text>')
    return card(W, H, "Activity overview", "".join(body), "includes private repos · counts only")


def languages_card(m: dict) -> str:
    W, H = 440, 256
    body = []

    def section(y0: int, label: str, items, total: int) -> int:
        body.append(f'<text x="20" y="{y0}" font-size="11" fill="{INK_2}">{esc(label)}</text>')
        if not items or not total:
            body.append(f'<text x="20" y="{y0+22}" font-size="11" fill="{INK_3}">No data yet</text>')
            return y0 + 40
        # stacked bar with 2px surface gaps, rounded ends via clip
        bx, bw, by, bh = 20, W - 40, y0 + 10, 8
        body.append(f'<clipPath id="c{y0}"><rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="4"/></clipPath>'
                    f'<g clip-path="url(#c{y0})">')
        x = bx
        shown = items[:6]
        for name, size in shown:
            seg = bw * size / total
            body.append(f'<rect x="{x:.1f}" y="{by}" width="{max(seg-2,0):.1f}" height="{bh}" fill="{m["colors"].get(name, INK_3)}"/>')
            x += seg
        if x < bx + bw - 2:   # remainder = languages outside the top list
            body.append(f'<rect x="{x:.1f}" y="{by}" width="{bx+bw-x:.1f}" height="{bh}" fill="{BORDER}"/>')
        body.append("</g>")
        # legend: dot + name + % (text in ink tokens, never series color)
        col_w = (W - 40) / 2
        for i, (name, size) in enumerate(shown):
            lx = 20 + (i % 2) * col_w
            ly = by + 26 + (i // 2) * 16
            pct = 100 * size / total
            body.append(f'<circle cx="{lx+4}" cy="{ly-4}" r="4" fill="{m["colors"].get(name, INK_3)}"/>'
                        f'<text x="{lx+14}" y="{ly}" font-size="11" fill="{INK}">{esc(name)}</text>'
                        f'<text x="{lx+col_w-8}" y="{ly}" font-size="11" text-anchor="end" fill="{INK_2}">{pct:.1f}%</text>')
        return by + 26 + ((len(shown) + 1) // 2) * 16 + 6

    y = section(58, "Most used · all repositories", m["lang_all"], m["lang_all_total"])
    section(y + 10, f"Recently used · repos touched in the last {RECENT_DAYS} days", m["lang_recent"][:4], m["lang_recent_total"])
    return card(W, H, "Languages", "".join(body), "by bytes · generated boilerplate excluded")


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
        foot = f'{m["habit_commits"]} commits in the last {HABITS_DAYS} days · most active on {["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][wd]}s, in the {period}'
    else:
        body.append(f'<text x="20" y="80" font-size="11" fill="{INK_3}">No commits found in the last {HABITS_DAYS} days</text>')
        foot = ""
    return card(W, H, "Coding habits", "".join(body), foot)


def main():
    raw = fetch()
    m = aggregate(raw)
    OUT.mkdir(parents=True, exist_ok=True)
    for fn, svg in (("overview.svg", overview_card(m)), ("languages.svg", languages_card(m)),
                    ("calendar.svg", calendar_card(m)), ("habits.svg", habits_card(m))):
        (OUT / fn).write_text(svg, encoding="utf-8")
    # Log only aggregate numbers — never repo names
    print(json.dumps({k: v for k, v in m.items() if k not in ("calendar", "colors", "by_weekday", "by_hour")},
                     default=str, indent=1))


if __name__ == "__main__":
    main()
