"""
Email handler (F1) — Resend API v2.29.0.

send_email: sends via Resend; routes to sink when kill switch is off.
handle_reply_webhook: parses Resend inbound/reply webhook payload.
"""
import time
from datetime import datetime, timezone

import resend

from agent.utils import (
    KILL_SWITCH_LIVE,
    OUTBOUND_SINK_EMAIL,
    RESEND_API_KEY,
    RESEND_FROM_EMAIL,
    emit_span,
)

resend.api_key = RESEND_API_KEY


async def send_email(
    to: str,
    subject: str,
    body: str,
    trace_id: str,
) -> dict:
    """
    Send an email via Resend.

    When KILL_SWITCH_LIVE_OUTBOUND is false the message is routed to
    OUTBOUND_SINK_EMAIL — the real recipient address is never contacted.

    Returns:
        {id, to, intended_to, status, timestamp}
    """
    t0 = time.monotonic()
    actual_to = to if KILL_SWITCH_LIVE else OUTBOUND_SINK_EMAIL
    status = "sent" if KILL_SWITCH_LIVE else "sink"

    params = {
        "from": RESEND_FROM_EMAIL,
        "to": [actual_to],
        "subject": subject,
        "text": body,
    }

    try:
        response = resend.Emails.send(params)
        if isinstance(response, dict):
            email_id = response.get("id", "unknown")
        else:
            email_id = getattr(response, "id", "unknown")
    except Exception as exc:
        email_id = f"error:{exc}"
        status = "error"

    latency_ms = (time.monotonic() - t0) * 1000
    result = {
        "id": email_id,
        "to": actual_to,
        "intended_to": to,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    emit_span(
        trace_id=trace_id,
        name="email_handler.send_email",
        input={
            "to": actual_to,
            "intended_to": to,
            "subject": subject,
            "kill_switch_live": KILL_SWITCH_LIVE,
        },
        output=result,
        latency_ms=latency_ms,
    )
    return result


def handle_reply_webhook(payload: dict) -> dict:
    """
    Extract reply text and thread_id from a Resend inbound/reply webhook payload.

    Resend sends either a nested {"type": "email.received", "data": {...}} envelope
    or a flat dict — handles both.

    Returns:
        {thread_id, reply_text, from_email, timestamp}
    """
    data = payload.get("data", payload)
    headers = data.get("headers", {})

    # thread_id: prefer In-Reply-To header, fall back to messageId
    thread_id = headers.get("In-Reply-To") or headers.get("in-reply-to") or data.get("messageId", "")
    reply_text = data.get("text") or data.get("html") or ""
    from_email = data.get("from", "")
    timestamp = data.get("createdAt") or datetime.now(timezone.utc).isoformat()

    return {
        "thread_id": thread_id,
        "reply_text": reply_text,
        "from_email": from_email,
        "timestamp": timestamp,
    }
