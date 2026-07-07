# TPP Policy Scanner

Automated monthly scan for new Utah policy ideas for The Policy Project's Policy Vault.

On the **1st of each month** this repository:

1. **Searches** ~35 sources (Utah agencies, research institutes, advocacy orgs, national think tanks, Utah news) via the Tavily API for policy-relevant publications from the last 60 days
2. **Extracts** candidate policy ideas from the results (Claude API)
3. **Deduplicates** against every existing entry in the Policy Vault Google Sheet
4. **Verifies** each surviving candidate against *current Utah law* — historically ~40% of promising candidates turn out to already be Utah statute, so this step is non-negotiable
5. **Writes** the survivors as ranked, fully-formatted rows into a new dated "Staging" tab in the Policy Vault
6. **Emails** Marin a ranked summary with a link to the tab

No one has to remember to run anything. If it breaks, GitHub emails the account owner a failure notice.

---

## One-time setup (~30 minutes)

### 1. API keys (four secrets)

Go to this repo's **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Where to get it |
|---|---|
| `TAVILY_API_KEY` | tavily.com → dashboard → API keys (free tier: 1,000 searches/mo, plenty) |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys. **Use a TPP org account, not a personal one.** Expected cost: $2–10/month |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | See step 2 — paste the entire JSON file contents |
| `GMAIL_APP_PASSWORD` | Google Account → Security → 2-Step Verification → App passwords (requires 2FA on the sending account) |

And under **Variables** (same page, "Variables" tab):

| Variable | Value |
|---|---|
| `VAULT_SPREADSHEET_ID` | `1V2drIfK1Gn4pb5i_Uj5S7f4SV4jiV227Wt6JV7ouXEE` |
| `EMAIL_FROM` | the Gmail address that owns the app password |
| `EMAIL_TO` | Marin's email address |

### 2. Google service account (lets the script write to the Sheet)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project (e.g. `tpp-policy-scanner`)
2. **APIs & Services → Enable APIs** → enable **Google Sheets API**
3. **IAM & Admin → Service Accounts → Create service account** (any name, no roles needed)
4. Open the service account → **Keys → Add key → JSON** → download the file
5. Paste the file's entire contents into the `GOOGLE_SERVICE_ACCOUNT_JSON` secret
6. **Share the Policy Vault spreadsheet** with the service account's email address (looks like `something@project.iam.gserviceaccount.com`) as an **Editor** — this is the step everyone forgets

### 3. Test it

Actions tab → **Monthly Policy Scan** → **Run workflow** (the `workflow_dispatch` button). Watch the log. First run should be done in ~10–15 minutes. Check the Sheet for the new tab and the inbox for the email.

---

## Files

| File | What it is |
|---|---|
| `scanner/scan.py` | The whole pipeline |
| `scanner/sources.yaml` | The source registry — **edit this to add/remove sources**, no code changes needed |
| `scanner/prompts.py` | The Claude prompts (extraction, dedupe, verification, ranking) |
| `.github/workflows/monthly-scan.yml` | The schedule (cron, currently 1st of month ~8 AM Mountain) |

## Changing things

- **Add a source**: add a line to `sources.yaml`. Done.
- **Change the schedule**: edit the `cron:` line in `.github/workflows/monthly-scan.yml` (times are UTC; Mountain is UTC-6/-7)
- **Change the email recipient**: edit the `EMAIL_TO` repository variable
- **Run it off-cycle**: Actions tab → Run workflow

## Design principles (read before "improving" it)

1. **The Google Sheet is the single source of truth.** This scanner only ever *adds staging tabs*; it never edits the main Policies tab. Humans promote rows from staging to the vault.
2. **Verification beats volume.** An idea that's already Utah law staged into the vault costs the team credibility with legislators. The verification step deliberately kills a large share of candidates. 5 verified ideas > 25 unverified ones.
3. **Never guess numbers.** Every cost/impact figure must show its math and source in the row. Anything unverifiable gets an explicit `FLAG: verify X` note instead of a made-up number.

## History

Built July 2026 by Becca Linford (TPP intern) with Claude Code, replacing a manual Claude session workflow. The scan protocol was developed and validated in the July 2026 scan (25 ideas staged; 10 candidates killed as already-enacted law).
