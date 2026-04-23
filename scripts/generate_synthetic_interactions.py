"""
G3 — Synthetic lead interaction script.

Loads 20 companies from the Crunchbase CSV and POSTs each to
POST /leads/process, recording wall-clock latency per request.
Reports p50 and p95 latency at the end.

Usage:
    # Start the API first:
    uvicorn agent.main:app --reload

    # Then in a separate terminal:
    python scripts/generate_synthetic_interactions.py
"""
import asyncio
import pathlib
import time

import httpx
import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).parent.parent
CSV_PATH = ROOT / "data" / "crunchbase-companies-information.csv"
API_URL = "http://127.0.0.1:8000/leads/process"
SAMPLE_SIZE = 20
RANDOM_SEED = 42
# Each pipeline call includes LLM inference — allow up to 3 minutes per request
REQUEST_TIMEOUT = 180.0


async def run_one(
    client: httpx.AsyncClient,
    idx: int,
    company_name: str,
) -> float:
    """POST a single lead. Returns wall-clock duration in seconds."""
    payload = {
        "company_name": company_name,
        "contact_email": "test@sink.com",
        "contact_name": "Test User",
    }
    t0 = time.monotonic()
    try:
        r = await client.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
        duration = time.monotonic() - t0
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        segment = body.get("icp_segment", "?")
        print(
            f"[{idx+1:02d}/{SAMPLE_SIZE}] {company_name[:45]:<45} "
            f"HTTP {r.status_code}  seg={segment}  {duration:.1f}s"
        )
    except httpx.TimeoutException:
        duration = time.monotonic() - t0
        print(f"[{idx+1:02d}/{SAMPLE_SIZE}] {company_name[:45]:<45} TIMEOUT  {duration:.1f}s")
    except Exception as exc:
        duration = time.monotonic() - t0
        print(f"[{idx+1:02d}/{SAMPLE_SIZE}] {company_name[:45]:<45} ERROR: {exc}  {duration:.1f}s")
    return duration


async def main() -> None:
    if not CSV_PATH.exists():
        print(f"ERROR: CSV not found at {CSV_PATH}")
        return

    df = pd.read_csv(CSV_PATH, usecols=["name"])
    df = df.dropna(subset=["name"])
    sample = df["name"].sample(n=min(SAMPLE_SIZE, len(df)), random_state=RANDOM_SEED).tolist()

    print("=" * 65)
    print(f"Conversion Engine — Synthetic Interaction Runner")
    print(f"Target: {API_URL}")
    print(f"Companies: {len(sample)}   Timeout per request: {REQUEST_TIMEOUT}s")
    print("=" * 65)

    latencies: list[float] = []
    async with httpx.AsyncClient() as client:
        for idx, company in enumerate(sample):
            duration = await run_one(client, idx, company)
            latencies.append(duration)

    if latencies:
        p50 = np.percentile(latencies, 50)
        p95 = np.percentile(latencies, 95)
        print("=" * 65)
        print(f"Completed {len(latencies)} interactions")
        print(f"p50 Latency: {p50:.2f}s")
        print(f"p95 Latency: {p95:.2f}s")
        print(f"Min: {min(latencies):.2f}s   Max: {max(latencies):.2f}s")
        print("=" * 65)
    else:
        print("No results recorded.")


if __name__ == "__main__":
    asyncio.run(main())
