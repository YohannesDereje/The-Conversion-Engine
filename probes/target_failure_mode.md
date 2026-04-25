# Target Failure Mode — Phase 5 Mechanism Design

**Prepared:** 2026-04-25  
**Source:** `probes/probe_library.md` live run + `probes/failure_taxonomy.md`

---

## 1. Failure Mode Name

**Signal over-claiming via ungrounded assertions when hiring velocity signal is weak or absent.**

Specific form: when `velocity_label` is `"low"` or `"insufficient_signal"` AND the `weak_hiring_velocity_signal` honesty flag is set, the LLM still generates assertive declarative claims about the prospect's hiring trajectory ("you're aggressively expanding your ML team", "your rapid hiring growth indicates...") in the outreach email body — despite the honesty constraint in the system prompt.

---

## 2. Category

**Signal Over-Claiming** (Category 2, probe IDs: SOC-01, SOC-02, SOC-03)

---

## 3. Observed Trigger Rate from Probe Library

- **SOC-01:** FAIL — `weak_hiring_velocity_signal` flag set, Grounded tone marker fails. LLM asserted hiring momentum with only 2 open roles.
- **SOC-02:** PASS (probe runner sensitivity) — flag names used (`weak_hiring_velocity_signal`) differ from probe's expected string (`insufficient_signal`); system behavior is correct but the honesty constraint is prompt-only.
- **SOC-03:** PASS — pipeline did not assert AI investment when `ai_maturity=0`.

**Trigger rate: 1/3 probes fired the failure (SOC-01). SOC-02 reveals the underlying structural risk: the honesty constraint is a soft prompt instruction, not a Python-enforced post-generation check.**

From `failure_taxonomy.md` estimated production frequency: ~20% of prospects have low-signal or no-signal hiring data (scraping blocked, private companies, careers page not public). In those 20%, the current soft constraint is insufficient — the LLM will draw on its own priors about the company rather than limiting itself to observed signal.

---

## 4. Business-Cost Derivation

**ACV range (source: `seed/baseline_numbers.md`, revised Feb 2026):** `$[ACV_MIN]–$[ACV_MAX]` talent outsourcing; `$[PROJECT_ACV_MIN]–$[PROJECT_ACV_MAX]` project consulting.

**How many deals it puts at risk per month:**

Assume a Segment 1/2 outreach run of 100 leads/month.
- 20 leads (20%) will have low/no hiring signal.
- Of those 20, the current system over-claims hiring momentum in the email body.
- A prospect who receives a demonstrably false claim about their own job board checks it in under 10 seconds. Instant credibility loss → immediate hard-no.
- Cold email reply rate baseline: 1–3% (source: `seed/baseline_numbers.md`, LeadIQ 2026). With over-claiming, hard-no rate from those 20 low-signal leads: estimated 80%.
- That is 16 leads that could have been engaged exploratorily instead destroyed with a false claim.
- At a discovery-call-to-proposal conversion of 30–50% (source: `seed/baseline_numbers.md`) and ACV midpoint of `$[ACV_MIDPOINT]` (source: `seed/baseline_numbers.md`, revised Feb 2026):
  - Potential value of 16 exploratory-angle leads = 16 × 2% reply rate × 40% call conversion × `$[ACV_MIDPOINT]` = **`$[EXPECTED_MONTHLY_ACV_AT_RISK]`/month in expected ACV lost solely to signal over-claiming.**

**Brand-reputation impact:**

A single "you're aggressively growing your ML team" claim sent to a CTO who just announced a freeze (visible to all on LinkedIn) can generate a public callout. At 1 negative public post reaching ~500 LinkedIn peers in the same target segment: estimated 15–30 leads who remove Tenacious from consideration without ever replying. That is the highest-severity version of this failure and it is not captured in the ACV calculation above.

---

## 5. Why This Failure Mode Is Highest-ROI to Fix vs. Runner-Up

**Runner-up considered: ICP misclassification (ICP-03/ICP-04)** — fixed in Phase 4 with Python-enforced filters. No longer an active failure mode.

**SOC-01 signal over-claiming is highest-ROI for three reasons:**

1. **Frequency:** Affects ~20% of leads in every run (scraper block rate is structural, not occasional). ICP misclassification post-fix affects <5% of leads.

2. **Severity of consequence:** A false factual claim about a prospect's own operations is immediately verifiable and immediately damaging. An ICP misclassification produces a slightly misaligned pitch — bad, but recoverable. A false "you're hiring aggressively" to a company in freeze destroys trust irreversibly.

3. **Mechanism gap:** All other failing probes were fixed with Python enforcement (interim CTO filter, decimal normalization, `_STAFFING_COUNT_RE`). SOC-01 is the only probe where the fix requires a **new architectural layer** — a post-generation scan — not a simple classifier fix. This makes it the correct target for Phase 5 mechanism design.

**SOC-02 is a structural early warning:** even though the probe passed on a technicality (flag name sensitivity), it revealed that the honesty constraint is prompt-only for the `weak_hiring_velocity_signal` case. Without Python enforcement, any model with strong company-name priors will override the prompt instruction.

---

## 6. Proposed Mechanism Design for Phase 5

**Mechanism name:** Signal-confidence-aware phrasing (Mechanism Option A from challenge spec)

**Description:**

After `compose_outreach()` generates the email body, a post-generation Python scan checks whether any of the `weak_*_signal` honesty flags are set. If they are, the scan runs a regex/keyword match against the composed email body to detect assertive hiring and growth claims. If assertive patterns are found, the email body is either:

- (a) **Rewritten** — the assertive sentence is replaced with the ask-language equivalent, OR
- (b) **Blocked for regeneration** — compose_outreach() is called again with an explicit injected constraint: "Do not make any assertive claim about hiring velocity. You MUST use ask language."

**Hyperparameters:**
- Assertive pattern list (regex): `\b(aggressively|rapidly|accelerat\w+|scaling|expand\w+)\s+(hir\w+|grow\w+|team)`, `\byou('re| are)\s+(grow\w+|hir\w+)`, `\b(strong|impressive|significant)\s+(hiring|growth|momentum)`
- Flags that trigger the scan: `weak_hiring_velocity_signal`, `weak_ai_maturity_signal`
- Regeneration limit: max 2 attempts before falling back to ask-language template

**Toggle:** environment variable `MECHANISM_SIGNAL_AWARE_PHRASING=true/false` (default: `true` in production, `false` for ablation baseline)

**Expected delta:** SOC-01 probe moves from FAIL to PASS. Estimated production impact: reduces false-assertion rate from ~80% of low-signal leads to <5% (only cases where the regex misses a novel assertive pattern).
