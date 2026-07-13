# TPP Policy Scanner

Automated scan for new Utah policy ideas for The Policy Project's Policy Vault.

Every run:

1. **Searches** ~30 sources (Utah agencies, research institutes, advocacy orgs, national think tanks, Utah news) via the Tavily API for policy-relevant publications
2. **Extracts** candidate policy ideas from the results (Claude API)
3. **Deduplicates** against every existing entry in the Policy Vault Google Sheet
4. **Verifies** each surviving candidate against *current Utah law* — historically ~40% of promising candidates turn out to already be Utah statute, so this step is non-negotiable
5. **Writes** the survivors as ranked, fully-formatted rows into a **new dated tab** in the Policy Vault, named like `Scan — Obj 3 — Jul 13 2026`, with a SCAN DETAILS block underneath showing the settings used and which candidates were killed as already-law

It runs automatically on the **1st of each month**, and anyone with access to this repo can run it on demand. If a run breaks, GitHub emails the account owner a failure notice.

---

## How to control what gets scanned (no code required)

There are two knobs, and they work together:

### 1. The "Scan Settings" tab — your standing preferences

The Policy Vault spreadsheet has a tab called **Scan Settings**. The scanner reads it at the start of *every* run. Edit column B, and every future scan honors it:

| Setting | Example values | What it does |
|---|---|---|
| Objectives | `All`, `3`, `1, 3, 6` | Which of the 7 objectives to scan for |
| Source tiers | `All`, `1`, `1-2` | 1 = Utah gov & research institutes, 2 = Utah advocacy orgs, 3 = national think tanks & Utah news |
| Extra search topics | `school lunch, teacher retention` | Extra topics searched every run until you clear the cell |
| Search window (days) | `60` | How far back to look for publications |
| Max ideas to stage | `25` | Cap on rows written per scan |

You can't break anything from this tab: an unreadable value just falls back to the default, and the scan notes what settings it actually used in its results tab.

### 2. The "Run workflow" button — one-off focused scans

For a one-time scan **right now** without touching the standing settings:

1. Go to this repo's **Actions** tab → **Policy Scan** → **Run workflow**
2. Optionally type objectives (e.g. `3`) and/or extra topics into the boxes
3. Click the green **Run workflow** button — results appear in a new dated Vault tab in ~10–15 minutes

Blank boxes = run with the Scan Settings tab as-is.

### Reading the results

Each scan creates one new tab in the Vault. The tab name tells you the date and scan type. Rows are ranked best-first (rank + rationale in the Notes column), formatted to match the Policies tab so rows can be promoted by copy-paste, with `ID #` left blank for Marin to assign. Below the rows, the SCAN DETAILS block lists the candidates that were **killed because they're already Utah law** — worth skimming, since it's the main way the scanner earns trust.

---

## One-time setup (~20 minutes)

### 1. API keys (three secrets)

Go to this repo's **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Where to get it |
|---|---|
| `TAVILY_API_KEY` | tavily.com → dashboard → API keys (free tier: 1,000 searches/mo, plenty) |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys. **Use a TPP org account, not a personal one.** Expected cost: $2–10/month |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | See step 2 — paste the entire JSON file contents |

And under **Variables** (same page, "Variables" tab):

| Variable | Value |
|---|---|
| `VAULT_SPREADSHEET_ID` | `1V2drIfK1Gn4pb5i_Uj5S7f4SV4jiV227Wt6JV7ouXEE` |

### 2. Google service account (lets the script read settings and write results)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project (e.g. `tpp-policy-scanner`)
2. **APIs & Services → Enable APIs** → enable **Google Sheets API**
3. **IAM & Admin → Service Accounts → Create service account** (any name, no roles needed)
4. Open the service account → **Keys → Add key → JSON** → download the file
5. Paste the file's entire contents into the `GOOGLE_SERVICE_ACCOUNT_JSON` secret
6. **Share the Policy Vault spreadsheet** with the service account's email address (looks like `something@project.iam.gserviceaccount.com`) as an **Editor** — this is the step everyone forgets

### 3. Test it

Actions tab → **Policy Scan** → **Run workflow** (leave the boxes blank). Watch the log; first run takes ~10–15 minutes. Check the Vault for the new dated tab.

---

## Files

| File | What it is |
|---|---|
| `scanner/scan.py` | The whole pipeline |
| `scanner/sources.yaml` | The source registry and search queries — **edit this to add/remove sources**, no code changes needed |
| `scanner/prompts.py` | The Claude prompts (extraction, dedupe, verification, row-building, ranking) |
| `scanner/utah_statistics.csv` | Utah baseline stats, so cost/impact figures cite real numbers |
| `.github/workflows/monthly-scan.yml` | The schedule (cron, currently 1st of month ~8 AM Mountain) and the Run-workflow button |

## Changing things

- **Focus a scan**: edit the Scan Settings tab (standing) or use the Run-workflow boxes (one-off) — see above
- **Add a source**: add a line to `sources.yaml`. Done.
- **Change the schedule**: edit the `cron:` line in `.github/workflows/monthly-scan.yml` (times are UTC; Mountain is UTC-6/-7)
- **Run it locally** (for testing): `TAVILY_API_KEY=... ANTHROPIC_API_KEY=... python scanner/scan.py --dry-run --objectives "3"`

## Design principles (read before "improving" it)

1. **The Google Sheet is the single source of truth.** This scanner only ever *adds* dated scan tabs; it never edits the main Policies tab. Humans promote rows from a scan tab into the vault.
2. **Verification beats volume.** An idea that's already Utah law staged into the vault costs the team credibility with legislators. The verification step deliberately kills a large share of candidates. 5 verified ideas > 25 unverified ones.
3. **Never guess numbers.** Every cost/impact figure must show its math and source in the row. Anything unverifiable gets an explicit `FLAG: verify X` note instead of a made-up number.
4. **Settings are data, not code.** Everything Marin needs to change lives in the Scan Settings tab or the Run-workflow boxes. Nothing she can type there can break a scan.

## History

Built July 2026 by Becca Linford (TPP intern) with Claude Code, replacing a manual Claude session workflow. The scan protocol was developed and validated in the July 2026 scan (25 ideas staged; 10 candidates killed as already-enacted law). Reworked July 13, 2026: email step removed in favor of dated results tabs; scan scope made controllable via the Scan Settings tab and Run-workflow inputs.
