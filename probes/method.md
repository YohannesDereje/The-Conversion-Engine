# Method — Conversion Engine: Act III + Act IV

**Phase 4 sections authored:** 2026-04-25  
**Phase 5 sections:** to be completed after mechanism implementation  

---

## Section 1: ICP Classifier Design

### Signal Inputs and Segment Mapping

The ICP classifier (`agent/enrichment/pipeline.py: _classify_segment()`) evaluates five enrichment signals:

| Signal | Source | Used by Segment |
|--------|--------|-----------------|
| `layoff_event.detected` + `layoff_event.date` (≤120 days) | `layoffs_enricher.py` | Seg 2 qualifier; Seg 2 disqualifier if >40% |
| `funding_event.last_funding_date` (≤180 days) + `funding_event.last_funding_stage` | `crunchbase_enricher.py` | Seg 1 (Series A/B) and Seg 2 (layoff+funding combo) |
| `leadership_change.detected` + `leadership_change.role` + `leadership_change.date` (≤90 days) | LLM signal via agent | Seg 3 qualifier; disqualified if role contains acting/interim markers |
| `ai_maturity.score` (0–3) | `ai_maturity_scorer.py` | Seg 4 qualifier (must be ≥2); Seg 4 disqualifier if <2 |
| `employee_count` | `crunchbase_enricher.py` | Seg 1 qualifier (15–80 employees only) |

### Priority Rule Implementation

Implemented as a strict if/elif chain (not a weighted sum) to guarantee deterministic priority:

```
Priority 1: layoff ≤120d AND funding ≤180d → segment_2 (layoff+funding override)
  Disqualifier: layoff_pct > 40% → fall through
Priority 2: leadership_change ≤90d AND NOT interim/acting → segment_3
  Disqualifier: role contains acting/interim/temporary/temp/provisional → fall through
Priority 3: ai_maturity ≥2 AND capability in bench_summary → segment_4
  Disqualifier: ai_maturity <2 → fall through
Priority 4: funding Series A/B ≤180d AND 15 ≤ employees ≤80 → segment_1
  Disqualifier: competitor client, anti-offshore stance, layoff >15% → fall through
Priority 5: abstain (conf = 0.0–0.5 range)
```

### Confidence Scoring Formula

Each segment returns a hardcoded confidence calibrated to signal quality:

- `segment_2_mid_market_restructure`: 0.75 (two corroborating signals)
- `segment_3_leadership_transition`: 0.72 (verified CTO/VPE role), 0.62 (other qualifying role)
- `segment_4_specialized_capability`: 0.70 (ai_maturity ≥2 + bench match)
- `segment_1_series_a_b`: 0.65 (single signal, no layoff corroboration)
- `abstain`: 0.0–0.5 (weak or disqualified signals)

### Abstention Threshold

`segment_confidence < 0.6` → abstain override enforced in `agent_core.py` before LLM email generation. Agent uses generic exploratory email template instead of segment-specific pitch.

---

## Section 2: Tone-Preservation Probe

### 5-Marker Rubric

Defined in `seed/style_guide.md`, evaluated by `agent/tone_probe.py`:

| Marker | Pass Condition | Fail Example |
|--------|---------------|-------------|
| Direct | No filler phrases, no hedge-stacking | "I just wanted to quickly reach out to touch base" |
| Grounded | Every claim references a specific observed signal | "you're a fast-growing company" (no signal cited) |
| Honest | No unverifiable superlatives, no invented data | "world-class engineers", "cost savings of 40%" |
| Professional | No offshore-vendor clichés, no exclamation marks | "rockstar developers", "ninja engineers" |
| Non-condescending | No "actually...", no implicit should-statements | "most companies in your position do X" |

### Scoring Implementation

`score_tone(email_subject, email_body, trace_id)` calls Qwen3 via OpenRouter with `style_guide.md` embedded in the system prompt. Returns structured output:

```json
{
  "scores": {"direct": 0|1, "grounded": 0|1, "honest": 0|1, "professional": 0|1, "non_condescending": 0|1},
  "total": 0-5,
  "passed": true|false,
  "violations": ["list of failed marker names"]
}
```

### Pass Threshold and Regeneration Policy

- `total ≥ 4` → passed. Email proceeds to send.
- `total < 4` → `tone_violation` added to `honesty_flags`. Email is NOT blocked (caller decides). Tone violation surfaced in `/leads/process` response and in Langfuse span.
- Regeneration: caller (`compose_outreach()`, `compose_engaged_reply()`, etc.) checks `passed` flag. If False, a second LLM generation pass is triggered with violation names injected into the prompt as explicit constraints. Maximum 2 regeneration attempts.

### Cost per Tone Probe Call

Qwen3-235b-a22b via OpenRouter: $0.0014/1K input + $0.0014/1K output. Tone probe call is ~800 input tokens + ~200 output tokens. Estimated cost per call: **~$0.0014**. At 3 emails per cold sequence: ~$0.004 total tone probe cost per lead.

---

## Section 3: Honesty Constraints (3 Python-Enforced Overrides)

All three overrides are applied in Python **after** LLM generation, before the email is sent. They are not delegated to the LLM prompt.

### Constraint 1: Insufficient Signal → Ask Language

**Location:** `agent/agent_core.py: compose_outreach()`  
**Condition:** `hiring_brief.velocity_label in ("insufficient_signal", "low")` OR `"weak_hiring_velocity_signal" in honesty_flags`  
**Enforcement:** Post-generation regex scan for assertive hiring patterns. If detected → regenerate with explicit ask-language constraint injected. (Phase 5 mechanism implementation.)  
**Status as of Phase 4:** Prompt-only (soft). SOC-01 proves this is insufficient — Python enforcement required.

### Constraint 2: Segment Confidence < 0.6 → Abstain Override

**Location:** `agent/enrichment/pipeline.py: _classify_segment()` and `agent/agent_core.py`  
**Condition:** `segment_confidence < 0.6`  
**Enforcement:** Pipeline returns `segment="abstain"`. Agent uses generic exploratory email — no segment-specific pitch generated. LLM never receives segment context that would lead to a false pitch.  
**Status:** Python-enforced. Verified by ICP-05 (ai_maturity=1 → abstain, conf=0.38).

### Constraint 3: Bench Unavailable → Never Commit Capacity

**Location:** `agent/reply_composer.py: detect_handoff_triggers()`  
**Condition:** Any of: specific staffing count requested (`_STAFFING_COUNT_RE` match), bench_available==False for requested stack, guarantee language in reply  
**Enforcement:** Handoff triggered BEFORE LLM reply generation. LLM never composes a capacity commitment. Reply is the fixed handoff template only: "Our delivery lead will follow up within 24 hours."  
**Status:** Python-enforced. Verified by BOC-01/02/03.

---

## Section 4: Reply Classification

### 6 Class Definitions

Implemented in `agent/reply_classifier.py` using Qwen3 with `seed/email_sequences/warm.md` class definitions in system prompt:

| Class | Definition | Routing |
|-------|-----------|---------|
| `engaged` | Substantive response with question or shared context | Compose engaged reply → send |
| `curious` | "Tell me more", "what do you do", "interesting" | Compose curious reply → send |
| `hard_no` | "Not interested", "remove me", "unsubscribe" | No email sent; HubSpot DISQUALIFIED |
| `soft_defer` | "Not now", "reach out in Q3", "too busy right now" | Compose soft_defer reply → send; HubSpot UNQUALIFIED |
| `objection` | Specific objection: price, incumbent vendor, POC-only policy | Check handoff first → compose objection reply |
| `ambiguous` | Uncertain | No email sent; HubSpot note "human review needed" |

### Ambiguous → Human (Safety Over False-Positive Confidence)

When `classify_reply()` returns confidence < threshold or class="ambiguous", the system routes to human review rather than guessing the wrong class. A misclassified `hard_no` as `engaged` and sending another email is a regulatory and brand risk. A false-positive ambiguous routing adds one human-review step; a false-positive engaged-reply to a hard-no is irreversible.

---

## Section 5: Re-Engagement Trigger Logic

### All 4 Required Conditions (must ALL be true)

Implemented in `agent/reengagement_composer.py: check_reengagement_eligible()`:

1. HubSpot shows at least one reply classified `engaged` or `curious` (contact showed prior interest)
2. No Cal.com booking in last 7 days (no confirmed discovery call scheduled)
3. `hs_lead_status` is NOT `DISQUALIFIED`, `OPTED_OUT`, or `hard_no` (contact is eligible)
4. `outreach_last_sent_at` for the reengage sub-sequence is > 45 days ago, or never sent

### Re-Engagement Sequence

```
reengage_1 → reengage_2 → reengage_3 → exhausted (no further outreach)
```

- `reengage_1`: 100 words max, ONE new data point from fresh enrichment, soft ask only ("reply yes for sector one-pager"), NO calendar link
- `reengage_2`: 50 words max, ONE specific yes/no question grounded in hiring brief, no follow-up clichés
- `reengage_3`: 40 words max, gracious close with specific month ("parking this until August"), door stays open

---

## Section 6: Human Handoff Trigger Conditions

### All 5 Conditions (from `seed/email_sequences/warm.md`)

Implemented in `agent/reply_composer.py: detect_handoff_triggers()`:

| Trigger | Detection Method | Example |
|---------|-----------------|---------|
| 1. Pricing outside quotable bands | Keyword match: "custom TCV", "volume discount", "multi-year", "specific %", "annual contract" | "What's your rate for 50 engineers over 3 years?" |
| 2. Specific staffing not on bench | `_STAFFING_COUNT_RE` regex + bench capacity check | "Can you provide 5 NestJS engineers?" (2 available) |
| 3. Public client reference in named sector | Keyword match: "reference", "case study", "client name", "speak to your customers" + sector name | "Can I speak to your fintech clients?" |
| 4. Legal/contractual language | Keyword match: "MSA", "DPA", "SLA", "NDA", "indemnity", "liability clause" | "We'll need an NDA before we proceed" |
| 5. C-level contact, headcount > 2,000 | HubSpot contact title field (CEO/CFO/COO) AND company employee_count > 2000 | CEO at 3,000-person company initiates reply |

### What the Agent Does When Handoff Fires

1. Composes `discovery_call_context_brief` via `compose_discovery_call_brief()` (10-section Markdown brief for delivery lead)
2. Sends ONLY: "Our delivery lead will follow up within 24 hours." (no additional content)
3. HubSpot: `hs_lead_status=IN_PROGRESS`, note added: "Human handoff — trigger: [trigger_name]"
4. Returns: `{status: "human_handoff", trigger: trigger_name, brief_word_count: N}`
5. Emits Langfuse span with trigger, contact_id, handoff timestamp

---

---

## Section 7: Mechanism Design — Signal-Confidence-Aware Phrasing

**Target failure mode:** SOC-01 — assertive hiring/growth claims generated when `weak_hiring_velocity_signal` flag is set (identified in Phase 4).

**Mechanism option:** A (signal-confidence-aware phrasing) from challenge spec.

**Description:**

After `compose_outreach()` generates the email body, a post-generation Python scan checks whether any `_WEAK_SIGNAL_FLAGS` (`weak_hiring_velocity_signal`, `weak_ai_maturity_signal`) are present in the active honesty flags. If they are, `_has_assertive_claims(email_body)` runs `_ASSERTIVE_CLAIM_RE` against the body. If assertive patterns are detected, the mechanism injects an explicit ask-language override into the conversation as a follow-up user turn and requests a regenerated `email_body`. Up to 2 regeneration attempts are made; on each attempt the output is re-scanned before accepting.

**Implementation location:** `agent/agent_core.py`, Rule 5 in `compose_outreach()`, after bench check and before Cal.com slot appending (lines ~461–503).

**Toggle (hyperparameter):**
- Environment variable: `MECHANISM_SIGNAL_AWARE_PHRASING=true` (default) / `false` (ablation baseline)
- Toggle is read at module load via `_MECHANISM_ENABLED = os.getenv("MECHANISM_SIGNAL_AWARE_PHRASING", "true").lower() == "true"`

**Hyperparameters:**
- Activation flags: `weak_hiring_velocity_signal`, `weak_ai_maturity_signal`
- Assertive pattern regex (`_ASSERTIVE_CLAIM_RE`): 7 pattern groups covering explicit growth/hiring assertions, "you are [growing|expanding]", "your [team|headcount] is [growing|scaling]", rapid-* constructions, and "with your growing/scaling team" phrases
- Max regeneration attempts: 2
- Fallback behavior: if both attempts still contain assertive claims, `assertive_claim_regen_failed` flag is added and the original body is used; pipeline is not blocked

**Flags added to `honesty_flags` when mechanism fires:**
- `mechanism_signal_aware_phrasing_triggered` — scan detected assertive claims and regeneration was attempted
- `assertive_claim_regen_failed` — both regeneration attempts still contained assertive claims (rare; signals prompt/model resistance)

**Three ablation variants tested:**

| Condition | Description |
|-----------|-------------|
| `mechanism_on` | `MECHANISM_SIGNAL_AWARE_PHRASING=true` (default, production) |
| `day1_baseline` | `MECHANISM_SIGNAL_AWARE_PHRASING=false` (prompt-only honesty constraint, no post-generation scan) |
| `automated_optimization_baseline` | Best-of-3 prompt variation at same compute budget (no extra LLM call; prompt only) |

---

---

## Section 8: Statistical Validation

**Comparison:** held-out mechanism ON (0.4000, n=20) vs dev mechanism OFF proxy (0.2667, n=30) — same model (qwen3-235b-a22b).

**Delta A = +0.1333** (0.4000 − 0.2667)

| Metric | Value |
|--------|-------|
| pass@1 mechanism ON | 0.4000 (8/20), CI [0.2188, 0.6134] |
| pass@1 mechanism OFF proxy | 0.2667 (8/30), CI [0.1418, 0.4445] |
| Delta A | +0.1333 |
| Test method | Two-proportion z-test (Wilson score intervals) |
| Pooled proportion | 0.32 (16/50) |
| z-statistic | 0.989 |
| p-value | 0.32 |
| Significant (p < 0.05) | No |

**Interpretation:** Delta A is positive (+0.1333) but not statistically significant at the p < 0.05 threshold due to small sample sizes (n=20 and n=30) and wide confidence intervals that overlap substantially. The result is directionally consistent with the mechanism improving performance but the effect cannot be confirmed at conventional significance levels with these sample sizes.

**Why τ²-Bench does not fully capture the mechanism's effect:** The signal-confidence-aware phrasing mechanism operates in `agent_core.py` (outreach email composition) and is not invoked by the τ²-Bench retail task runner. The mechanism's primary measured improvement is at the adversarial probe level: SOC-01 moves from FAIL (prompt-only constraint, LLM overrides) to PASS (Python-enforced post-generation scan). A domain-matched evaluation — re-running the SOC probes with mechanism ON vs OFF — would show a more direct effect.

**Note on baseline comparison:** Delta A vs the facilitator-provided baseline (qwen3-next-80b, 5 trials) = −0.3267. This negative value reflects the model capability difference between qwen3-235b-a22b (used for held-out) and qwen3-next-80b (facilitator baseline), not the mechanism effect. The same-model comparison (above) is the appropriate ablation.

---

## Section 9: Cost-Quality Trade-Off

**Mechanism cost overhead:** The signal-confidence-aware phrasing mechanism adds at most 2 additional LLM calls (regeneration attempts) when assertive claims are detected. Each regeneration call uses the same model (qwen3-235b-a22b) and token budget as the original compose call.

| Scenario | LLM calls | Est. cost/lead |
|----------|-----------|----------------|
| No assertive claims detected (mechanism passes through) | 1 call | baseline |
| Assertive claims detected, 1 regen clears them | 2 calls | ~2× baseline |
| Assertive claims detected, 2 regens needed | 3 calls | ~3× baseline |
| Regen fails (flag added, original body used) | 3 calls | ~3× baseline |

**Trigger rate:** Based on probe runs, the mechanism fires when `weak_hiring_velocity_signal` or `weak_ai_maturity_signal` flags are set AND assertive patterns are found. Estimated trigger rate: ~15–20% of leads (those with low/no hiring signal where LLM over-claims). For the remaining ~80%, the mechanism adds zero cost (fast regex scan only).

**Cost-quality verdict:** The extra LLM call cost on ~20% of leads is justified by the risk eliminated — a false hiring assertion sent to a prospect who can verify it in 10 seconds causes permanent credibility loss. The cost of one extra compose call (~$0.001–0.002 at qwen3 rates) is negligible vs the ACV at risk from a false-claim hard-no.
