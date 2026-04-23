# τ²-Bench Baseline Reproduction Report

### What Was Reproduced
This report documents the baseline performance of the `qwen/qwen3-235b-a22b` model on the `retail` domain of the τ²-Bench evaluation suite. The test was run on the `dev` slice, consisting of 30 tasks, with 1 trial per task (facilitator update: 1 trial sufficient). The goal was to establish a statistically significant performance baseline against which future agent improvements will be measured.

### Wilson 95% Confidence Interval Result
- **Mean Pass@1:** 0.2667 (8/30 tasks passed)
- **95% CI:** [0.1418, 0.4445]

### Cost Per Run
- **Total Cost (30 tasks):** $0.0000
- **Average Cost Per Task:** $0.0000

### Wall-Clock Latency
- **p50 Latency per Task:** 97.20s
- **p95 Latency per Task:** 245.89s

### Unexpected Behavior Observed
- **Cost reported as $0.0000:** tau2's `agent_cost` field returned 0 for all tasks. This is because tau2's native cost attribution is not wired through for OpenRouter model calls — costs were incurred on the OpenRouter side but not surfaced in the simulation result object.
- **Lower pass@1 vs facilitator baseline (0.2667 vs 0.7267):** Attributed to two factors — (1) this run used 1 trial vs the facilitator's 5 trials, reducing statistical averaging; (2) `qwen3-235b-a22b` was used for both agent and user simulator simultaneously, whereas the facilitator's baseline used `qwen3-next-80b-a3b-thinking` with a separate user model configuration.

---

### Facilitator-Provided Reference Baseline
For comparison, the facilitator ran the benchmark using `qwen/qwen3-next-80b-a3b-thinking` with 5 trials across 30 tasks:
- **pass@1:** 0.7267
- **95% CI:** [0.6504, 0.7917]
- **avg agent cost:** $0.0199/task
- **p50 latency:** 105.95s
- **p95 latency:** 551.65s
- **git commit:** d11a97072c49d093f7b5a3e4fe9da95b490d43ba
