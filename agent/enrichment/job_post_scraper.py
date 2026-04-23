"""Playwright-based job posting scraper with graceful fallback."""
import re
from datetime import datetime, timezone


async def scrape_job_postings(company_domain: str) -> dict:
    """
    Scrape open job postings for a company domain.
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
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

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
        return result

    result["role_titles"] = _dedupe(result["role_titles"])[:30]
    result["open_roles_today"] = len(result["role_titles"])
    result["status"] = "success" if result["open_roles_today"] > 0 else "no_data"
    return result


def _domain_to_slug(domain: str) -> str:
    name = domain.split(".")[0]
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")


async def _scrape_wellfound(ctx, slug: str) -> list:
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
        try:
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
