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
STAGING_TAB = "Staging"      # one running tab; the team screens rows here
LOG_TAB = "Scan Log"         # one row per run: settings, counts, already-law list
STATS_TAB = "Utah Statistics"
ANNUAL_PREFIX = "Annual Scan —"
PICK_HEADER = "Full treatment? (Y)"
ANNUAL_HEADERS = [PICK_HEADER, "Rank", "Objective", "Sub-goal",
                  "Idea Summary", "Source", "Source Link",
                  "Utah-law check", "Utah Code", "Rank rationale"]

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
}

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
    # streaming: the SDK refuses non-streaming calls whose max_tokens could
    # take over 10 minutes (e.g. ranking hundreds of ideas)
    with claude.messages.stream(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        resp = stream.get_final_message()
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

            + (f", topics={s['topics']}" if s["topics"] else ""))


def selected_objectives(cfg, objs) -> list[str]:
    if objs is None:
        return cfg["objectives"]
    return [o for o in cfg["objectives"] if int(o.split(".")[0]) in objs]


def objectives_text(cfg, objs) -> str:
    """In-scope objectives with their sub-goals, for the extraction prompt."""
    out = []
    for o in selected_objectives(cfg, objs):
        n = int(o.split(".")[0])
        out.append(o)
        out += [f"  {t['id']} {t['target']}" for t in cfg.get("targets", [])
                if t["objective"] == n]
    return "\n".join(out)


def target_label(cfg, tid) -> str:
    for t in cfg.get("targets", []):
        if t["id"] == str(tid).strip():
            return f"{t['id']} {t['target']}"
    return str(tid or "")


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
    """Idea summaries from Policies, New Policy Ideas, CUT, Staging and every
    scan tab — found by the header that starts "Idea Summary"."""
    summaries = []
    for title in tab_titles(svc):
        if (title not in ("Policies", "New Policy Ideas", "CUT")
                and not title.startswith(("Staging", "Scan —", ANNUAL_PREFIX))):
            continue
        rows = read_rows(svc, title)
        heads = [str(h).strip().lower() for h in (rows[0] if rows else [])]
        col = next((i for i, h in enumerate(heads) if h.startswith("idea summary")), 3)
        summaries += [r[col] for r in rows[1:] if len(r) > col and r[col].strip()]
    log(f"loaded {len(summaries)} existing vault/staging summaries")
    return summaries


def tab_titles(svc) -> list[str]:
    return [t["properties"]["title"]
            for t in svc.get(spreadsheetId=VAULT_ID).execute()["sheets"]]


def read_rows(svc, title, rng="A1:AZ") -> list[list[str]]:
    return svc.values().get(spreadsheetId=VAULT_ID,
                            range=f"'{title}'!{rng}").execute().get("values", [])


def ensure_tab(svc, title, headers) -> list[str]:
    """Create the tab with a header row if it's missing; return its headers."""
    if title in tab_titles(svc):
        rows = read_rows(svc, title, "A1:AZ1")
        if rows and any(rows[0]):
            return rows[0]
    else:
        svc.batchUpdate(spreadsheetId=VAULT_ID, body={"requests": [
            {"addSheet": {"properties": {"title": title}}}]}).execute()
    svc.values().update(spreadsheetId=VAULT_ID, range=f"'{title}'!A1",
                        valueInputOption="RAW", body={"values": [headers]}).execute()
    return headers


def append_rows(svc, title, rows) -> None:
    if rows:
        svc.values().append(spreadsheetId=VAULT_ID, range=f"'{title}'!A1",
                            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                            body={"values": rows}).execute()


def write_log(svc, scan_name, settings, funnel, killed) -> None:
    headers = ["Date", "Scan", "Settings used", "Funnel",
               "Removed as already Utah law (idea — why)"]
    ensure_tab(svc, LOG_TAB, headers)
    gone = ""
    if killed and settings["show_killed"]:
        gone = "\n".join(f"• {k['idea']} — {k['verification'].get('reasoning', '')}"
                         for k in killed)[:49000]
    append_rows(svc, LOG_TAB, [[dt.date.today().isoformat(), scan_name,
                                settings_summary(settings), funnel, gone]])


def stage_rows(svc, rows) -> None:
    """Append full rows to the running Staging tab, in the Policies layout."""
    headers = ensure_tab(svc, STAGING_TAB, read_policy_headers(svc))
    append_rows(svc, STAGING_TAB, to_sheet_rows(rows, headers))
    log(f"added {len(rows)} rows to '{STAGING_TAB}'")


def write_annual_tab(svc, rows) -> None:
    base = f"{ANNUAL_PREFIX} {dt.date.today().strftime('%b %d %Y')}"
    taken, title, n = set(tab_titles(svc)), base, 2
    while title in taken:
        title, n = f"{base} ({n})", n + 1
    ensure_tab(svc, title, ANNUAL_HEADERS)
    append_rows(svc, title, [[
        "", r["_rank"] if r["_rank"] != 999 else "", r.get("objective", ""),
        r.get("target_barrier", ""), r.get("idea_summary", ""),
        r.get("source_label", ""), r.get("source_url", ""),
        r.get("law_check", ""), r.get("utah_code", ""), r.get("notes", "")]
        for r in rows])
    log(f"wrote {len(rows)} ideas to '{title}'")


def read_utah_stats(svc) -> str:
    """The Vault's Utah Statistics tab (metric, value, year, source, note);
    falls back to the bundled CSV if the tab can't be read."""
    if svc:
        try:
            rows = read_rows(svc, STATS_TAB, "A1:E500")  # E = verification notes / FLAGs
            if rows:
                return "\n".join(" | ".join(r) for r in rows if r)
        except Exception as e:
            log(f"WARN could not read '{STATS_TAB}' tab ({e}) — using bundled CSV")
    return (STATS_FILE.read_text(encoding="utf-8", errors="replace")
            if STATS_FILE.exists() else "(no Utah statistics available)")


def vault_options(svc) -> dict:
    """Values the team already uses in Policies (Target / Barrier, Type(s) of
    Change, Type(s) of Cost), so new rows use the same vocabulary. Only values
    used at least twice count, to skip one-off free text."""
    out = {"objective": [], "target_barrier": [], "type_of_change": [], "type_of_cost": []}
    if not svc:
        return out
    try:
        rows = read_rows(svc, "Policies", "A1:AZ")
    except Exception as e:
        log(f"WARN could not read Policies values ({e})")
        return out
    plan = column_plan(rows[0]) if rows else []
    for key in out:
        if key not in plan:
            continue
        i, counts = plan.index(key), {}
        for r in rows[1:]:
            v = r[i].strip() if len(r) > i else ""
            if v:
                counts[v] = counts.get(v, 0) + 1
        out[key] = sorted(v for v, c in counts.items() if c >= 2)[:80]
    return out


# ---------------------------------------------------------------- pipeline stages

def search_plan(cfg, settings) -> list[tuple[str, list[str]]]:
    """The (query, domains) pairs to search. Monthly/topic mode: each topic
    query once per source tier. Annual mode: every annual_queries line on
    every source separately, so no site competes with another for results."""
    tiers = settings["tiers"]
    srcs = [src for src in cfg["sources"] if tiers is None or src["tier"] in tiers]
    objs = settings["objectives"]
    if settings["mode"] == "annual":
        qs = [t["q"] for t in cfg["targets"]
              if objs is None or t["objective"] in objs]
        qs += [f"Utah {t}" for t in settings["topics"]]
        return [(f"{src['hint']} {q}" if src.get("hint") else q, [src["domain"]])
                for src in srcs if src.get("annual", True) for q in qs]
    groups = {}  # one search per tier, so each tier gets its own 20 results
    for src in srcs:
        doms = groups.setdefault(src["tier"], [])
        if src["domain"] not in doms:  # a domain can be listed twice with hints
            doms.append(src["domain"])
    queries = [q["q"] for q in cfg["queries"]
               if objs is None or not q.get("objectives")
               or set(q["objectives"]) & set(objs)]
    queries += [f"Utah policy {t}" for t in settings["topics"]]
    return [(q, doms) for q in queries for doms in groups.values()]


LISTING_HINTS = ("meeting", "agenda", "search", "/tag/", "/category/", "/topics/",
                 "calendar", "/news/?", "index.php", "/archive")


def is_listing_page(url: str) -> bool:
    """True for homepages and list/index pages (meeting lists, search, tags),
    which aren't a specific source document."""
    u = str(url or "").strip().lower()
    if not u.startswith("http"):
        return True
    path = u.split("://", 1)[1].partition("/")[2].split("#")[0]
    if path.strip("/") == "":
        return True
    return any(h in "/" + path for h in LISTING_HINTS)


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

    objectives = objectives_text(cfg, objs)
    ideas = []
    for i in range(0, len(hits), BATCH):
        blob = "\n\n".join(
            f"[{h.get('url')}] {h.get('title')}\n{h.get('content', '')[:1200]}"
            for h in hits[i:i + BATCH])
        try:
            got = ask_claude_json(MODEL_FAST, prompts.EXTRACT.format(
                objectives=objectives, results=blob, today=dt.date.today().isoformat()))
            ideas += [g for g in got if isinstance(g, dict) and g.get("idea")
                      and not is_listing_page(g.get("source_url", ""))]
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


def research_blob(results, label) -> str:
    return "\n\n".join(
        f"[{label}] [{r.get('url')}] {r.get('title')}\n{r.get('content', '')[:800]}"
        for r in results)


def verify_one(c) -> dict:
    """Utah-law check: one general web search plus one limited to
    le.utah.gov (bills and code). The results are kept for row-building, so
    Utah Code and Past Attempts come from what was actually found."""
    q = c["idea"][:150]
    try:
        general = tavily_search(f"Utah law status: {q}", days=None, max_results=5)
        try:
            leg = tavily_search(q, domains=["le.utah.gov"], days=None, max_results=5)
        except Exception as e:
            log(f"WARN le.utah.gov search failed: {e}")
            leg = []
        c["research"] = (research_blob(leg, "le.utah.gov") + "\n\n"
                         + research_blob(general, "web"))
        v = ask_claude_json(MODEL_FAST, prompts.VERIFY.format(
            idea=c["idea"], results=c["research"]))
    except Exception as e:
        log(f"WARN verification errored, treating as uncertain: {e}")
        c.setdefault("research", "")
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


def build_rows(candidates, stats, options) -> list[dict]:
    """Full Vault rows. Each idea gets one more search for cost comparables
    (fiscal notes, other states' programs) before Claude writes the row."""
    opts = "\n".join(f"{k}: " + ("; ".join(v) if v else "(none found — free text)")
                     for k, v in options.items())

    def one(c):
        try:
            try:
                cost = tavily_search(f"fiscal note cost estimate state program {c['idea'][:150]}",
                                     days=None, max_results=5)
            except Exception as e:
                log(f"WARN cost search failed: {e}")
                cost = []
            row = ask_claude_json(MODEL_SMART, prompts.BUILD_ROW.format(
                idea=c["idea"], objective=c.get("objective", ""),
                source_url=c.get("source_url", ""),
                verification=json.dumps(c.get("verification", {})),
                law_research=c.get("research", "") or "(none)",
                cost_research=research_blob(cost, "cost") or "(none)",
                stats=stats[:20000], options=opts))
            if isinstance(row, dict):
                row["_key"] = c.get("_key")
            if isinstance(row, dict):
                row.setdefault("objective", c.get("objective", ""))
                row["more_info"] = " ".join(x for x in (
                    row.get("more_info", ""),
                    f"Utah-law check: {c.get('verification', {}).get('reasoning', '')}") if x)
            return row
        except Exception as e:
            log(f"WARN row build failed, skipping: {e}")
            return None

    rows = [r for r in pmap(one, candidates) if isinstance(r, dict)]
    log(f"built {len(rows)} rows from {len(candidates)} verified ideas")
    return rows


def short_rows(candidates, cfg) -> list[dict]:
    """Annual scan: what extraction and the Utah-law check found — no extra
    searches or Claude calls. Full treatment happens later for picked ideas."""
    return [{
        "objective": normalize_objective(c.get("objective", ""), cfg),
        "target_barrier": target_label(cfg, c.get("target", "")),
        "idea_summary": c["idea"],
        "source_label": c.get("source_label", ""),
        "source_url": c.get("source_url", ""),
        "utah_code": c.get("verification", {}).get("utah_law_cite", ""),
        "law_check": c.get("verification", {}).get("reasoning", ""),
    } for c in candidates]


def rank(rows) -> list[dict]:
    if not rows:
        return rows
    numbered = "\n".join(f"{i}. {r.get('idea_summary', '')}"
                         for i, r in enumerate(rows, 1))
    try:
        ranking = ask_claude_json(MODEL_SMART, prompts.RANK.format(
            ideas=numbered, today=dt.date.today().isoformat()),
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


# Which scanner field fills which Policies column, matched on the start of the
# column's header (lowercase). First match wins. Columns not listed are left
# blank for the team. Works for both the OG and CURRENT Vault layouts.
COLUMN_FIELDS = [
    ("objective", "objective"), ("target", "target_barrier"),
    ("idea summary", "idea_summary"), ("policy pitch", "=N"),
    ("idea source", "idea_source"), ("utah code", "utah_code"),
    ("past attempts", "past_attempts"), ("more info", "more_info"),
    ("type(s) of change", "type_of_change"), ("type(s) of cost", "type_of_cost"),
    ("cost notes", "cost_notes"), ("team member", "team_member"),
    ("estimated cost", "estimated_cost"), ("cost explanation", "cost_explanation"),
    ("impact / population", "impact_explanation"), ("impact - #", "people_affected"),
    ("# people", "people_affected"), ("other follow up", "follow_up"),
    ("notes / reactions", "notes"),
]


def column_plan(headers) -> list[str]:
    plan = []
    for h in headers:
        h = str(h).strip().lower()
        plan.append(next((f for pre, f in COLUMN_FIELDS if h.startswith(pre)), ""))
    return plan


def read_policy_headers(svc) -> list[str]:
    """The live Policies header row, so scan rows paste straight into it."""
    try:
        vals = svc.values().get(spreadsheetId=VAULT_ID,
                                range="'Policies'!A1:AZ1").execute()
        headers = vals.get("values", [[]])[0]
        if any(column_plan(headers)):
            return headers
    except Exception as e:
        log(f"WARN could not read Policies headers ({e}) — using built-in layout")
    return VAULT_HEADERS


def to_sheet_rows(rows, headers=VAULT_HEADERS) -> list[list[str]]:
    plan = column_plan(headers)
    # The CURRENT Vault has no explanation columns; the number cell already
    # carries its one-line math, source and Conf rating, so the longer
    # explanation is dropped rather than glued on (it doubled cell length).
    out = []
    for r in rows:
        out.append([f[1:] if f.startswith("=") else str(r.get(f, "")) if f else ""
                    for f in plan])
    return out


# ---------------------------------------------------------------- main

def normalize_objective(text, cfg, vault_objs=()) -> str:
    """'Strengthen Public Education' or '3' -> the numbered name the Vault
    uses, e.g. '3. Strengthen Public Education'."""
    text = str(text or "").strip()
    names = list(vault_objs) + list(cfg["objectives"])
    m = re.match(r"\s*(\d)", text)
    n = m.group(1) if m else next(
        (o.split(".")[0] for o in cfg["objectives"]
         if text and text.lower() in o.lower()), None)
    if n is None:
        return text
    return next((o for o in names if o.strip().startswith(f"{n}.")), text)


def finish_rows(rows, cfg, options) -> list[dict]:
    stamp = f"auto-scan {dt.date.today().isoformat()}"
    for r in rows:
        r["team_member"] = stamp
        r["objective"] = normalize_objective(r.get("objective"), cfg,
                                             options.get("objective", []))
        tb, ob = str(r.get("target_barrier", "")), r["objective"]
        if tb[:1].isdigit() and ob[:1].isdigit() and tb[0] != ob[0]:
            r["target_barrier"] = ""  # Guide: objective and target must coincide
    return rows


def col_letter(i: int) -> str:
    out = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        out = chr(65 + rem) + out
    return out


def full_treatment(svc, cfg, settings) -> int:
    """Give full rows to every annual-scan idea marked Y in the
    "Full treatment? (Y)" column, add them to Staging, and mark them done."""
    picks = []
    for title in tab_titles(svc):
        if not title.startswith(ANNUAL_PREFIX):
            continue
        rows = read_rows(svc, title)
        if not rows:
            continue
        idx = {str(h).strip(): i for i, h in enumerate(rows[0])}
        if PICK_HEADER not in idx:
            continue
        get = lambda r, h: r[idx[h]].strip() if h in idx and len(r) > idx[h] else ""
        for n, r in enumerate(rows[1:], start=2):
            if get(r, PICK_HEADER).upper() in ("Y", "YES", "X"):
                key = (title, n)
                picks.append({"_key": key, "idea": get(r, "Idea Summary"),
                              "objective": get(r, "Objective"),
                              "source_url": get(r, "Source Link"),
                              "source_label": get(r, "Source"),
                              "_cell": f"'{title}'!{col_letter(idx[PICK_HEADER])}{n}"})
    picks = [p for p in picks if p["idea"]]
    log(f"full treatment: {len(picks)} ideas marked Y")
    if not picks:
        return 0

    live, dead = verify(picks)
    options = vault_options(svc)
    rows = finish_rows(rank(build_rows(live, read_utah_stats(svc), options)), cfg, options)
    stage_rows(svc, rows)

    today = dt.date.today().isoformat()
    done = {r.get("_key") for r in rows}
    marks = []
    for p in picks:
        if p in dead:
            mark = f"Not staged {today} — now Utah law: {p['verification'].get('reasoning', '')}"
        elif p["_key"] in done:
            mark = f"Done {today} — in {STAGING_TAB}"
        else:
            mark = "Row build failed — set back to Y to retry"
        marks.append({"range": p["_cell"], "values": [[mark[:500]]]})
    svc.values().batchUpdate(spreadsheetId=VAULT_ID, body={
        "valueInputOption": "RAW", "data": marks}).execute()
    funnel = (f"{len(picks)} picked → {len(dead)} now law → "
              f"{len(rows)} added to {STAGING_TAB}")
    write_log(svc, "Full treatment (annual picks)", settings, funnel, dead)
    log(f"done: {funnel}")
    return 0


def check_setup(dry_run: bool) -> list[str]:
    """Plain-English check of the GitHub secrets/variables before any spend."""
    out = []
    for name in ("TAVILY_API_KEY", "ANTHROPIC_API_KEY"):
        if not os.environ.get(name, "").strip():
            out.append(f"secret {name} is missing or empty (check the exact spelling)")
    if dry_run:
        return out
    if not VAULT_ID:
        out.append("variable VAULT_SPREADSHEET_ID is missing (Variables tab, not Secrets)")
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        out.append("secret GOOGLE_SERVICE_ACCOUNT_JSON is missing or empty")
    else:
        try:
            info = json.loads(raw)
            if "client_email" not in info or "private_key" not in info:
                out.append("GOOGLE_SERVICE_ACCOUNT_JSON doesn't look like a service-account key file")
        except json.JSONDecodeError as e:
            out.append("GOOGLE_SERVICE_ACCOUNT_JSON isn't a complete copy of the .json file "
                       f"(broken at line {e.lineno}). Re-paste the whole file, from the first "
                       "{ to the last }.")
    return out


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

    log("code build: staging-workflow (2026-11)")
    if problems := check_setup(args.dry_run):
        for msg in problems:
            log(f"SETUP PROBLEM: {msg}")
        log("Fix these under Settings → Secrets and variables → Actions, then run again. "
            "Nothing was searched, so no credits were used.")
        return 1

    cfg = yaml.safe_load((HERE / "sources.yaml").read_text())
    svc = None if args.dry_run else sheets_service()

    settings = read_scan_settings(svc) if svc else dict(DEFAULT_SETTINGS)
    # one-off overrides: env vars come from the GitHub "Run workflow" input
    # boxes and the annual/full-treatment workflows; CLI flags win over all
    for key, env_var, cli_val, parse in [
        ("objectives", "SCAN_OBJECTIVES", args.objectives, lambda v: parse_nums(v, 7)),
        ("tiers", "SCAN_TIERS", args.tiers, lambda v: parse_nums(v, 3)),
        ("topics", "SCAN_TOPICS", args.topics, parse_topics),
    ]:
        for raw in (os.environ.get(env_var, ""), cli_val or ""):
            if raw.strip():
                settings[key] = parse(raw)
    if m := re.search(r"\d+", os.environ.get("SCAN_DAYS", "")):
        settings["days"] = int(m.group())
    if raw := os.environ.get("SCAN_MAX", "").strip():
        if (v := parse_max(raw)) is not ...:
            settings["max_ideas"] = v
    if raw := os.environ.get("SCAN_LABEL", "").strip():
        settings["label"] = raw
    if os.environ.get("SCAN_SHOW_KILLED", "").strip().lower() == "false":
        settings["show_killed"] = False
    mode = os.environ.get("SCAN_MODE", "").strip().lower()
    if mode in ("annual", "full_treatment"):
        settings["mode"] = mode
    if args.days:
        settings["days"] = args.days
    if args.max_ideas:
        settings["max_ideas"] = args.max_ideas
    log(f"settings: {settings_summary(settings)}")

    if settings["mode"] == "full_treatment":
        if not svc:
            log("full treatment reads the Vault, so it can't run as a dry run")
            return 0
        return full_treatment(svc, cfg, settings)

    candidates = gather_candidates(cfg, settings)
    if not candidates:
        log("no candidates found — exiting cleanly")
        return 0

    existing = read_existing_summaries(svc) if svc else []
    n_raw = len(candidates)
    candidates = dedupe(candidates, existing)
    live, dead = verify(candidates)
    annual = settings["mode"] == "annual"
    # rank first, then cap, so only the ideas that will be staged get the
    # extra cost search and full row (saves credits on capped monthly runs)
    for i, c in enumerate(live):
        c["_key"] = i
    ranked = rank([{**r, "_key": c["_key"]} for r, c in zip(short_rows(live, cfg), live)])
    if settings["max_ideas"] is not None:
        ranked = ranked[: settings["max_ideas"]]
    if annual:
        rows = ranked
    else:
        order = {r["_key"]: (r["_rank"], r.get("notes", "")) for r in ranked}
        chosen = [c for c in live if c["_key"] in order]
        options = vault_options(svc)
        rows = build_rows(chosen, read_utah_stats(svc), options)
        for r in rows:
            r["_rank"], r["notes"] = order.get(r.get("_key"), (999, ""))
        rows = finish_rows(sorted(rows, key=lambda r: r["_rank"]), cfg, options)

    where = "listed for picking" if annual else f"added to {STAGING_TAB}"
    funnel = (f"{n_raw} candidates → {len(candidates)} after dedupe → "
              f"{len(live)} verified live, {len(dead)} already law → {len(rows)} {where}")

    if args.dry_run:
        print(json.dumps(rows, indent=2))
        log(f"DRY RUN: {funnel}")
        return 0

    if annual:
        write_annual_tab(svc, rows)
    else:
        stage_rows(svc, rows)
    write_log(svc, scan_label(settings), settings, funnel, dead)
    log(f"done: {funnel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
