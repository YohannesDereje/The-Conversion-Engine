# τ²-Bench Baseline Reproduction Report

### What Was Reproduced
This report documents the baseline performance of the `qwen/qwen3-235b-a22b` model on the `retail` domain of the τ²-Bench evaluation suite. The test was run on the `dev` slice, consisting of 30 tasks, with 1 trial per task (facilitator update: 1 trial sufficient). The goal was to establish a statistically significant performance baseline against which future agent improvements will be measured.

### Wilson 95% Confidence Interval Result
- **Mean Pass@1:** `[PLACEHOLDER - PENDING BENCHMARK COMPLETION]`
- **95% CI:** `[PLACEHOLDER - PENDING BENCHMARK COMPLETION]`

### Cost Per Run
- **Total Cost (30 tasks):** `[PLACEHOLDER - PENDING BENCHMARK COMPLETION]`
- **Average Cost Per Task:** `[PLACEHOLDER - PENDING BENCHMARK COMPLETION]`

### Wall-Clock Latency
- **p50 Latency per Task:** `[PLACEHOLDER - PENDING BENCHMARK COMPLETION]`
- **p95 Latency per Task:** `[PLACEHOLDER - PENDING BENCHMARK COMPLETION]`

### Unexpected Behavior Observed
*[PLACEHOLDER - PENDING ANALYSIS OF BENCHMARK RESULTS]*

---

### Facilitator-Provided Reference Baseline
For comparison, the facilitator ran the benchmark using `qwen/qwen3-next-80b-a3b-thinking` with 5 trials across 30 tasks:
- **pass@1:** 0.7267
- **95% CI:** [0.6504, 0.7917]
- **avg agent cost:** $0.0199/task
- **p50 latency:** 105.95s
- **p95 latency:** 551.65s
- **git commit:** d11a97072c49d093f7b5a3e4fe9da95b490d43ba
