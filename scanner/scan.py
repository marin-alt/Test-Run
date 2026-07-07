#!/usr/bin/env python3
"""TPP monthly policy scanner.

Pipeline: Tavily search -> Claude extract -> dedupe vs vault -> verify vs Utah law
          -> build rows -> rank -> write staging tab -> email Marin.

Run locally:  TAVILY_API_KEY=... ANTHROPIC_API_KEY=... python scanner/scan.py --dry-run
In CI:        driven by .github/workflows/monthly-scan.yml
"""

import argparse
import datetime as dt
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from anthropic import Anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build

import prompts

HERE = Path(__file__).parent
MODEL_FAST = "claude-haiku-4-5-20251001"   # extraction, verification
MODEL_SMART = "claude-sonnet-5"            # dedupe, row-building, ranking, email

VAULT_ID = os.environ.get("VAULT_SPREADSHEET_ID", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

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

claude = Anthropic()  # reads ANTHROPIC_API_KEY


def log(msg: str) -> None:
    print(f"[scan] {msg}", flush=True)


def ask_claude(model: str, prompt: str, max_tokens: int = 8000) -> str:
    resp = claude.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def ask_claude_json(model: str, prompt: str, max_tokens: int = 8000):
    """Ask for JSON and parse it, tolerating markdown fences."""
    text = ask_claude(model, prompt + "\n\nRespond with ONLY valid JSON.", max_tokens)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    return json.loads(text)


# ---------------------------------------------------------------- Tavily

def tavily_search(query: str, domains=None, days: int = 60, max_results: int = 5):
    r = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": os.environ["TAVILY_API_KEY"],
            "query": query,
            "days": days,
            "max_results": max_results,
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
    """Idea summaries from the Policies tab plus every Staging tab."""
    meta = svc.get(spreadsheetId=VAULT_ID).execute()
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    summaries = []
    for title in titles:
        if title != "Policies" and not title.startswith("Staging"):
            continue
        vals = svc.values().get(
            spreadsheetId=VAULT_ID, range=f"'{title}'!D2:D500").execute()
        summaries += [row[0] for row in vals.get("values", []) if row and row[0]]
    log(f"loaded {len(summaries)} existing vault/staging summaries")
    return summaries


def write_staging_tab(svc, rows: list[list[str]]) -> str:
    month = dt.date.today().strftime("%b %Y")
    title = f"Staging — New Ideas ({month})"
    resp = svc.batchUpdate(
        spreadsheetId=VAULT_ID,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()
    gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    svc.values().update(
        spreadsheetId=VAULT_ID, range=f"'{title}'!A1",
        valueInputOption="RAW", body={"values": [VAULT_HEADERS] + rows},
    ).execute()
    url = f"https://docs.google.com/spreadsheets/d/{VAULT_ID}/edit?gid={gid}#gid={gid}"
    log(f"wrote {len(rows)} rows to '{title}' -> {url}")
    return url


# ---------------------------------------------------------------- pipeline stages

def gather_candidates(cfg) -> list[dict]:
    domains = [s["domain"] for s in cfg["sources"]]
    hits = []
    for q in cfg["queries"]:
        try:
            hits += tavily_search(q, domains=domains)
        except Exception as e:  # one failed query shouldn't kill the run
            log(f"WARN query failed ({q}): {e}")
    log(f"{len(hits)} search results")
    if not hits:
        return []
    blob = "\n\n".join(
        f"[{h.get('url')}] {h.get('title')}\n{h.get('content', '')[:1200]}" for h in hits)
    ideas = ask_claude_json(MODEL_FAST, prompts.EXTRACT.format(
        objectives="\n".join(cfg["objectives"]), results=blob))
    log(f"{len(ideas)} candidate ideas extracted")
    return ideas


def dedupe(candidates, existing) -> list[dict]:
    if not candidates:
        return []
    result = ask_claude_json(MODEL_SMART, prompts.DEDUPE.format(
        candidates=json.dumps(candidates, indent=1),
        existing="\n".join(f"- {s[:300]}" for s in existing)))
    by_idea = {r["idea"]: r for r in result}
    keep = [c for c in candidates
            if not by_idea.get(c["idea"], {}).get("duplicate", False)]
    log(f"dedupe: {len(candidates)} -> {len(keep)}")
    return keep


def verify(candidates) -> tuple[list[dict], list[dict]]:
    live, dead = [], []
    for c in candidates:
        try:
            results = tavily_search(f"Utah law status: {c['idea'][:150]}",
                                    days=3650, max_results=5)
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
        if v["status"] == "dead":
            dead.append(c)
        else:
            c["idea"] = v.get("reframed_idea") or c["idea"]
            if v["status"] == "uncertain":
                c["idea"] += "  [FLAG: could not fully verify Utah status — check before use]"
            live.append(c)
    log(f"verify: {len(live)} live, {len(dead)} already-law/dead")
    return live, dead


def build_rows(candidates, cfg) -> list[dict]:
    stats = STATS_FILE.read_text() if STATS_FILE.exists() else "(stats file missing)"
    rows = []
    for c in candidates:
        try:
            row = ask_claude_json(MODEL_SMART, prompts.BUILD_ROW.format(
                idea=c["idea"], objective=c.get("objective", ""),
                source_url=c.get("source_url", ""),
                verification=json.dumps(c.get("verification", {})),
                stats=stats[:8000], barriers="(use vault dropdown values)"))
            rows.append(row)
        except Exception as e:
            log(f"WARN row build failed, skipping: {e}")
    return rows


def rank(rows) -> list[dict]:
    if not rows:
        return rows
    ranking = ask_claude_json(MODEL_SMART, prompts.RANK.format(
        ideas=json.dumps([r["idea_summary"] for r in rows], indent=1)))
    order = {r["idea_summary"]: (r["rank"], r["rationale"]) for r in ranking}
    for row in rows:
        rk, why = order.get(row["idea_summary"], (999, ""))
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


def send_email(tab_url, rows, killed) -> None:
    body = ask_claude(MODEL_SMART, prompts.EMAIL.format(
        tab_url=tab_url,
        ideas=json.dumps([{ "rank": r["_rank"], "idea": r["idea_summary"],
                            "cost": r.get("estimated_cost", ""),
                            "people": r.get("people_affected", "")} for r in rows], indent=1),
        killed=json.dumps([{ "idea": k["idea"],
                             "why": k["verification"]["reasoning"]} for k in killed], indent=1)))
    msg = MIMEText(body, "html")
    msg["Subject"] = f"New Policy Ideas — {dt.date.today().strftime('%B %Y')} automated scan"
    msg["From"], msg["To"] = EMAIL_FROM, EMAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_FROM, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)
    log(f"email sent to {EMAIL_TO}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="run the pipeline but print rows instead of writing/emailing")
    args = ap.parse_args()

    cfg = yaml.safe_load((HERE / "sources.yaml").read_text())

    candidates = gather_candidates(cfg)
    if not candidates:
        log("no candidates this month — exiting cleanly")
        return 0

    svc = None if args.dry_run else sheets_service()
    existing = [] if args.dry_run else read_existing_summaries(svc)
    candidates = dedupe(candidates, existing)
    live, dead = verify(candidates)
    rows = rank(build_rows(live, cfg))

    if args.dry_run:
        print(json.dumps(rows, indent=2))
        log(f"DRY RUN: would stage {len(rows)} rows; {len(dead)} killed as already-law")
        return 0

    if not rows:
        log("nothing survived verification — sending a no-news email")
    tab_url = write_staging_tab(svc, to_sheet_rows(rows)) if rows else "(no tab created)"
    send_email(tab_url, rows, dead)
    return 0


if __name__ == "__main__":
    sys.exit(main())
