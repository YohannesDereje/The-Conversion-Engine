"""
eval/tau2_bench_runner.py — Group H2

Runs τ²-Bench retail domain evaluation (reproduction check).

Facilitator update (Apr 2026):
  - Baseline is provided; this script runs ONE trial only.
  - Results are appended to eval/score_log.json and eval/trace_log.jsonl
    alongside the facilitator baseline so both entries are visible.

Usage:
    python eval/tau2_bench_runner.py

Prerequisites:
    - tau2-bench cloned at C:\\Users\\Yohannes\\Desktop\\tau2-bench
    - OPENROUTER_API_KEY set in project .env
"""
import json
import math
import os
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone

# ── bootstrap: load env, add tau2 to sys.path ─────────────────────────────────
_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
_TAU2_ROOT = pathlib.Path(r"C:\Users\Yohannes\Desktop\tau2-bench")
_TAU2_SRC = _TAU2_ROOT / "src"

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")                         # project keys first
if (_TAU2_ROOT / ".env").exists():
    load_dotenv(_TAU2_ROOT / ".env", override=False)        # tau2 keys (no override)

if str(_TAU2_SRC) not in sys.path:
    sys.path.insert(0, str(_TAU2_SRC))

# ── output paths ──────────────────────────────────────────────────────────────
_EVAL_DIR = _PROJECT_ROOT / "eval"
_SCORE_LOG = _EVAL_DIR / "score_log.json"
_TRACE_LOG = _EVAL_DIR / "trace_log.jsonl"

# ── run config ────────────────────────────────────────────────────────────────
DOMAIN     = os.getenv("TAU2_BENCH_DOMAIN", "retail")
_MODEL     = os.getenv("TAU2_BENCH_MODEL", "qwen/qwen3-235b-a22b")
AGENT_MODEL = f"openrouter/{_MODEL}"
USER_MODEL  = f"openrouter/{_MODEL}"
N_TRIALS   = 1          # facilitator update — one trial is enough
N_TASKS    = int(os.getenv("TAU2_BENCH_DEV_SLICE_SIZE", "30"))
SEED       = 42


# ── helpers ───────────────────────────────────────────────────────────────────

def wilson_ci(n_pass: int, n_total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% confidence interval."""
    if n_total == 0:
        return 0.0, 0.0
    p = n_pass / n_total
    denom = 1 + z ** 2 / n_total
    centre = (p + z ** 2 / (2 * n_total)) / denom
    margin = z * math.sqrt(p * (1 - p) / n_total + z ** 2 / (4 * n_total ** 2)) / denom
    return round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
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
                duration: float, run_id: str) -> str:
    if lf is None:
        return ""
    try:
        from langfuse.types import TraceContext
        tid = lf.create_trace_id()
        tc = TraceContext(
            trace_id=tid,
            name=f"tau2.{DOMAIN}.task_{task_id}",
            metadata={"task_id": task_id, "trial": 1, "run_id": run_id, "domain": DOMAIN},
        )
        obs = lf.start_observation(
            trace_context=tc,
            name="tau2_task",
            as_type="span",
            input={"task_id": task_id, "model": AGENT_MODEL, "domain": DOMAIN},
            metadata={"run_id": run_id},
        )
        obs.update(output={
            "reward": reward,
            "passed": reward >= 1.0,
            "cost_usd": cost,
            "duration_s": duration,
        })
        obs.end()
        return tid
    except Exception:
        return ""


def _load_existing_runs() -> list[dict]:
    """
    Read score_log.json.  Handles two formats:
      - flat baseline object  → wraps it as first run entry
      - runs list             → returns as-is
    """
    if not _SCORE_LOG.exists():
        return []
    raw = json.loads(_SCORE_LOG.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    # facilitator-provided flat object
    ci = raw.get("pass_at_1_ci_95", [0.0, 0.0])
    n  = raw.get("total_tasks", 30)
    return [{
        "run_id":            "baseline",
        "model":             "openrouter/qwen/qwen3-next-80b-a3b-thinking",
        "domain":            raw.get("domain", "retail"),
        "slice":             "dev",
        "n_tasks":           n,
        "n_trials":          raw.get("num_trials", 5),
        "pass_at_1_mean":    raw.get("pass_at_1", 0.0),
        "ci_lower":          ci[0] if len(ci) > 0 else 0.0,
        "ci_upper":          ci[1] if len(ci) > 1 else 0.0,
        "cost_usd":          round(raw.get("avg_agent_cost", 0.0) * n, 4),
        "avg_cost_per_task": raw.get("avg_agent_cost", 0.0),
        "wall_clock_p50_s":  raw.get("p50_latency_seconds", 0.0),
        "wall_clock_p95_s":  raw.get("p95_latency_seconds", 0.0),
        "timestamp":         "2026-04-22T00:00:00Z",
        "note":              "facilitator-provided baseline",
    }]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # ── preflight ────────────────────────────────────────────────────────────
    if not os.getenv("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set. Check your .env file.")
        sys.exit(1)

    try:
        from tau2.run import get_tasks, run_single_task
        from tau2.data_model.simulation import TextRunConfig
    except ImportError as exc:
        print(f"ERROR: Cannot import tau2 — {exc}")
        print(f"Expected tau2 source at: {_TAU2_SRC}")
        print("Run 'uv sync' inside tau2-bench, then retry.")
        sys.exit(1)

    run_id   = uuid.uuid4().hex[:8]
    ts_start = datetime.now(timezone.utc).isoformat()
    lf       = _get_langfuse()

    print("=" * 65)
    print("τ²-Bench Runner  —  reproduction check")
    print(f"Domain : {DOMAIN}   Tasks : {N_TASKS}   Trials : {N_TRIALS}")
    print(f"Model  : {AGENT_MODEL}")
    print(f"Run ID : {run_id}")
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

            tid = _emit_trace(lf, task_id, reward, cost, duration, run_id)

            trace_records.append({
                # original trace_log.jsonl format (matches facilitator files)
                "agent_cost":         cost,
                "domain":             DOMAIN,
                "duration":           duration,
                "reward":             reward,
                "simulation_id":      sim_id,
                "task_id":            task_id,
                "termination_reason": reason,
                # H4 extended fields
                "trace_id":   tid or sim_id,
                "trial":      1,
                "passed":     passed,
                "turns":      turns,
                "cost_usd":   cost,
                "duration_s": duration,
                "model":      AGENT_MODEL,
            })

        except Exception as exc:
            duration = time.monotonic() - t0
            print(f"ERROR  {duration:6.1f}s  {exc}")
            total     += 1
            latencies.append(duration)
            trace_records.append({
                "agent_cost": 0.0, "domain": DOMAIN, "duration": duration,
                "reward": 0.0, "simulation_id": uuid.uuid4().hex,
                "task_id": task_id, "termination_reason": "runner_error",
                "trace_id": "", "trial": 1, "passed": False,
                "turns": 0, "cost_usd": 0.0, "duration_s": duration,
                "model": AGENT_MODEL,
            })

    if lf:
        lf.flush()

    # ── compute stats ─────────────────────────────────────────────────────────
    pass_at_1  = passes / total if total > 0 else 0.0
    ci_lo, ci_hi = wilson_ci(passes, total)
    total_cost = sum(costs)
    avg_cost   = total_cost / total if total > 0 else 0.0
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)

    print("\n" + "=" * 65)
    print(f"pass@1 : {pass_at_1:.4f}  ({passes}/{total})")
    print(f"95% CI : [{ci_lo}, {ci_hi}]")
    print(f"cost   : ${total_cost:.4f} total  (${avg_cost:.4f}/task avg)")
    print(f"p50    : {p50:.2f}s   p95 : {p95:.2f}s")

    # ── append trace_log.jsonl ────────────────────────────────────────────────
    with open(_TRACE_LOG, "a", encoding="utf-8") as f:
        for rec in trace_records:
            f.write(json.dumps(rec) + "\n")
    print(f"\nAppended {len(trace_records)} records -> {_TRACE_LOG.name}")

    # ── update score_log.json ─────────────────────────────────────────────────
    runs = _load_existing_runs()
    runs.append({
        "run_id":            run_id,
        "model":             AGENT_MODEL,
        "domain":            DOMAIN,
        "slice":             "dev",
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
    })
    _SCORE_LOG.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    print(f"Updated score_log.json  ({len(runs)} total runs)")
    print("=" * 65)


if __name__ == "__main__":
    main()
