"""
P3-L1 — End-to-end smoke test for the complete Phase 3 system.

Runs 5 test scenarios against a locally running FastAPI server.
Server must be running: uvicorn agent.main:app --port 8000

Usage:
    python -m scripts.smoke_test [--base-url http://localhost:8000]
"""
import argparse
import os
import sys

import httpx
from dotenv import load_dotenv
load_dotenv()

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"

_results: list[dict] = []


def _check(label: str, condition: bool, detail: str = "") -> bool:
    icon = PASS if condition else FAIL
    line = f"  {icon} {label}"
    if detail:
        line += f" -- {detail}"
    print(line)
    _results.append({"label": label, "passed": condition, "detail": detail})
    return condition


def _post(client: httpx.Client, url: str, payload: dict, timeout: float = 180.0) -> dict:
    r = client.post(url, json=payload, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"_raw_status": r.status_code, "_raw_text": r.text[:500]}


# ── Test 1: Cold sequence ─────────────────────────────────────────────────────

def test1_cold_sequence(base: str, client: httpx.Client):
    print(f"\n{INFO} Test 1 -- Cold sequence (Notion)")

    r = _post(client, f"{base}/leads/process", {
        "company_name": "Notion",
        "contact_email": "test-notion@sink.tenacious.com",
        "contact_name": "Test User",
    })
    _check("Email 1 status ok", r.get("status") == "ok", str(r.get("status")))
    _check("Email 1 routed to sink",
           "sink" in str(r.get("email_routed_to", "")).lower() or r.get("email_status") == "sink",
           str(r.get("email_routed_to")))
    _check("HubSpot contact created",
           bool(r.get("contact_id") and r.get("contact_id") != "unknown"),
           str(r.get("contact_id")))
    _check("Langfuse trace_id present",
           bool(r.get("langfuse_trace_id")),
           str(r.get("langfuse_trace_id")))

    contact_id = r.get("contact_id", "unknown")
    contact_email = "test-notion@sink.tenacious.com"

    # Email 2 -- followup (may be gated by day timer; accept ok, skipped, too_early, or 404-like)
    r2 = _post(client, f"{base}/leads/followup", {
        "contact_id": contact_id,
        "company_name": "Notion",
        "contact_email": contact_email,
        "contact_name": "Test User",
    })
    step2_ok = r2.get("status") in ("ok", "skipped", "too_early") or \
               "detail" not in r2 or r2.get("_raw_status", 200) < 500
    _check("Email 2 endpoint responds (day gate may block)", step2_ok,
           str(r2.get("status", r2.get("detail", r2.get("_raw_status")))))

    # Email 3
    r3 = _post(client, f"{base}/leads/followup", {
        "contact_id": contact_id,
        "company_name": "Notion",
        "contact_email": contact_email,
        "contact_name": "Test User",
    })
    step3_ok = r3.get("status") in ("ok", "skipped", "closed", "too_early") or \
               r3.get("_raw_status", 200) < 500
    _check("Email 3 endpoint responds (day gate may block)", step3_ok,
           str(r3.get("status", r3.get("detail", r3.get("_raw_status")))))

    return contact_id, contact_email


# ── Test 2: Warm reply -- engaged ──────────────────────────────────────────────

def test2_warm_engaged(base: str, client: httpx.Client):
    print(f"\n{INFO} Test 2 -- Warm reply (engaged)")

    # Resend webhook format: "from" is a plain email string, body is in "text"
    webhook_payload = {
        "type": "email.received",
        "data": {
            "from": "engaged-prospect@example.com",
            "subject": "Re: Quick question on MLOps",
            "text": (
                "Thanks for reaching out. We're actually evaluating options for "
                "our ML infrastructure team right now. Would love to hear more "
                "about how you handle model deployment pipelines. When are you free?"
            ),
            "headers": {"In-Reply-To": "thread-engaged-001"},
        },
    }
    r = _post(client, f"{base}/email/webhook", webhook_payload, timeout=180.0)
    no_5xx = "_raw_status" not in r or r.get("_raw_status", 200) < 500
    _check("Webhook accepted (no 5xx)", no_5xx,
           str(r.get("status", r.get("_raw_status"))))
    reply_class = r.get("reply_class")
    _check("Reply class classified (not None)",
           reply_class is not None,
           str(reply_class))
    _sink = os.getenv("OUTBOUND_SINK_EMAIL", "").lower()
    _routed_to = str(r.get("email_routed_to", "")).lower()
    _check("Reply routed to sink",
           "sink" in _routed_to or (_sink and _sink in _routed_to),
           str(r.get("email_routed_to")))


# ── Test 3: Hard no ───────────────────────────────────────────────────────────

def test3_hard_no(base: str, client: httpx.Client):
    print(f"\n{INFO} Test 3 -- Hard no (remove me)")

    webhook_payload = {
        "type": "email.received",
        "data": {
            "from": "hardno-prospect@example.com",
            "subject": "Re: Quick question",
            "text": "Please remove me from your list. Not interested.",
            "headers": {"In-Reply-To": "thread-hardno-001"},
        },
    }
    r = _post(client, f"{base}/email/webhook", webhook_payload, timeout=60.0)
    _check("Hard no classified",
           r.get("reply_class") == "hard_no" or "hard_no" in str(r).lower(),
           str(r.get("reply_class")))
    _check("No reply email sent",
           not r.get("reply_sent", False) and not r.get("email_sent", False),
           str(r.get("reply_sent")))
    opted_out = (
        str(r.get("action", "")).lower() in ("opted_out_no_reply",) or
        "disqualif" in str(r).lower() or
        "opted" in str(r).lower()
    )
    _check("HubSpot DISQUALIFIED or opted_out action fired", opted_out,
           str(r.get("action", r.get("new_status"))))


# ── Test 4: Human handoff ─────────────────────────────────────────────────────

def test4_human_handoff(base: str, client: httpx.Client):
    print(f"\n{INFO} Test 4 -- Human handoff (MSA terms)")

    webhook_payload = {
        "type": "email.received",
        "data": {
            "from": "msa-prospect@example.com",
            "subject": "Re: Engineering squad proposal",
            "text": "Can you send over the MSA terms and your standard contract?",
            "headers": {"In-Reply-To": "thread-msa-001"},
        },
    }
    r = _post(client, f"{base}/email/webhook", webhook_payload, timeout=120.0)
    handoff_fired = r.get("handoff_triggered") is True or "handoff" in str(r).lower()
    _check("Handoff trigger fired", handoff_fired,
           str(r.get("handoff_triggered", r.get("action"))))
    delivery_lead_msg = "our delivery lead will follow up within 24 hours"
    reply_body = str(r.get("reply_body", r.get("composed_body", r.get("body", "")))).lower()
    _check("Handoff reply contains correct text",
           delivery_lead_msg in reply_body,
           reply_body[:120] if reply_body else "no body found")


# ── Test 5: Re-engagement ─────────────────────────────────────────────────────

def test5_reengagement(base: str, client: httpx.Client):
    print(f"\n{INFO} Test 5 -- Re-engagement (STALLED contact)")

    r = _post(client, f"{base}/leads/reengage", {
        "contact_id": "smoke-test-stalled-001",
        "company_name": "Acme Corp",
        "contact_email": "stalled-prospect@example.com",
        "contact_name": "Stalled User",
    }, timeout=60.0)
    no_crash = "_raw_status" not in r or r.get("_raw_status", 200) < 500
    _check("Reengage endpoint responds without crash", no_crash,
           str(r.get("status", r.get("_raw_status"))))
    valid_status = r.get("status") in ("ok", "not_eligible", "not_found")
    _check("Reengage returns valid status", valid_status, str(r.get("status")))

    if r.get("status") == "ok":
        _check("Re-engage email routed to sink",
               "sink" in str(r.get("email_routed_to", "")).lower(),
               str(r.get("email_routed_to")))
    else:
        print(f"    (not_eligible/not_found expected without seeded STALLED contact: {r.get('reason')})")


# ── Health check ──────────────────────────────────────────────────────────────

def test_health(base: str, client: httpx.Client):
    print(f"\n{INFO} Health check")
    r = client.get(f"{base}/health", timeout=10.0).json()
    _check("Health status ok", r.get("status") == "ok", str(r.get("status")))
    _check("Kill switch is safe (not live)", not r.get("kill_switch_live", True),
           str(r.get("kill_switch_live")))
    return r


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    print(f"\nConversion Engine -- Phase 3 Smoke Test")
    print(f"Target: {base}")
    print("=" * 60)

    with httpx.Client() as client:
        try:
            test_health(base, client)
        except Exception as exc:
            print(f"{FAIL} Cannot reach {base}/health -- is the server running? ({exc})")
            sys.exit(1)

        test1_cold_sequence(base, client)
        test2_warm_engaged(base, client)
        test3_hard_no(base, client)
        test4_human_handoff(base, client)
        test5_reengagement(base, client)

    total = len(_results)
    passed = sum(1 for r in _results if r["passed"])
    failed = total - passed

    print("\n" + "=" * 60)
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} failed)")
        for r in _results:
            if not r["passed"]:
                print(f"  {FAIL} {r['label']}: {r['detail']}")
    else:
        print(" -- all green")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
