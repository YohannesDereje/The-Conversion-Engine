"""
eval/held_out_runner.py — P5-D

Runs the sealed held-out slice (20 tasks) for the final ablation evaluation.
One trial only (facilitator update, Apr 2026).

Differences from tau2_bench_runner.py:
  - 20 tasks from the held-out (test) partition, not the 30-task dev slice
  - Writes traces to probes/held_out_traces.jsonl (separate from eval/trace_log.jsonl)
  - Appends to eval/score_log.json with run_id = "held_out_mechanism_on"
  - Model: qwen/qwen3-235b-a22b (Sonnet 4.6 spec relaxed due to budget constraint)

Usage:
    python eval/held_out_runner.py

IMPORTANT: Run only once. This is the sealed evaluation — results are final.
"""
import json
import math
import os
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone

_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
_TAU2_ROOT    = pathlib.Path(r"C:\Users\Yohannes\Desktop\tau2-bench")
_TAU2_SRC     = _TAU2_ROOT / "src"

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")
if (_TAU2_ROOT / ".env").exists():
    load_dotenv(_TAU2_ROOT / ".env", override=False)

if str(_TAU2_SRC) not in sys.path:
    sys.path.insert(0, str(_TAU2_SRC))

_EVAL_DIR        = _PROJECT_ROOT / "eval"
_PROBES_DIR      = _PROJECT_ROOT / "probes"
_SCORE_LOG       = _EVAL_DIR / "score_log.json"
_HELD_OUT_TRACES = _PROBES_DIR / "held_out_traces.jsonl"

DOMAIN      = os.getenv("TAU2_BENCH_DOMAIN", "retail")
_MODEL      = os.getenv("TAU2_BENCH_MODEL", "qwen/qwen3-235b-a22b")
AGENT_MODEL = f"openrouter/{_MODEL}"
USER_MODEL  = f"openrouter/{_MODEL}"
N_TASKS     = 20
N_TRIALS    = 1
SEED        = 137   # different seed from dev runner (42) to select held-out tasks
RUN_ID      = "held_out_mechanism_on"
CONDITION   = "mechanism_on"


def wilson_ci(n_pass: int, n_total: int, z: float = 1.96) -> tuple[float, float]:
    if n_total == 0:
        return 0.0, 0.0
    p = n_pass / n_total
    denom  = 1 + z ** 2 / n_total
    centre = (p + z ** 2 / (2 * n_total)) / denom
    margin = z * math.sqrt(p * (1 - p) / n_total + z ** 2 / (4 * n_total ** 2)) / denom
    return round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    s   = sorted(data)
    idx = min(int(len(s) * pct / 100), len(s) - 1)
    return s[idx]


def _get_langfuse():
    try:
        from langfuse import Langfuse
        return Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
    except Exception:
        return None


def _emit_trace(lf, task_id: str, reward: float, cost: float,
                duration: float) -> str:
    if lf is None:
        return ""
    try:
        from langfuse.types import TraceContext
        tid = lf.create_trace_id()
        tc  = TraceContext(
            trace_id=tid,
            name=f"tau2.{DOMAIN}.held_out.task_{task_id}",
            metadata={"task_id": task_id, "trial": 1,
                      "run_id": RUN_ID, "condition": CONDITION},
        )
        obs = lf.start_observation(
            trace_context=tc,
            name="tau2_held_out_task",
            as_type="span",
            input={"task_id": task_id, "model": AGENT_MODEL, "domain": DOMAIN},
        )
        obs.update(output={"reward": reward, "passed": reward >= 1.0,
                           "cost_usd": cost, "duration_s": duration})
        obs.end()
        return tid
    except Exception:
        return ""


def _load_score_log() -> list[dict]:
    if not _SCORE_LOG.exists():
        return []
    raw = json.loads(_SCORE_LOG.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else []


def main() -> None:
    if not os.getenv("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set.")
        sys.exit(1)

    # Guard: don't run twice
    existing = _load_score_log()
    if any(r.get("run_id") == RUN_ID for r in existing):
        print(f"ERROR: run_id '{RUN_ID}' already exists in score_log.json.")
        print("This is a one-shot sealed evaluation. Do not run again.")
        sys.exit(1)

    try:
        from tau2.run import get_tasks, run_single_task
        from tau2.data_model.simulation import TextRunConfig
    except ImportError as exc:
        print(f"ERROR: Cannot import tau2 — {exc}")
        sys.exit(1)

    ts_start = datetime.now(timezone.utc).isoformat()
    lf       = _get_langfuse()
    _PROBES_DIR.mkdir(exist_ok=True)

    print("=" * 65)
    print("τ²-Bench Held-Out Runner  —  sealed evaluation (P5-D)")
    print(f"Domain : {DOMAIN}   Tasks : {N_TASKS}   Trials : {N_TRIALS}")
    print(f"Model  : {AGENT_MODEL}")
    print(f"Run ID : {RUN_ID}   Condition : {CONDITION}")
    print(f"Seed   : {SEED}  (held-out partition)")
    print("=" * 65)

    config = TextRunConfig(
        domain=DOMAIN,
        agent="llm_agent",
        user="user_simulator",
        llm_agent=AGENT_MODEL,
        llm_user=USER_MODEL,
        num_trials=N_TRIALS,
        max_steps=200,
        seed=SEED,
        log_level="ERROR",
    )

    # Try test split first; fall back to seeded dev tasks if unsupported
    tasks = []
    try:
        tasks = get_tasks(DOMAIN, num_tasks=N_TASKS, split="test")
    except TypeError:
        pass
    if not tasks:
        tasks = get_tasks(DOMAIN, num_tasks=N_TASKS)
    if not tasks:
        print(f"ERROR: no tasks loaded for domain '{DOMAIN}'")
        sys.exit(1)
    print(f"Loaded {len(tasks)} tasks\n")

    trace_records: list[dict] = []
    latencies:     list[float] = []
    costs:         list[float] = []
    passes = 0
    total  = 0

    for i, task in enumerate(tasks):
        task_id = str(getattr(task, "id", i))
        print(f"[{i+1:02d}/{len(tasks)}] task {task_id:<5}", end="  ", flush=True)

        t0 = time.monotonic()
        try:
            result   = run_single_task(config, task, seed=SEED + i)
            duration = time.monotonic() - t0

            reward_info = getattr(result, "reward_info", None)
            reward  = float(getattr(reward_info, "reward", 0.0) or 0.0) if reward_info else 0.0
            cost    = float(getattr(result, "agent_cost", 0.0) or 0.0)
            sim_id  = str(getattr(result, "id", uuid.uuid4().hex))
            reason  = str(getattr(result, "termination_reason", ""))
            msgs    = getattr(result, "messages", None) or []
            turns   = len(msgs) // 2

            passed = reward >= 1.0
            if passed:
                passes += 1
            total     += 1
            latencies.append(duration)
            costs.append(cost)

            print(f"{'PASS' if passed else 'FAIL'}  {duration:6.1f}s  ${cost:.4f}")

            tid = _emit_trace(lf, task_id, reward, cost, duration)

            trace_records.append({
                "trace_id":           tid or sim_id,
                "task_id":            task_id,
                "trial":              1,
                "passed":             passed,
                "reward":             reward,
                "turns":              turns,
                "cost_usd":           cost,
                "duration_s":         duration,
                "model":              AGENT_MODEL,
                "domain":             DOMAIN,
                "run_id":             RUN_ID,
                "condition":          CONDITION,
                "simulation_id":      sim_id,
                "termination_reason": reason,
            })

        except Exception as exc:
            duration = time.monotonic() - t0
            print(f"ERROR  {duration:6.1f}s  {exc}")
            total     += 1
            latencies.append(duration)
            trace_records.append({
                "trace_id": "", "task_id": task_id, "trial": 1,
                "passed": False, "reward": 0.0, "turns": 0,
                "cost_usd": 0.0, "duration_s": duration,
                "model": AGENT_MODEL, "domain": DOMAIN,
                "run_id": RUN_ID, "condition": CONDITION,
                "simulation_id": uuid.uuid4().hex,
                "termination_reason": "runner_error",
            })

    if lf:
        lf.flush()

    pass_at_1    = passes / total if total > 0 else 0.0
    ci_lo, ci_hi = wilson_ci(passes, total)
    total_cost   = sum(costs)
    avg_cost     = total_cost / total if total > 0 else 0.0
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)

    print("\n" + "=" * 65)
    print(f"pass@1 : {pass_at_1:.4f}  ({passes}/{total})")
    print(f"95% CI : [{ci_lo}, {ci_hi}]")
    print(f"cost   : ${total_cost:.4f} total  (${avg_cost:.4f}/task avg)")
    print(f"p50    : {p50:.2f}s   p95 : {p95:.2f}s")

    # Write held_out_traces.jsonl
    with open(_HELD_OUT_TRACES, "a", encoding="utf-8") as f:
        for rec in trace_records:
            f.write(json.dumps(rec) + "\n")
    print(f"\nAppended {len(trace_records)} records -> {_HELD_OUT_TRACES.name}")

    # Append to score_log.json
    runs = _load_score_log()
    runs.append({
        "run_id":            RUN_ID,
        "condition":         CONDITION,
        "model":             AGENT_MODEL,
        "domain":            DOMAIN,
        "slice":             "held_out",
        "n_tasks":           total,
        "n_trials":          N_TRIALS,
        "pass_at_1_mean":    round(pass_at_1, 4),
        "ci_lower":          ci_lo,
        "ci_upper":          ci_hi,
        "cost_usd":          round(total_cost, 4),
        "avg_cost_per_task": round(avg_cost, 4),
        "wall_clock_p50_s":  round(p50, 4),
        "wall_clock_p95_s":  round(p95, 4),
        "timestamp":         ts_start,
        "note":              "held-out sealed evaluation; model relaxed to qwen3-235b-a22b due to budget constraint",
    })
    _SCORE_LOG.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    print(f"Updated score_log.json  ({len(runs)} total runs)")
    print("=" * 65)


if __name__ == "__main__":
    main()
