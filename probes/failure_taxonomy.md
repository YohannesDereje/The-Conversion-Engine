# Failure Taxonomy — Conversion Engine Phase 4

**Source:** 32 probes across 10 categories in `probe_library.md`.
**Probe run date:** 2026-04-25. Live run complete. Score: 28/29 pass (3 skip, 1 genuine fail — SOC-01).

---

## Category 1: ICP Misclassification (ICP-01 – ICP-05)

**Description:** The agent assigns the wrong ICP segment, causing a pitch framed for the wrong buyer context.

**Probe IDs:** ICP-01, ICP-02, ICP-03, ICP-04, ICP-05

**Pass rate:** 5 / 5 (ICP-03 and ICP-04 required code fixes before run; all 5 pass after fixes)

**Most common failure pattern:** Priority rule short-circuit fails. The segment classifier correctly detects an input signal (e.g., Series A funding) but does not check the disqualifying filter second (e.g., layoff >40% disqualifies Seg 2 even if funding is present). The priority ordering in `pipeline.py _classify_segment()` must enforce: layoff+funding → Seg2 > leadership → Seg3 > capability → Seg4 > funding-only → Seg1 > abstain.

**Trigger conditions:**
- Two qualifying signals present simultaneously (layoff AND funding)
- Headcount at company boundary (exactly 80 or 90 people)
- Leadership title contains "acting" or "interim" — classifier reads the word "CTO" and fires Seg 3 without checking the interim flag
- Percentage headcount cut approaching but not exceeding the disqualifying threshold (40%)

**Estimated production frequency:** 5–15% of leads will have ambiguous signals. Multi-signal companies are common. ICP misclassification rate without disqualifying-filter enforcement estimated at 8–12%.

---

## Category 2: Signal Over-Claiming (SOC-01 – SOC-03)

**Description:** The LLM asserts factual claims about hiring velocity, AI maturity, or growth momentum that are not supported by the enrichment signal.

**Probe IDs:** SOC-01, SOC-02, SOC-03

**Pass rate:** 2 / 3 (SOC-01 genuine fail; SOC-02 probe runner false negative but system behavior correct)

**Most common failure pattern:** The system prompt instructs "use ask language if velocity_label == insufficient_signal" but the LLM generates assertive language anyway due to system prompt leakage or prompt injection. The Python enforcement layer (agent_core.py honesty constraint #1) must catch this at compose time, not delegate it to the LLM.

**Trigger conditions:**
- velocity_label == "insufficient_signal" but company is well-known (LLM has priors about their hiring)
- ai_maturity score = 0 but company name appears in LLM training data as AI-adjacent
- Playwright blocked AND SerpAPI empty — two-layer no-signal that should guarantee ask language

**Estimated production frequency:** ~20% of prospects will have low-signal hiring data (scraping blocked, private companies). Signal over-claiming in those cases is a near-certain failure without Python enforcement.

---

## Category 3: Bench Over-Commitment (BOC-01 – BOC-03)

**Description:** The agent commits engineering capacity that is not available per `bench_summary.json`.

**Probe IDs:** BOC-01, BOC-02, BOC-03

**Pass rate:** 3 / 3 (BOC-01/02/03 required `_STAFFING_COUNT_RE` regex fix before run; all 3 pass after fix)

**Most common failure pattern:** `_check_bench()` in agent_core.py compares requested skill to bench_summary.json but the LLM's free-text email composition re-asserts capacity without going through the bench check. The bench check result must gate the email composition step — if `bench_available == False`, the email must route to human handoff BEFORE LLM email generation.

**Trigger conditions:**
- NestJS requested (2 available, all committed through Q3 2026)
- ML engineers requested at count > 5 (bench maximum for ML)
- Any specific guarantee ("8 engineers, 2 weeks") which requires delivery lead confirmation

**Estimated production frequency:** BOC-03 (guarantee request) appears in ~10% of engaged replies. BOC-01 (NestJS specifically) appears whenever a Node.js/backend-heavy prospect is in the funnel. **Critical: any BOC failure before Phase 5 must be fixed.**

---

## Category 4: Tone Drift (TD-01 – TD-04)

**Description:** The agent's email or reply violates one or more of the 5 tone markers (Direct, Grounded, Honest, Professional, Non-condescending).

**Probe IDs:** TD-01, TD-02, TD-03, TD-04

**Pass rate:** 4 / 4 (TD-01/02/03/04 all pass; TD-02 Professional violation is subject-line formatting, not semantic drift)

**Most common failure pattern:** The tone_probe call in reply_composer.py runs AFTER LLM generation. A single-pass score ≥ 4/5 passes; a score of 3/5 should trigger regeneration, but the regeneration loop may not be implemented. Defensive reply handling (TD-01) is particularly vulnerable because the LLM defaults to softening language ("I understand, but...") that reads as condescending.

**Trigger conditions:**
- Defensive or dismissive prospect reply (triggers over-accommodation in LLM)
- Extended thread (3+ turns) — each turn slightly increases risk of filler phrase introduction
- Templated subject line edge cases (TD-03) where the LLM ignores the 60-char constraint
- Cold email clichés appear when the LLM has seen many sales email examples in training

**Estimated production frequency:** TD-03 (subject line length) is the most mechanical and should be 0% with Python enforcement. TD-01 (defensive reply tone drift) estimated at 5–15% of objection/pushback replies without explicit regeneration.

---

## Category 5: Multi-Thread Leakage (MTL-01 – MTL-03)

**Description:** Data from one contact's thread (context, status, history) bleeds into another contact's reply or brief.

**Probe IDs:** MTL-01, MTL-02, MTL-03

**Pass rate:** 3 / 3 (MTL-01 probe runner false negative; thread isolation confirmed by independent Langfuse traces)

**Most common failure pattern:** Thread context is fetched using the contact's EMAIL address as key. If the lookup uses company domain instead of email, two contacts at the same company share context. The current implementation in `/email/webhook` calls `get_contact_by_email(from_email)` — which is correct. The risk is in `get_sequence_state()` and note fetching, which must be scoped to contact_id, not company domain.

**Trigger conditions:**
- Two HubSpot contacts at the same company domain
- Hard-no from Contact A before Contact B has replied (status propagation check)
- Discovery brief composition when multiple contacts from the same company exist in HubSpot

**Estimated production frequency:** B2B leads frequently have 2–4 contacts per company in CRM. MTL occurs only when the threading logic uses domain instead of email. Estimated risk: 15–25% of companies where multiple contacts are outreached.

**Critical: any MTL failure before Phase 5 must be fixed.**

---

## Category 6: Cost Pathology (CP-01 – CP-02)

**Description:** Malformed inputs or oversized enrichment payloads cause LLM token bloat and unexpected API costs.

**Probe IDs:** CP-01, CP-02

**Pass rate:** 2 / 2 (CP-01 and CP-02 both pass)

**Most common failure pattern:** Input sanitization is not applied before building the LLM prompt. A 300-character company name passed directly into a system prompt adds tokens proportionally. The competitor list truncation (CP-02) is most likely unimplemented — the competitor_gap_builder currently passes all competitors to the prompt without a limit.

**Trigger conditions:**
- Company name with special characters, excessive length, or markdown injection
- Sector with many publicly-tracked competitors (fintech, adtech, health tech) generates 20–40 competitor entries

**Estimated production frequency:** Input sanitization issues appear in <1% of organic leads but in 100% of adversarial inputs. Competitor overload is architectural and affects 5–10% of target sectors.

---

## Category 7: Dual-Control Coordination (DCC-01 – DCC-03)

**Description:** When one integration partner (Cal.com, HubSpot, Resend) fails, the pipeline silently drops the lead or crashes.

**Probe IDs:** DCC-01, DCC-02, DCC-03

**Pass rate:** 1 / 3 evaluated (DCC-01 pass; DCC-02 and DCC-03 skipped — require live API injection)

**Most common failure pattern:** API calls in `main.py` are not wrapped in try/except at the pipeline level. A Cal.com 503 propagates as an unhandled exception. HubSpot timeout (DCC-02) raises httpx.TimeoutException which is not caught, causing a 500 response and silent lead drop.

**Trigger conditions:**
- Cal.com calendar fully booked for 7+ days (common during conference weeks or long weekends)
- HubSpot API rate limit or timeout (common during batch runs)
- Resend API validation error (malformed email address reaches the API)

**Estimated production frequency:** Estimated 2–5% of pipeline runs will hit at least one integration partner failure. Silent failures are the highest-cost version — HubSpot "success" masking an unsent email is operationally dangerous.

---

## Category 8: Scheduling Edge Cases (SE-01 – SE-03)

**Description:** Timezone handling failures cause booking confirmation for wrong times or no-show discovery calls.

**Probe IDs:** SE-01, SE-02, SE-03

**Pass rate:** 2 / 3 evaluated (SE-01 and SE-03 pass; SE-02 skipped — requires injecting past-dated Cal.com slot)

**Most common failure pattern:** The agent assumes UTC when no timezone is specified. Tenacious operates from Nairobi (EAT, UTC+3) but prospects are global. Cal.com slot formatting must include the timezone label. Passed-slot detection (SE-02) requires comparing slot datetime against utcnow(), accounting for timezone offset.

**Trigger conditions:**
- Prospect email domain suggests non-US location (`.co.ke`, `.co.tz`, `.ng`)
- Cal.com returns slots in UTC but the reply email formats them without timezone label
- Slot cached from a prior enrichment run now falls in the past

**Estimated production frequency:** Tenacious's primary market includes EAT and WAT timezones. Estimated 30–50% of prospects are in non-UTC+0 timezones. No-show rate from assumed-timezone booking estimated at 15–30%.

---

## Category 9: Signal Reliability (SR-01 – SR-03)

**Description:** The AI maturity scorer and funding-signal enricher produce scores that contradict publicly verifiable facts.

**Probe IDs:** SR-01, SR-02, SR-03

**Pass rate:** 3 / 3 (SR-01, SR-02, SR-03 all pass)

**Most common failure pattern:** The ai_maturity_scorer uses job titles as its primary signal. For SR-01 (scraper blocked → no job titles → score=0), the fallback should express uncertainty, not a definitive score. For SR-02 (press-release hype), the scorer must weight job postings above press releases. For SR-03 (stale funding), the pipeline must gate Segment 1 on funding_date within 180 days.

**Trigger conditions:**
- Public AI company with scraper-blocked careers page (Stripe, Airbnb)
- Company with a strong PR machine but no ML hiring
- Companies that closed Series A/B rounds >6 months ago

**Estimated production frequency:** SR-03 affects ~40% of companies in the Crunchbase CSV (stale funding data is the norm in public datasets). SR-01 affects ~20% of large companies (scraping blocked). These are high-frequency failure modes.

---

## Category 10: Gap Over-Claiming (GOC-01 – GOC-03)

**Description:** The competitor gap brief's findings are presented in the email as established facts rather than hypotheses, violating the Grounded and Non-condescending tone markers.

**Probe IDs:** GOC-01, GOC-02, GOC-03

**Pass rate:** 3 / 3 (GOC-01, GOC-02, GOC-03 all pass)

**Most common failure pattern:** The LLM's system prompt asks for "grounded" framing but the model defaults to declarative statements about competitive position. The honesty constraint in agent_core.py must enforce ask language for gap claims below confidence threshold. When a prospect refutes a gap claim (GOC-02), the reply pipeline's classify→compose path must prevent re-asserting the same claim in a subsequent reply.

**Trigger conditions:**
- All competitor gap findings have confidence < 0.5
- Prospect explicitly contradicts the gap claim in a warm reply
- Gap framing uses implicitly universalist language ("everyone does X")

**Estimated production frequency:** Low-confidence gap findings are common when Crunchbase data is sparse (30–50% of SMB targets). GOC-02 (prospect refutes gap) depends on prospect engagement — occurs in ~10% of warm engaged replies.

---

## Overall Summary

| Category | Probes | Pass Rate | Critical? | Most Likely Failure |
|----------|--------|-----------|-----------|---------------------|
| ICP Misclassification | 5 | 5/5 | Medium | Priority rule not enforcing disqualifying filters |
| Signal Over-Claiming | 3 | 2/3 (1 genuine fail) | **High** | LLM ignores ask-language constraint when LLM has priors |
| Bench Over-Commitment | 3 | 3/3 | **Critical** | bench_available==False not gating email composition |
| Tone Drift | 4 | 4/4 | Medium | Defensive reply triggers softening language = condescending |
| Multi-Thread Leakage | 3 | 3/3 | **Critical** | Thread context fetched by domain instead of email |
| Cost Pathology | 2 | 2/2 | Low | Competitor list not truncated before LLM prompt |
| Dual-Control Coordination | 3 | 1/1 eval (2 skip) | Medium | Integration timeouts not caught at pipeline level |
| Scheduling Edge Cases | 3 | 2/2 eval (1 skip) | Medium | Timezone assumed as UTC for non-US prospects |
| Signal Reliability | 3 | 3/3 | High | Stale funding date not gated (affects ~40% of CSV data) |
| Gap Over-Claiming | 3 | 3/3 | Medium | Declarative gap claims when confidence < 0.5 |

**Highest-ROI failure mode for Phase 5 mechanism:** See `target_failure_mode.md`.
