#!/usr/bin/env python3
"""TPP policy scanner.

Pipeline: read Scan Settings tab -> Tavily search -> Claude extract
          -> dedupe vs vault -> verify vs Utah law -> build rows -> rank
          -> write a dated results tab in the Policy Vault.

Results land in a new tab named like "Scan — Obj 3 — Jul 13 2026", with a
SCAN DETAILS block under the rows (settings used, funnel counts, and the
candidates killed because they're already Utah law).

What gets scanned is controlled, in increasing precedence:
  1. defaults in DEFAULT_SETTINGS
  2. the "Scan Settings" tab of the Policy Vault (Marin edits this)
  3. SCAN_OBJECTIVES / SCAN_TIERS / SCAN_TOPICS env vars
     (set by the GitHub "Run workflow" button's input boxes)
  4. command-line flags (local runs)

Run locally:  TAVILY_API_KEY=... ANTHROPIC_API_KEY=... python scan.py --dry-run
Focused run:  python scan.py --objectives "3" --topics "school lunch"
In CI:        .github/workflows/monthly-scan.yml (monthly cron + manual button)
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml
from anthropic import Anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build

import prompts

HERE = Path(__file__).parent
MODEL_FAST = "claude-haiku-4-5-20251001"   # extraction, verification
MODEL_SMART = "claude-sonnet-5"            # dedupe, row-building, ranking

VAULT_ID = os.environ.get("VAULT_SPREADSHEET_ID", "")
SETTINGS_TAB = "Scan Settings"

VAULT_HEADERS = [
    "ID #", "Objective", "Target / Barrier", "Idea Summary (1-2 Sentences)",
    "Policy Pitch?", "Idea Source (Link)", "Page #/Reference", "Utah Code (Link)",
    "Past Attempts", "More Info/Notes", "Type(s) of Change", "Type(s) of Cost",
    "Cost notes", "Team Member(s)", "Reviewed", "Review Date", "Policy Brief(s)",
    "Estimated Cost", "Cost Explanation", "# People Affected",
    "Impact / Population Explanation", "Other follow up?", "Notes / Reactions",
    "Answers/Updates", "Analyst Favs", "Emily Favs", "Brooke Favs", "Gabby Favs",
    "Stand alone?", "Write a 2-pager?",
]

# Utah stats bundled with the repo so the scanner never invents baselines.
STATS_FILE = HERE / "utah_statistics.csv"

DEFAULT_SETTINGS = {
    "objectives": None,   # None = all 7; otherwise a list like [3] or [1, 3, 6]
    "tiers": None,        # None = all source tiers; otherwise e.g. [1] or [1, 2]
    "topics": [],         # extra search topics added to the run
    "days": 60,           # how far back to search
    "max_ideas": 25,      # cap on rows staged; None = no cap
    "label": "",          # results-tab name prefix, e.g. "1-yr lookback"
    "show_killed": True,  # list already-law candidates in SCAN DETAILS
    "mode": "topic",      # "topic" = topic queries per tier (monthly);
                          # "annual" = every annual_queries line per source
    "short_rows": False,  # True = skip cost/impact estimates (annual scan)
}

NOT_ESTIMATED = "Not estimated (annual scan) — fill in if this idea is picked"

WORKERS = 4  # parallel Claude/Tavily calls in verify + row-building
BATCH = 25   # items per Claude call in extraction and dedupe

claude = Anthropic()  # reads ANTHROPIC_API_KEY


def log(msg: str) -> None:
    print(f"[scan] {msg}", flush=True)


def pmap(fn, items):
    """Run fn over items with a few parallel workers; keeps input order."""
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(fn, items))


def parse_max(val: str):
    """'25' -> 25; 'all', 'no cap' or '0' -> None; junk -> leave unchanged (returns ...)."""
    if re.search(r"\ball\b|no cap|none", val.lower()):
        return None
    m = re.search(r"\d+", val)
    if not m:
        return ...
    return int(m.group()) or None  # 0 also means no cap


def ask_claude(model: str, prompt: str, max_tokens: int = 8000) -> str:
    resp = claude.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    # Models that emit a thinking block put it first, so take the text block,
    # not content[0] (which may be a ThinkingBlock with no .text).
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def ask_claude_json(model: str, prompt: str, max_tokens: int = 8000):
    """Ask for JSON and parse it, tolerating markdown fences."""
    text = ask_claude(model, prompt + "\n\nRespond with ONLY valid JSON.", max_tokens)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    return json.loads(text)


# ---------------------------------------------------------------- settings

def parse_nums(text: str, hi: int):
    """'3' -> [3]; '1, 3' -> [1, 3]; '1-2' -> [1, 2]; 'All' or junk -> None (= all)."""
    text = str(text)
    if "all" in text.lower():
        return None
    nums = set()
    for a, b in re.findall(r"(\d)\s*[-–]\s*(\d)", text):
        nums.update(range(int(a), int(b) + 1))
    for ch in re.sub(r"\d\s*[-–]\s*\d", "", text):
        if ch.isdigit():
            nums.add(int(ch))
    nums = sorted(n for n in nums if 1 <= n <= hi)
    return nums or None


def parse_topics(text: str) -> list[str]:
    return [t.strip() for t in str(text).split(",") if t.strip()]


def read_scan_settings(svc) -> dict:
    """Read the 'Scan Settings' tab. A missing tab or an unparseable value
    falls back to the default — a bad setting can never break a scan."""
    s = dict(DEFAULT_SETTINGS)
    try:
        vals = svc.values().get(
            spreadsheetId=VAULT_ID, range=f"'{SETTINGS_TAB}'!A1:B30").execute()
    except Exception as e:
        log(f"WARN could not read '{SETTINGS_TAB}' tab ({e}) — using defaults")
        return s
    for row in vals.get("values", []):
        if len(row) < 2 or not str(row[1]).strip():
            continue
        key, val = str(row[0]).strip().lower(), str(row[1]).strip()
        if key.startswith("objective"):
            s["objectives"] = parse_nums(val, 7)
        elif key.startswith("source tier"):
            s["tiers"] = parse_nums(val, 3)
        elif key.startswith("extra"):
            s["topics"] = parse_topics(val)
        elif key.startswith("search window"):
            if m := re.search(r"\d+", val):
                s["days"] = int(m.group())
        elif key.startswith("max"):
            if (v := parse_max(val)) is not ...:
                s["max_ideas"] = v
    return s


def scan_label(settings) -> str:
    label = ("All objectives" if settings["objectives"] is None
             else "Obj " + ",".join(map(str, settings["objectives"])))
    if settings["topics"]:
        label += " + topics"
    if settings["label"]:
        label = f"{settings['label']} — {label}"
    return label


def settings_summary(s) -> str:
    return (f"objectives={'all' if s['objectives'] is None else s['objectives']}, "
            f"tiers={'all' if s['tiers'] is None else s['tiers']}, "
            f"window={s['days']}d, max={s['max_ideas'] or 'no cap'}"
            + (", mode=annual (every source separately)" if s["mode"] == "annual" else "")
            + (", short rows" if s["short_rows"] else "")
            + (f", topics={s['topics']}" if s["topics"] else ""))


def selected_objectives(cfg, objs) -> list[str]:
    if objs is None:
        return cfg["objectives"]
    return [o for o in cfg["objectives"] if int(o.split(".")[0]) in objs]


# ---------------------------------------------------------------- Tavily

def tavily_search(query: str, domains=None, days=60, max_results: int = 5):
    """days=None -> no date filter. Tavily's `days` only applies to news
    searches, so the window is sent as an absolute start_date instead."""
    date_filter = ({} if days is None else
                   {"start_date": (dt.date.today() - dt.timedelta(days=days)).isoformat()})
    r = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": os.environ["TAVILY_API_KEY"],
            "query": query,
            "max_results": max_results,
            **date_filter,
            **({"include_domains": domains} if domains else {}),
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("results", [])


# ---------------------------------------------------------------- Google Sheets

def sheets_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds).spreadsheets()


def read_existing_summaries(svc) -> list[str]:
    """Idea summaries from the Policies tab plus every prior scan/staging tab."""
    meta = svc.get(spreadsheetId=VAULT_ID).execute()
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    summaries = []
    for title in titles:
        if title != "Policies" and not title.startswith(("Staging", "Scan —")):
            continue
        vals = svc.values().get(
            spreadsheetId=VAULT_ID, range=f"'{title}'!D2:D").execute()
        summaries += [row[0] for row in vals.get("values", []) if row and row[0]]
    log(f"loaded {len(summaries)} existing vault/staging summaries")
    return summaries


def write_results_tab(svc, rows, settings, killed, funnel: str) -> str:
    today = dt.date.today()
    base = f"Scan — {scan_label(settings)} — {today.strftime('%b %d %Y')}"
    meta = svc.get(spreadsheetId=VAULT_ID).execute()
    taken = {s["properties"]["title"] for s in meta["sheets"]}
    title, n = base, 2
    while title in taken:
        title, n = f"{base} ({n})", n + 1

    resp = svc.batchUpdate(
        spreadsheetId=VAULT_ID,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    details = [
        [""],
        ["SCAN DETAILS"],
        ["Date", today.isoformat()],
        ["Scan type", scan_label(settings)],
        ["Settings used", settings_summary(settings)],
        ["Funnel", funnel],
    ]
    if killed and settings["show_killed"]:
        details.append(
            ["Killed candidates (already Utah law or dead) — kept so we don't re-chase them:"])
        details += [[f"• {k['idea']}", k["verification"]["reasoning"]] for k in killed]

    svc.values().update(
        spreadsheetId=VAULT_ID, range=f"'{title}'!A1",
        valueInputOption="RAW",
        body={"values": [VAULT_HEADERS] + rows + details},
    ).execute()
    url = f"https://docs.google.com/spreadsheets/d/{VAULT_ID}/edit?gid={gid}#gid={gid}"
    log(f"wrote {len(rows)} rows to '{title}' -> {url}")
    return url


# ---------------------------------------------------------------- pipeline stages

def search_plan(cfg, settings) -> list[tuple[str, list[str]]]:
    """The (query, domains) pairs to search. Monthly/topic mode: each topic
    query once per source tier. Annual mode: every annual_queries line on
    every source separately, so no site competes with another for results."""
    tiers = settings["tiers"]
    srcs = [src for src in cfg["sources"] if tiers is None or src["tier"] in tiers]
    if settings["mode"] == "annual":
        return [(f"{src['hint']} {q}" if src.get("hint") else q, [src["domain"]])
                for src in srcs if src.get("annual", True)
                for q in cfg["annual_queries"] + [f"Utah {t}" for t in settings["topics"]]]
    groups = {}  # one search per tier, so each tier gets its own 20 results
    for src in srcs:
        groups.setdefault(src["tier"], []).append(src["domain"])
    objs = settings["objectives"]
    queries = [q["q"] for q in cfg["queries"]
               if objs is None or not q.get("objectives")
               or set(q["objectives"]) & set(objs)]
    queries += [f"Utah policy {t}" for t in settings["topics"]]
    return [(q, doms) for q in queries for doms in groups.values()]


def gather_candidates(cfg, settings) -> list[dict]:
    objs = settings["objectives"]
    plan = search_plan(cfg, settings)
    hits, seen = [], set()
    for q, doms in plan:
        try:
            for h in tavily_search(q, domains=doms, days=settings["days"],
                                   max_results=20):
                if h.get("url") not in seen:
                    seen.add(h.get("url"))
                    hits.append(h)
        except Exception as e:  # one failed query shouldn't kill the run
            log(f"WARN query failed ({q} @ {doms[0] if len(doms) == 1 else 'tier'}): {e}")
    log(f"{len(hits)} unique search results from {len(plan)} searches")
    if not hits:
        return []

    objectives = "\n".join(selected_objectives(cfg, objs))
    ideas = []
    for i in range(0, len(hits), BATCH):
        blob = "\n\n".join(
            f"[{h.get('url')}] {h.get('title')}\n{h.get('content', '')[:1200]}"
            for h in hits[i:i + BATCH])
        try:
            got = ask_claude_json(MODEL_FAST, prompts.EXTRACT.format(
                objectives=objectives, results=blob))
            ideas += [g for g in got if isinstance(g, dict) and g.get("idea")]
        except Exception as e:  # a bad batch loses its ideas, not the run
            log(f"WARN extraction batch {i // BATCH + 1} failed: {e}")
    log(f"{len(ideas)} candidate ideas extracted")
    return ideas


def dedupe(candidates, existing) -> list[dict]:
    """Drop candidates already in the Vault or a prior scan tab. Runs in
    batches; ideas kept from earlier batches count as existing for later ones,
    so the same idea found in two sources is only kept once."""
    keep, existing = [], list(existing)
    for i in range(0, len(candidates), BATCH):
        batch = candidates[i:i + BATCH]
        try:
            result = ask_claude_json(MODEL_SMART, prompts.DEDUPE.format(
                candidates=json.dumps(batch, indent=1),
                existing="\n".join(f"- {s[:300]}" for s in existing)))
            by_idea = {r["idea"]: r for r in result if isinstance(r, dict)}
            kept = [c for c in batch
                    if not by_idea.get(c["idea"], {}).get("duplicate", False)]
        except Exception as e:  # keep the batch, but say it wasn't checked
            log(f"WARN dedupe batch {i // BATCH + 1} failed: {e}")
            kept = batch
            for c in kept:
                c["idea"] += "  [FLAG: duplicate check failed — may already be in the Vault]"
        keep += kept
        existing += [c["idea"] for c in kept]
    log(f"dedupe: {len(candidates)} -> {len(keep)}")
    return keep


def verify_one(c) -> dict:
    try:
        results = tavily_search(f"Utah law status: {c['idea'][:150]}",
                                days=None, max_results=5)
        blob = "\n\n".join(
            f"[{r.get('url')}] {r.get('title')}\n{r.get('content', '')[:800]}"
            for r in results)
        v = ask_claude_json(MODEL_FAST, prompts.VERIFY.format(
            idea=c["idea"], results=blob))
    except Exception as e:
        log(f"WARN verification errored, treating as uncertain: {e}")
        v = {"status": "uncertain", "reasoning": f"verification error: {e}",
             "reframed_idea": c["idea"], "utah_law_cite": ""}
    c["verification"] = v
    return c


def verify(candidates) -> tuple[list[dict], list[dict]]:
    live, dead = [], []
    for c in pmap(verify_one, candidates):
        v = c["verification"]
        if v.get("status") == "dead":
            dead.append(c)
        else:
            c["idea"] = v.get("reframed_idea") or c["idea"]
            if v.get("status") != "live":
                c["idea"] += "  [FLAG: could not fully verify Utah status — check before use]"
            live.append(c)
    log(f"verify: {len(live)} live, {len(dead)} already-law/dead")
    return live, dead


def build_rows(candidates, cfg) -> list[dict]:
    # errors="replace" so a stray non-UTF-8 byte in the CSV can't crash a scan
    stats = (STATS_FILE.read_text(encoding="utf-8", errors="replace")
             if STATS_FILE.exists() else "(stats file missing)")

    def one(c):
        try:
            return ask_claude_json(MODEL_SMART, prompts.BUILD_ROW.format(
                idea=c["idea"], objective=c.get("objective", ""),
                source_url=c.get("source_url", ""),
                verification=json.dumps(c.get("verification", {})),
                stats=stats[:8000], barriers="(use vault dropdown values)"))
        except Exception as e:
            log(f"WARN row build failed, skipping: {e}")
            return None

    rows = [r for r in pmap(one, candidates) if isinstance(r, dict)]
    log(f"built {len(rows)} rows from {len(candidates)} verified ideas")
    return rows


def short_rows(candidates) -> list[dict]:
    """Annual scan: a row from what extraction and the Utah-law check already
    found, with no extra Claude call. Cost/impact left for picked ideas."""
    rows = []
    for c in candidates:
        v = c.get("verification", {})
        rows.append({
            "objective": c.get("objective", ""),
            "idea_summary": c["idea"],
            "idea_source": " — ".join(x for x in (c.get("source_label", ""),
                                                  c.get("source_url", "")) if x),
            "utah_code": v.get("utah_law_cite", ""),
            "more_info": f"Utah-law check: {v.get('reasoning', '')}",
            "estimated_cost": NOT_ESTIMATED,
            "people_affected": NOT_ESTIMATED,
        })
    return rows


def rank(rows) -> list[dict]:
    if not rows:
        return rows
    numbered = "\n".join(f"{i}. {r.get('idea_summary', '')}"
                         for i, r in enumerate(rows, 1))
    try:
        ranking = ask_claude_json(MODEL_SMART, prompts.RANK.format(ideas=numbered),
                                  max_tokens=32000)
    except Exception as e:  # never lose verified ideas to a ranking glitch
        log(f"WARN ranking failed, writing rows unranked: {e}")
        ranking = []
    # Sonnet sometimes wraps the list in a dict like {"rankings": [...]};
    # unwrap to the first list, and tolerate odd shapes so a ranking glitch
    # never discards verified ideas — they just write unranked.
    if isinstance(ranking, dict):
        lists = [v for v in ranking.values() if isinstance(v, list)]
        ranking = lists[0] if lists else []
    order = {}
    for r in ranking:
        if isinstance(r, dict) and str(r.get("id", "")).isdigit():
            order[int(r["id"])] = (r.get("rank", 999), r.get("rationale", ""))
    for i, row in enumerate(rows, 1):
        rk, why = order.get(i, (999, ""))
        if not isinstance(rk, int):
            rk = int(rk) if str(rk).isdigit() else 999
        row["_rank"], row["notes"] = rk, why
    return sorted(rows, key=lambda r: r["_rank"])


def to_sheet_rows(rows) -> list[list[str]]:
    out = []
    for r in rows:
        out.append([
            "", r.get("objective", ""), r.get("target_barrier", ""),
            r.get("idea_summary", ""), "N", r.get("idea_source", ""), "",
            r.get("utah_code", ""), r.get("past_attempts", ""),
            r.get("more_info", ""), r.get("type_of_change", ""),
            r.get("type_of_cost", ""), r.get("cost_notes", ""),
            "auto-scan", "", "", "", r.get("estimated_cost", ""),
            r.get("cost_explanation", ""), r.get("people_affected", ""),
            r.get("impact_explanation", ""), r.get("follow_up", ""),
            r.get("notes", ""), "", "", "", "", "", "", "",
        ])
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="run the pipeline but print rows instead of writing")
    ap.add_argument("--objectives", help='e.g. "3" or "1,3,6" (overrides Scan Settings tab)')
    ap.add_argument("--tiers", help='e.g. "1" or "1-2" (overrides Scan Settings tab)')
    ap.add_argument("--topics", help="comma-separated extra search topics")
    ap.add_argument("--days", type=int, help="search window in days")
    ap.add_argument("--max-ideas", type=int, help="cap on staged rows")
    args = ap.parse_args()

    log("code build: annual-scan (2026-11)")

    cfg = yaml.safe_load((HERE / "sources.yaml").read_text())
    svc = None if args.dry_run else sheets_service()

    settings = read_scan_settings(svc) if svc else dict(DEFAULT_SETTINGS)
    # one-off overrides: env vars come from the GitHub "Run workflow" input
    # boxes; CLI flags (local runs) win over everything
    for key, env_var, cli_val, parse in [
        ("objectives", "SCAN_OBJECTIVES", args.objectives, lambda v: parse_nums(v, 7)),
        ("tiers", "SCAN_TIERS", args.tiers, lambda v: parse_nums(v, 3)),
        ("topics", "SCAN_TOPICS", args.topics, parse_topics),
    ]:
        for raw in (os.environ.get(env_var, ""), cli_val or ""):
            if raw.strip():
                settings[key] = parse(raw)
    # lookback-scan.yml sets these; the monthly workflow leaves them unset
    if m := re.search(r"\d+", os.environ.get("SCAN_DAYS", "")):
        settings["days"] = int(m.group())
    if raw := os.environ.get("SCAN_MAX", "").strip():
        if (v := parse_max(raw)) is not ...:
            settings["max_ideas"] = v
    if raw := os.environ.get("SCAN_LABEL", "").strip():
        settings["label"] = raw
    if os.environ.get("SCAN_SHOW_KILLED", "").strip().lower() == "false":
        settings["show_killed"] = False
    if os.environ.get("SCAN_MODE", "").strip().lower() == "annual":
        settings["mode"] = "annual"
    if os.environ.get("SCAN_SHORT_ROWS", "").strip().lower() == "true":
        settings["short_rows"] = True
    if args.days:
        settings["days"] = args.days
    if args.max_ideas:
        settings["max_ideas"] = args.max_ideas
    log(f"settings: {settings_summary(settings)}")

    candidates = gather_candidates(cfg, settings)
    if not candidates:
        log("no candidates found — exiting cleanly")
        return 0

    existing = read_existing_summaries(svc) if svc else []
    n_raw = len(candidates)
    candidates = dedupe(candidates, existing)
    live, dead = verify(candidates)
    rows = rank(short_rows(live) if settings["short_rows"] else build_rows(live, cfg))
    if settings["max_ideas"] is not None:
        rows = rows[: settings["max_ideas"]]

    funnel = (f"{n_raw} candidates → {len(candidates)} after dedupe → "
              f"{len(live)} verified live, {len(dead)} killed → {len(rows)} staged")

    if args.dry_run:
        print(json.dumps(rows, indent=2))
        log(f"DRY RUN: {funnel}")
        return 0

    if not rows and not dead:
        log("nothing survived and nothing was killed — no tab written")
        return 0
    write_results_tab(svc, to_sheet_rows(rows), settings, dead, funnel)
    log(f"done: {funnel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
