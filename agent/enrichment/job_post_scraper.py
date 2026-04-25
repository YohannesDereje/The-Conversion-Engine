"""Playwright-based job posting scraper with SerpAPI fallback."""
import asyncio
import os
import re
import time
from datetime import datetime, timezone
from urllib.robotparser import RobotFileParser

import httpx

# Rule 4: user agent must identify the program
_USER_AGENT = "TRP1-Week10-Research (trainee@trp1.example)"


async def _robots_allows(domain: str, path: str) -> bool:
    """Return True if robots.txt permits _USER_AGENT to fetch the given path.

    Fails open (returns True) if robots.txt is unreachable — conservative but
    preferable to silently skipping valid targets.
    """
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            r = await client.get(
                f"https://{domain}/robots.txt",
                headers={"User-Agent": _USER_AGENT},
            )
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(_USER_AGENT, f"https://{domain}{path}")
    except Exception:
        return True


async def scrape_job_postings(
    company_domain: str,
    company_name: str = "",
    trace_id: str = "",
) -> dict:
    """
    Scrape open job postings for a company domain.
    Fallback chain: Playwright → SerpAPI → no_data.
    Returns: open_roles_today, role_titles, sources, status, scraped_at.
    Always returns a valid dict — never raises.
    """
    result = {
        "open_roles_today": 0,
        "role_titles": [],
        "sources": [],
        "status": "no_data",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    if not company_domain:
        return result

    domain = company_domain.replace("https://", "").replace("http://", "").rstrip("/")
    slug = _domain_to_slug(domain)

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=_USER_AGENT)

            wf_titles = await _scrape_wellfound(ctx, slug)
            if wf_titles:
                result["role_titles"].extend(wf_titles)
                result["sources"].append("wellfound")

            careers_titles = await _scrape_careers_page(ctx, domain)
            if careers_titles:
                new = [t for t in careers_titles if t not in result["role_titles"]]
                result["role_titles"].extend(new)
                if "company_careers_page" not in result["sources"]:
                    result["sources"].append("company_careers_page")

            await browser.close()

    except Exception:
        result["status"] = "error"

    result["role_titles"] = _dedupe(result["role_titles"])[:30]
    result["open_roles_today"] = len(result["role_titles"])

    if result["open_roles_today"] > 0:
        result["status"] = "success"
        return result

    # P3-A1: SerpAPI fallback when Playwright returns no_data
    query = company_name or slug
    serpapi_titles = await _serpapi_search_jobs(query, trace_id)
    if serpapi_titles:
        result["role_titles"] = serpapi_titles
        result["open_roles_today"] = len(serpapi_titles)
        result["sources"] = ["serpapi"]
        result["status"] = "partial"
    # else status stays "no_data"
    return result


async def _serpapi_search_jobs(company_name: str, trace_id: str = "") -> list:
    """SerpAPI Google Jobs fallback. Rule 4: 2-second delay before call."""
    api_key = os.getenv("SERPAPI_API_KEY", "")
    if not api_key or not company_name:
        return []

    await asyncio.sleep(2)  # Rule 4: 2-second inter-request delay

    t0 = time.monotonic()
    titles: list = []
    status = "no_data"
    try:
        from serpapi import GoogleSearch  # google-search-results package

        params = {
            "engine": "google_jobs",
            "q": f"{company_name} jobs",
            "api_key": api_key,
        }
        search = GoogleSearch(params)
        data = search.get_dict()
        for job in (data.get("jobs_results") or [])[:30]:
            title = (job.get("title") or "").strip()
            if _is_job_title(title):
                titles.append(title)
        titles = _dedupe(titles)[:20]
        status = "success" if titles else "no_data"
    except Exception:
        status = "error"

    latency_ms = (time.monotonic() - t0) * 1000
    if trace_id:
        try:
            from agent.utils import emit_span
            emit_span(
                trace_id=trace_id,
                name="job_scraper_serpapi",
                input={"company_name": company_name, "source": "serpapi"},
                output={"status": status, "roles_found": len(titles)},
                latency_ms=latency_ms,
            )
        except Exception:
            pass

    return titles


def _domain_to_slug(domain: str) -> str:
    name = domain.split(".")[0]
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")


async def _scrape_wellfound(ctx, slug: str) -> list:
    # P2-F: robots.txt check for wellfound.com before scraping
    if not await _robots_allows("wellfound.com", f"/company/{slug}/jobs"):
        return []
    try:
        page = await ctx.new_page()
        await page.goto(
            f"https://wellfound.com/company/{slug}/jobs",
            timeout=15000,
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(2500)

        titles = []
        for sel in ["h2", "h3", "[class*='title']", "a[href*='/jobs/']"]:
            els = await page.query_selector_all(sel)
            for el in els[:40]:
                text = (await el.inner_text()).strip()
                if _is_job_title(text):
                    titles.append(text)
            if len(titles) >= 5:
                break

        await page.close()
        return _dedupe(titles)
    except Exception:
        try:
            await page.close()
        except Exception:
            pass
        return []


async def _scrape_careers_page(ctx, domain: str) -> list:
    for path in ["/careers", "/jobs", "/about/careers", "/work-with-us", "/open-roles"]:
        # P2-F: check robots.txt before each path attempt
        if not await _robots_allows(domain, path):
            continue
        try:
            # P2-E: Rule 4 — 2-second delay between requests to the same domain
            await asyncio.sleep(2)
            page = await ctx.new_page()
            await page.goto(
                f"https://{domain}{path}",
                timeout=12000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(1500)

            titles = []
            for sel in ["h2", "h3", "h4", "[class*='job']", "[class*='role']", "[class*='position']"]:
                els = await page.query_selector_all(sel)
                for el in els[:60]:
                    try:
                        text = (await el.inner_text()).strip()
                        if _is_job_title(text):
                            titles.append(text)
                    except Exception:
                        pass

            await page.close()
            if titles:
                return _dedupe(titles)[:25]
        except Exception:
            try:
                await page.close()
            except Exception:
                pass

    return []


_JOB_PATTERN = re.compile(
    r"\b(engineer|developer|scientist|analyst|manager|director|lead|architect|"
    r"designer|product|data|ml|ai|software|backend|frontend|full.?stack|devops|"
    r"platform|infrastructure|security|mobile|qa|sre|research|nlp|llm|mlops|"
    r"python|golang|typescript|react)\b",
    re.IGNORECASE,
)


def _is_job_title(text: str) -> bool:
    return bool(text and 4 < len(text) < 100 and _JOB_PATTERN.search(text))


def _dedupe(lst: list) -> list:
    seen, out = set(), []
    for item in lst:
        key = item.lower().strip()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
