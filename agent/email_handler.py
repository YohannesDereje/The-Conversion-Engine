"""
Email handler (F1) — Resend API v2.29.0.

send_email       : sends via Resend; routes to sink when kill switch is off.
handle_reply_webhook  : parses Resend inbound/reply webhook; calls registered handlers.
handle_bounce_webhook : parses Resend bounce/delivery-failure webhook.
register_reply_handler  : attach downstream logic to reply events.
register_bounce_handler : attach downstream logic to bounce events.
"""
import logging
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
_logger = logging.getLogger(__name__)

# ── downstream handler registry ───────────────────────────────────────────────
# External logic can attach to reply and bounce events by registering callables.
# Each handler receives the parsed event dict.

_reply_handlers: list = []
_bounce_handlers: list = []


def register_reply_handler(handler) -> None:
    """Register a callable that receives parsed reply events."""
    _reply_handlers.append(handler)


def register_bounce_handler(handler) -> None:
    """Register a callable that receives parsed bounce/delivery-failure events."""
    _bounce_handlers.append(handler)


# ── send ──────────────────────────────────────────────────────────────────────

async def send_email(
    to: str,
    subject: str,
    body: str,
    trace_id: str,
) -> dict:
    """
    Send an email via Resend.

    When KILL_SWITCH_LIVE_OUTBOUND is false the message is routed to
    OUTBOUND_SINK_EMAIL — the real recipient is never contacted.

    Returns:
        {id, to, intended_to, status, timestamp}

    Raises:
        ValueError: if required fields are missing (malformed input guard).
    """
    if not subject or not body:
        raise ValueError("send_email: subject and body are required")

    t0 = time.monotonic()
    actual_to = to if KILL_SWITCH_LIVE else OUTBOUND_SINK_EMAIL
    status = "sent" if KILL_SWITCH_LIVE else "sink"

    params = {
        "from": RESEND_FROM_EMAIL,
        "to": [actual_to],
        "subject": subject,
        "text": body,
        "headers": {"X-Tenacious-Status": "draft"},
    }

    email_id = "unknown"
    try:
        response = resend.Emails.send(params)
        if isinstance(response, dict):
            email_id = response.get("id", "unknown")
        else:
            email_id = getattr(response, "id", "unknown")
    except Exception as exc:
        email_id = f"error:{exc}"
        status = "error"
        _logger.error("Resend send failed to=%s: %s", actual_to, exc)

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


# ── webhook handlers ──────────────────────────────────────────────────────────

def handle_reply_webhook(payload: dict) -> dict:
    """
    Parse a Resend inbound/reply webhook and route to registered reply handlers.

    Handles both nested {"type": ..., "data": {...}} and flat Resend envelopes.
    Registered handlers receive the parsed reply dict.

    Returns:
        {event, thread_id, reply_text, from_email, timestamp}

    Raises:
        TypeError: if payload is not a dict (malformed input guard).
    """
    if not isinstance(payload, dict):
        raise TypeError(f"handle_reply_webhook: expected dict, got {type(payload).__name__}")

    data = payload.get("data", payload)
    headers = data.get("headers", {})
    event_type = payload.get("type", "email.reply")

    # Bounce/delivery events are routed to the bounce handler instead
    if any(k in event_type for k in ("bounced", "delayed", "complained", "delivery")):
        return handle_bounce_webhook(payload)

    thread_id = (
        headers.get("In-Reply-To")
        or headers.get("in-reply-to")
        or data.get("messageId", "")
    )

    result = {
        "event": "reply",
        "thread_id": thread_id,
        "reply_text": data.get("text") or data.get("html") or "",
        "from_email": data.get("from", ""),
        "timestamp": data.get("createdAt") or datetime.now(timezone.utc).isoformat(),
    }

    for handler in _reply_handlers:
        try:
            handler(result)
        except Exception as exc:
            _logger.error("reply handler %s raised: %s", handler, exc)

    return result


def handle_bounce_webhook(payload: dict) -> dict:
    """
    Parse a Resend bounce / delivery-failure webhook and route to registered
    bounce handlers.

    Resend event types covered: email.bounced, email.delivery_delayed,
    email.complained.

    Returns:
        {event, email_id, bounce_type, recipient, timestamp}

    Raises:
        TypeError: if payload is not a dict (malformed input guard).
    """
    if not isinstance(payload, dict):
        raise TypeError(f"handle_bounce_webhook: expected dict, got {type(payload).__name__}")

    data = payload.get("data", payload)
    event_type = payload.get("type", "email.bounced")

    result = {
        "event": "bounce",
        "bounce_type": event_type,
        "email_id": data.get("emailId") or data.get("id", ""),
        "recipient": data.get("to") or data.get("email", ""),
        "timestamp": data.get("createdAt") or datetime.now(timezone.utc).isoformat(),
    }

    _logger.warning("Email bounce event: %s for %s", event_type, result.get("recipient"))

    for handler in _bounce_handlers:
        try:
            handler(result)
        except Exception as exc:
            _logger.error("bounce handler %s raised: %s", handler, exc)

    return result
