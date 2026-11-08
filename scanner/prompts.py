"""Claude prompts for each pipeline stage. Kept in one file so non-engineers
can tune the instructions without touching pipeline logic."""

EXTRACT = """You are a policy analyst for The Policy Project (TPP), a Utah nonprofit.
Below are recent search results from policy sources. Extract every distinct,
actionable STATE-LEVEL policy idea that Utah could adopt.

Rules:
- An idea must be something the Utah Legislature, a Utah agency, or Utah Medicaid
  could actually do (statute, rule, appropriation, waiver). Skip federal-only ideas,
  vague aspirations ("invest in kids"), and pure research findings with no ask.
- Phrase each idea verb-first: "Fund...", "Require...", "Cap...", "Create...".
- Note which source it came from (URL).

The objectives IN SCOPE for this scan, each with its numbered sub-goals.
An idea is in scope ONLY if it would directly advance one of these sub-goals;
SKIP anything that does not (e.g. general economic development, water,
land use or energy policy with no clear link to a sub-goal). Objective 5 is
the catch-all for in-scope ideas that fit no other objective.
{objectives}

Return JSON: a list of objects with keys:
  idea (string, 1-2 sentences, verb-first)
  objective (string, the objective's full name as written above)
  target (string, the number of the sub-goal it advances most, e.g. "1.3")
  source_url (string)
  source_label (string)

Search results:
{results}
"""

DEDUPE = """You maintain TPP's Policy Vault. Below is (A) a list of NEW candidate
policy ideas and (B) the summaries of EVERY existing vault entry and prior staging row.

For each candidate, decide: is it substantially covered by an existing entry?
"Substantially covered" = same mechanism aimed at the same population. A narrower or
complementary variant of an existing idea counts as covered unless it uses a clearly
different lever (e.g., private-insurance mandate vs. Medicaid coverage are different).

Return JSON: a list of objects with keys:
  idea (copied from input)
  duplicate (true/false)
  matches (string: the existing entry it duplicates, or "")

(A) Candidates:
{candidates}

(B) Existing entries:
{existing}
"""

VERIFY = """You are verifying whether Utah has ALREADY enacted a policy idea.
Below are web search results about the idea's status in Utah.

Historically ~40% of promising candidates turn out to already be Utah law — be
skeptical. If Utah passed a bill doing substantially this, the idea is DEAD even
if advocates want it expanded. If Utah did a narrow version, the idea is LIVE only
if reframed as the specific remaining gap (rewrite it that way).

Return JSON with keys:
  status ("live" | "dead" | "uncertain")
  reasoning (1-2 sentences citing the specific bill/statute if dead)
  reframed_idea (string; the gap-focused rewrite if partially covered, else original)
  utah_law_cite (string; bill number or code section if found, else "")

Idea: {idea}

Search results:
{results}
"""

BUILD_ROW = """Build a Policy Vault row for this verified policy idea.
Follow TPP's verification protocol strictly. Never invent a number, bill,
code section or past attempt.

- Utah Code: only a le.utah.gov URL that appears in the research below.
  Otherwise "FLAG: verify Utah Code on le.utah.gov".
- Past attempts: only Utah bills found in the le.utah.gov or web research
  below — bill number, year, outcome, URL. If none: "None found in
  le.utah.gov search — FLAG: verify".
- Estimated cost: give a range, show the math, cite each source inline
  (fiscal notes or other states' comparable programs from the cost research
  below), and end with "Conf: LOW", "Conf: MED" or "Conf: HIGH".
- People affected: use the Utah statistics below FIRST, citing the stat's
  source; show the math; end with "Conf: LOW/MED/HIGH". Population and
  demographic figures may come only from those statistics or from a primary
  source in the research (Census/ACS, ibis.utah.gov, DOPL, schools.utah.gov
  (USBE), hwic.utah.gov). Otherwise "FLAG: verify <what> with <agency>".
- Type of cost / cost notes: say whether a cost exists, one-time vs ongoing,
  mechanism (appropriation / tax expenditure / mandate / regulation),
  funding source, who ultimately bears it, and cost shift vs net new.
- Target / Barrier, Type of change, Type of cost: use a value from the
  Vault's lists below when one fits; leave target_barrier blank if none does.
- Legal interpretation claims: mark "[HYPOTHESIS — requires counsel review]".

Idea: {idea}
Objective: {objective}
Source: {source_url}
Utah-law check: {verification}

Vault values already in use:
{options}

Law and bill research:
{law_research}

Cost research:
{cost_research}

Utah statistics (metric | value | year | source):
{stats}

Return JSON with keys (matching vault columns):
  objective, target_barrier, idea_summary, idea_source, utah_code, past_attempts,
  more_info, type_of_change, type_of_cost, cost_notes, estimated_cost,
  cost_explanation, people_affected, impact_explanation, follow_up
"""

RANK = """Rank these staged policy ideas best-first using TPP's scoring rubric:
1. Values alignment: cross-political-spectrum framing potential; removes barriers for
   people in poverty, women, children, people of color
2. Impact: how many Utahns, how often (daily > monthly > yearly)
3. Feasibility: realistic path through Utah's legislature; public + private support
4. Evidence: data-proven outcomes; other states' success

Return JSON: a list with one object per idea, with keys:
  id (integer: the idea's number from the list below)
  rank (integer, 1 = best, no ties)
  rationale (one sentence for the Notes column, e.g. "RANK 3 — big reach, hard politics")

Ideas (numbered):
{ideas}
"""
