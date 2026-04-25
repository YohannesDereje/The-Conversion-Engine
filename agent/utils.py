"""
Shared utilities: env loading, Langfuse singleton, emit_span helper.
All handler modules import from here to avoid duplicating client init logic.
"""
import os
import pathlib
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

_ROOT = pathlib.Path(__file__).parent.parent

# ── kill switch & sink routing ────────────────────────────────────────────────
KILL_SWITCH_LIVE = os.getenv("KILL_SWITCH_LIVE_OUTBOUND", "false").lower() == "true"
OUTBOUND_SINK_EMAIL = os.getenv("OUTBOUND_SINK_EMAIL", "staff-sink@program.com")
OUTBOUND_SINK_SMS = os.getenv("OUTBOUND_SINK_SMS", "+10000000000")

# ── Resend ────────────────────────────────────────────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")

# ── Africa's Talking ──────────────────────────────────────────────────────────
AT_API_KEY = os.getenv("AFRICAS_TALKING_API_KEY", "")
AT_USERNAME = os.getenv("AFRICAS_TALKING_USERNAME", "sandbox")
AT_SHORTCODE = os.getenv("AFRICAS_TALKING_SHORTCODE", "")

# ── HubSpot ───────────────────────────────────────────────────────────────────
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

# ── Cal.com ───────────────────────────────────────────────────────────────────
CALCOM_API_KEY = os.getenv("CALCOM_API_KEY", "")
CALCOM_BASE_URL = os.getenv("CALCOM_BASE_URL", "https://api.cal.com").rstrip("/")
CALCOM_EVENT_TYPE_ID = int(os.getenv("CALCOM_EVENT_TYPE_ID", "0") or "0")
CALCOM_SDR_EMAIL = os.getenv("CALCOM_SDR_EMAIL", "sdr@tenacious.com")

# ── OpenRouter pricing (used to compute cost_usd in Langfuse spans) ───────────
OPENROUTER_PRICES: dict[str, dict[str, float]] = {
    "qwen/qwen3-235b-a22b": {"input_per_1k": 0.0014, "output_per_1k": 0.0014},
    "qwen/qwen3-30b-a3b": {"input_per_1k": 0.0001, "output_per_1k": 0.0001},
}


def compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return cost in USD for a given model and token counts. Returns 0.0 if model unknown."""
    pricing = OPENROUTER_PRICES.get(model)
    if not pricing:
        return 0.0
    return (input_tokens / 1000) * pricing["input_per_1k"] + \
           (output_tokens / 1000) * pricing["output_per_1k"]


# ── Langfuse ──────────────────────────────────────────────────────────────────
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

_langfuse = None


def get_langfuse():
    global _langfuse
    if _langfuse is None:
        try:
            from langfuse import Langfuse
            _langfuse = Langfuse(
                public_key=LANGFUSE_PUBLIC_KEY,
                secret_key=LANGFUSE_SECRET_KEY,
                host=LANGFUSE_HOST,
            )
        except Exception:
            _langfuse = _NoopLangfuse()
    return _langfuse


class _NoopLangfuse:
    def create_trace_id(self) -> str:
        return "noop-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")

    def flush(self) -> None:
        pass

    def start_observation(self, **_) -> "_NoopObs":
        return _NoopObs()


class _NoopObs:
    def update(self, **_) -> None:
        pass

    def end(self) -> None:
        pass


def emit_span(
    trace_id: str,
    name: str,
    input: dict,
    output: dict,
    latency_ms: float,
    metadata: dict | None = None,
) -> None:
    """
    Emit a Langfuse span attached to an existing trace_id.

    Cost attribution: if metadata contains 'model', 'input_tokens', and
    'output_tokens', cost_usd is computed automatically and added to the span.

    Swallows all errors — tracing must never break business logic.
    """
    if not trace_id:
        return
    lf = get_langfuse()
    try:
        meta = {"latency_ms": round(latency_ms, 2), **(metadata or {})}

        # Compute cost_usd when token usage is provided
        model = meta.get("model", "")
        input_tokens = int(meta.get("input_tokens", 0) or 0)
        output_tokens = int(meta.get("output_tokens", 0) or 0)
        if model and (input_tokens or output_tokens):
            meta["cost_usd"] = round(compute_cost_usd(model, input_tokens, output_tokens), 6)

        from langfuse.types import TraceContext
        tc = TraceContext(trace_id=trace_id, name=name)
        obs = lf.start_observation(
            trace_context=tc,
            name=name,
            as_type="span",
            input=input,
            metadata=meta,
        )
        obs.update(output=output)
        obs.end()
    except Exception:
        pass
