# TPP Policy Scanner

Automated scan for new Utah policy ideas for The Policy Project's Policy Vault.

## What this is for

TPP runs two kinds of scans, and both have **one purpose: to find and track new policy ideas** for the Policy Vault. Neither is meant to collect research findings or statistics for their own sake.

| Scan | How it runs | What it looks for | Where ideas go |
|---|---|---|---|
| **Monthly scan** (this repo) | Automatically, 1st of each month | Recent publications from the sources in `sources.yaml` | Staging tab |
| **Annual scan** (this repo) | By hand, once a year | Past-year publications, every source × every sub-goal | Annual Scan tab → picks → Staging |
| **Bi-annual research scan** (not in this repo) | By hand with Claude, every ~6 months | Utah research reports and needs assessments, read in depth | Word doc; its Vault-ready rows → Staging |

A policy idea counts only if it is **something the Utah Legislature, a state agency or Utah Medicaid could do** and it is **not already Utah law or already in the Vault**. Ideas come from two places:

1. **Recommendations a report makes outright** — both scans.
2. **Ideas drawn from gaps and needs a report documents** — the bi-annual research scan only, because each one needs a model (another state's law or a research source) and that takes reading beyond the report. Gap ideas with no model are labeled "TPP concept" and treated as hypotheses. The automated scans skip findings that come with no policy ask.

Findings and statistics show up only as evidence for an idea: in a row's cost and impact estimates (from the Utah Statistics tab) or in the bi-annual scan's evidence sections.

## How the scanner works

Every run:

1. **Searches** ~30 sources (Utah agencies, research institutes, advocacy orgs, national think tanks, Utah news) via the Tavily API for policy-relevant publications
2. **Extracts** candidate policy ideas from the results (Claude API)
3. **Deduplicates** against every existing entry in the Policy Vault Google Sheet
4. **Verifies** each surviving candidate against *current Utah law* (one general web search plus one limited to le.utah.gov) — historically ~40% of promising candidates turn out to already be Utah statute, so this step is non-negotiable
5. **Builds full Vault rows** — Utah Code and past attempts only from what the le.utah.gov/web research found; cost estimates from one extra search for fiscal notes and other states' comparable programs; impact from the Vault's **Utah Statistics** tab; each estimate shows its math, cites sources and ends with a confidence rating (Conf: LOW/MED/HIGH). Target / Barrier and types of change and cost reuse the values already in the Policies tab.
6. **Adds** the ranked rows to the running **Staging** tab, in the Policies layout, with the feasibility-screening columns left blank for the team, and records the run in the **Scan Log** tab (settings, counts, and which ideas were removed as already Utah law)

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

### 3. Annual scan (past year, every source) → pick → Full Treatment

**Step 1 — Annual Scan.** Actions tab → **Annual Scan (past year)** → **Run workflow**. It searches **each** research, government and advocacy source in `sources.yaml` on its own, once for **each sub-goal** of TPP's 7 objectives (the `targets` list in `sources.yaml`, copied from the Vault's "Objectives + Targets/Barriers" tab), for past-year publications. Only ideas that advance one of those sub-goals are kept. News sites and le.utah.gov are left out (`annual: false`). Expect a few hours. Every idea that survives dedupe and the Utah-law check is listed, ranked, in a new tab named like `Annual Scan — Nov 08 2026`: rank, objective, sub-goal, idea, source, the Utah-law check. No cost/impact estimates yet. Already-law ideas are only counted in the Scan Log, not listed. It ignores the Scan Settings tab except **Extra search topics** — clear that cell first if you don't want those.

**Step 2 — Pick.** Type `Y` in the **Full treatment? (Y)** column next to the ideas you want.

**Step 3 — Full Treatment.** Actions tab → **Full Treatment (annual picks)** → **Run workflow**. Each picked idea is re-checked against Utah law, gets a full row (same rules as the monthly scan), and is added to **Staging**. Its pick cell changes to `Done <date>` (or says why it wasn't staged), so you can run it again any time without repeats.

## Reading the results

- **Staging** — every new full row (monthly scan and annual picks), ranked within its run (Notes column), `Team Member(s)` = `auto-scan <date>`, `ID #` blank. The team screens feasibility here (Status, Why Passed / Parked, Package Fit, etc. — the scanner never fills those), then copies finished rows to Policies.
- **Scan Log** — one row per run: settings used, the counts at each step, and (monthly only) the ideas removed as already Utah law with the reason. Worth skimming, since it's the main way the scanner earns trust.
- **Annual Scan — <date>** — the annual list to pick from.

Duplicates are checked against Policies, New Policy Ideas, CUT, Staging and every annual/scan tab, so an idea listed once (even if not picked) won't come back as new.

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
| `VAULT_SPREADSHEET_ID` | `1QRDMnU11uf9qBqfPXOXixl7puEcqYLywB8xJ-GDvwLw` (CURRENT Policy Vault - TPP Owned; the July 2026 scans ran against the OG Policy Vault, `1V2drIfK1Gn4pb5i_Uj5S7f4SV4jiV227Wt6JV7ouXEE`) |

### 2. Google service account (lets the script read settings and write results)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project (e.g. `tpp-policy-scanner`)
2. **APIs & Services → Enable APIs** → enable **Google Sheets API**
3. **IAM & Admin → Service Accounts → Create service account** (any name, no roles needed)
4. Open the service account → **Keys → Add key → JSON** → download the file
5. Paste the file's entire contents into the `GOOGLE_SERVICE_ACCOUNT_JSON` secret
6. **Share the Policy Vault spreadsheet** with the service account's email address (looks like `something@project.iam.gserviceaccount.com`) as an **Editor** — this is the step everyone forgets

### 3. Test it

Actions tab → **Policy Scan** → **Run workflow** (leave the boxes blank). Watch the log; first run takes ~10–15 minutes. Check the Vault's Staging and Scan Log tabs.

---

## Files

| File | What it is |
|---|---|
| `scanner/scan.py` | The whole pipeline |
| `scanner/sources.yaml` | The source registry and search queries — **edit this to add/remove sources**, no code changes needed |
| `scanner/prompts.py` | The Claude prompts (extraction, dedupe, verification, row-building, ranking) |
| `scanner/utah_statistics.csv` | Backup copy of Utah baseline stats, used only if the Vault's **Utah Statistics** tab can't be read |
| `.github/workflows/monthly-scan.yml` | The schedule (cron, currently 1st of month ~8 AM Mountain) and the Run-workflow button |
| `.github/workflows/annual-scan.yml` | The manual annual scan (past year, every source separately) |
| `.github/workflows/full-treatment.yml` | Builds full rows for annual-scan ideas marked Y and adds them to Staging |

## Changing things

- **Focus a scan**: edit the Scan Settings tab (standing) or use the Run-workflow boxes (one-off) — see above
- **Add a source**: add a line to `sources.yaml`. Done.
- **Change the schedule**: edit the `cron:` line in `.github/workflows/monthly-scan.yml` (times are UTC; Mountain is UTC-6/-7)
- **Run it locally** (for testing): `TAVILY_API_KEY=... ANTHROPIC_API_KEY=... python scanner/scan.py --dry-run --objectives "3"`

## Design principles (read before "improving" it)

1. **The Google Sheet is the single source of truth.** This scanner only adds rows to Staging, Scan Log and Annual Scan tabs (and marks annual picks done); it never edits the Policies tab. Humans screen rows in Staging and promote them into the vault.
2. **Verification beats volume.** An idea that's already Utah law staged into the vault costs the team credibility with legislators. The verification step deliberately kills a large share of candidates. 5 verified ideas > 25 unverified ones.
3. **Never guess numbers.** Every cost/impact figure must show its math, source and confidence rating in the row. Anything unverifiable gets an explicit `FLAG: verify X` note instead of a made-up number.
4. **Settings are data, not code.** Everything Marin needs to change lives in the Scan Settings tab or the Run-workflow boxes. Nothing she can type there can break a scan.

## History

Built July 2026 by Becca Linford (TPP intern) with Claude Code, replacing a manual Claude session workflow. The scan protocol was developed and validated in the July 2026 scan (25 ideas staged; 10 candidates killed as already-enacted law). Reworked July 13, 2026: email step removed in favor of dated results tabs; scan scope made controllable via the Scan Settings tab and Run-workflow inputs.
