"""Flashscore.dk scraper using Playwright for fixture lists, lineups, and TV channels."""

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, Browser, Page

CET_TZ = ZoneInfo("Europe/Copenhagen")

logger = logging.getLogger(__name__)

BASE_URL = "https://www.flashscore.dk"

# Popular Danish and European leagues available on flashscore.dk
LEAGUES = {
    "superliga": {"name": "Superliga", "path": "/fodbold/danmark/superliga"},
    "1-division": {"name": "1. Division", "path": "/fodbold/danmark/1-division"},
    "premier-league": {"name": "Premier League", "path": "/fodbold/england/premier-league"},
    "la-liga": {"name": "La Liga", "path": "/fodbold/spanien/la-liga"},
    "bundesliga": {"name": "Bundesliga", "path": "/fodbold/tyskland/bundesliga"},
    "serie-a": {"name": "Serie A", "path": "/fodbold/italien/serie-a"},
    "ligue-1": {"name": "Ligue 1", "path": "/fodbold/frankrig/ligue-1"},
    "champions-league": {"name": "Champions League", "path": "/fodbold/europa/champions-league"},
    "europa-league": {"name": "Europa League", "path": "/fodbold/europa/europa-league"},
}

_playwright = None
_browser: Browser | None = None


async def get_browser() -> Browser:
    """Get or create a singleton browser instance."""
    global _playwright, _browser
    if _browser is None or not _browser.is_connected():
        logger.info("Launching Chromium browser...")
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        logger.info("Browser launched successfully")
    return _browser


async def close_browser():
    """Close the browser and playwright instances."""
    global _playwright, _browser
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


async def _dismiss_cookie_banner(page: Page):
    """Dismiss the cookie consent banner if present."""
    try:
        accept_btn = page.locator("#onetrust-accept-btn-handler")
        await accept_btn.click(timeout=3000)
        await page.wait_for_timeout(500)
    except Exception:
        pass


def _parse_feed_data(html: str) -> list[dict]:
    """Parse Flashscore's feed data from page source into match records.

    Feed data uses ¬ as field separator, ÷ between key and value,
    and ~ as record separator. Match records start with AA÷.
    Flashscore stores multiple feeds (summary-results, summary-fixtures, etc.)
    so we extract all of them.
    """
    # Find ALL feed data assignments in the page
    # Actual format: initialFeeds["summary-results"] = { data: `...` }
    # Uses template literals (backticks) inside an object
    feed_strings = re.findall(
        r'initialFeeds\s*\[\s*["\'][^"\']*["\']\s*\]\s*=\s*\{\s*data:\s*`([^`]+)`',
        html,
        re.DOTALL,
    )

    if not feed_strings:
        # Fallback: try quoted strings (older format)
        feed_strings = re.findall(
            r'initialFeeds\s*\[\s*["\'][^"\']*["\']\s*\]\s*=\s*["\'](.+?)["\'](?:\s*[;])',
            html,
            re.DOTALL,
        )

    if not feed_strings:
        # Last resort: find any large data string with the ÷ delimiter
        feed_strings = re.findall(r'`([^`]*AA÷[^`]{50,})`', html)

    if not feed_strings:
        logger.warning("Could not find feed data in page source")
        return []

    logger.info(f"Found {len(feed_strings)} feed data blocks")

    matches = []
    seen_ids = set()

    for raw in feed_strings:
        # Unescape any JS string escapes
        raw = raw.replace("\\'", "'").replace('\\"', '"')

        # Split into individual records - each match starts with AA÷
        parts = raw.split("~AA÷")

        for i, part in enumerate(parts):
            if i == 0:
                # First part is header/league info before any match
                # But check if it starts with AA÷ itself
                if not part.startswith("AA÷"):
                    continue
                part = part  # Already has AA÷
            else:
                part = "AA÷" + part

            fields = {}
            for field in part.split("¬"):
                if "÷" in field:
                    key, _, value = field.partition("÷")
                    if key in fields:
                        fields[key + "2"] = value
                    else:
                        fields[key] = value

            match_id = fields.get("AA", "")
            if not match_id or match_id in seen_ids:
                continue

            seen_ids.add(match_id)
            matches.append(fields)

    logger.info(f"Parsed {len(matches)} total match records from all feeds")
    return matches


def _filter_by_date(matches: list[dict], date_str: str) -> list[dict]:
    """Filter match records to only include those on the given date (YYYY-MM-DD).

    Uses CET/CEST timezone since flashscore.dk is Danish.
    """
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.error(f"Invalid date format: {date_str}")
        return matches

    filtered = []
    for m in matches:
        ts_str = m.get("AD", "")
        if not ts_str:
            continue
        try:
            ts = int(ts_str)
            match_date = datetime.fromtimestamp(ts, tz=CET_TZ).date()
            if match_date == target:
                filtered.append(m)
        except (ValueError, OSError):
            continue

    return filtered


def _match_fields_to_dict(fields: dict) -> dict:
    """Convert raw feed fields to a clean match dict."""
    match_id = fields.get("AA", "")

    # Home team: AE field, fallback to CX
    home_team = fields.get("AE", fields.get("CX", "Unknown"))
    # Away team: AF field
    away_team = fields.get("AF", "Unknown")

    # Time from timestamp
    ts_str = fields.get("AD", "")
    match_time = ""
    if ts_str:
        try:
            ts = int(ts_str)
            dt = datetime.fromtimestamp(ts, tz=CET_TZ)
            match_time = dt.strftime("%H:%M")
        except (ValueError, OSError):
            pass

    # Score: AG = home goals, AH = away goals
    score = None
    home_goals = fields.get("AG", "")
    away_goals = fields.get("AH", "")
    if home_goals and away_goals:
        score = f"{home_goals} - {away_goals}"

    # Status: AB field (1=not started, 2=live, 3=finished)
    status_code = fields.get("AB", "")
    status = "scheduled"
    if status_code == "3":
        status = "finished"
    elif status_code == "2":
        status = "live"

    return {
        "match_id": match_id,
        "home_team": home_team,
        "away_team": away_team,
        "time": match_time,
        "score": score,
        "status": status,
        "round": fields.get("ER", ""),
        "url": f"{BASE_URL}/kamp/{match_id}/",
    }


def _fetch_page(url: str) -> str:
    """Fetch a page using urllib (no browser needed for feed data)."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "da,en;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


async def scrape_fixtures(league_id: str, date_str: str) -> list[dict]:
    """
    Scrape fixtures for a given league and date.

    Uses plain HTTP requests to fetch the page source and parse feed data
    directly — no browser needed since the data is embedded in the HTML.

    Args:
        league_id: Key from LEAGUES dict (e.g. "superliga")
        date_str: Date in YYYY-MM-DD format

    Returns:
        List of fixture dicts with match_id, home_team, away_team, time, score, url
    """
    league = LEAGUES.get(league_id)
    if not league:
        return []

    # Try both fixtures and results pages to find matches for the date
    urls_to_try = [
        f"{BASE_URL}{league['path']}/kampe/",
        f"{BASE_URL}{league['path']}/resultater/",
    ]

    all_matches = []
    seen_ids = set()

    for url in urls_to_try:
        logger.info(f"Fetching {url}")
        try:
            html = _fetch_page(url)
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            continue

        raw_matches = _parse_feed_data(html)
        logger.info(f"Parsed {len(raw_matches)} total matches from {url}")

        filtered = _filter_by_date(raw_matches, date_str)
        logger.info(f"Found {len(filtered)} matches for date {date_str}")

        for m in filtered:
            mid = m.get("AA", "")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                all_matches.append(_match_fields_to_dict(m))

        if all_matches:
            break  # Found matches, no need to try next URL

    # Sort by time
    all_matches.sort(key=lambda x: x["time"])
    return all_matches


async def scrape_match_detail(match_id: str) -> dict:
    """
    Scrape match detail including lineups and TV channels.

    Args:
        match_id: Flashscore match ID

    Returns:
        Dict with teams, lineups, substitutes, and TV channels
    """
    browser = await get_browser()
    page = await browser.new_page()

    result = {
        "match_id": match_id,
        "home_team": "",
        "away_team": "",
        "time": "",
        "score": None,
        "home_lineup": [],
        "away_lineup": [],
        "home_substitutes": [],
        "away_substitutes": [],
        "tv_channels": [],
    }

    try:
        url = f"{BASE_URL}/kamp/{match_id}/"
        logger.info(f"Fetching match detail: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _dismiss_cookie_banner(page)

        # Wait for match content to render
        try:
            await page.wait_for_selector(
                '[class*="participantName"]', timeout=8000
            )
        except Exception:
            logger.warning("Timed out waiting for participant names")

        # Extract basic match info + TV channels in one evaluate call
        info = await page.evaluate("""() => {
            const result = {};

            // Team names: use duelParticipant home/away containers
            const homeContainer = document.querySelector(
                '[class*="duelParticipant__home"]'
            );
            const awayContainer = document.querySelector(
                '[class*="duelParticipant__away"]'
            );
            if (homeContainer) {
                const nameEl = homeContainer.querySelector(
                    '[class*="participantName"]'
                );
                if (nameEl) result.home_team = nameEl.textContent.trim();
            }
            if (awayContainer) {
                const nameEl = awayContainer.querySelector(
                    '[class*="participantName"]'
                );
                if (nameEl) result.away_team = nameEl.textContent.trim();
            }

            // Fallback: parse from page title "TOT - ATM | Home vs Away"
            if (!result.home_team || !result.away_team) {
                const title = document.title;
                const vsMatch = title.match(/\\|\\s*(.+?)\\s+vs\\s+(.+?)\\s+(LIVE|$)/i);
                if (vsMatch) {
                    if (!result.home_team) result.home_team = vsMatch[1].trim();
                    if (!result.away_team) result.away_team = vsMatch[2].trim();
                }
            }

            // Score
            const scoreEl = document.querySelector('[class*="detailScore"]');
            if (scoreEl) {
                const text = scoreEl.textContent.trim().replace(/\\s+/g, ' ');
                if (text && text !== '-') result.score = text;
            }

            // Start time
            const timeEl = document.querySelector('[class*="startTime"]');
            if (timeEl) result.time = timeEl.textContent.trim();

            // TV channels
            result.tv_channels = [];
            const tvSection = document.querySelector(
                '[data-testid="wcl-summaryTvStreaming"]'
            );
            if (tvSection) {
                const channelsDiv = tvSection.querySelector('[class*="channel"]') ||
                                   tvSection.querySelector('[class*="Channel"]');
                const container = channelsDiv || tvSection;

                container.querySelectorAll('a, [class*="tvStation"]').forEach(el => {
                    const text = el.textContent.trim();
                    if (text && !result.tv_channels.includes(text) && text !== 'TV Kanal') {
                        result.tv_channels.push(text);
                    }
                });

                if (result.tv_channels.length === 0 && channelsDiv) {
                    channelsDiv.querySelectorAll('*').forEach(el => {
                        if (el.children.length === 0) {
                            const text = el.textContent.trim();
                            if (text && !result.tv_channels.includes(text) && text !== 'TV Kanal') {
                                result.tv_channels.push(text);
                            }
                        }
                    });
                }
            }

            return result;
        }""")

        tv_channels = info.pop("tv_channels", [])
        result.update({k: v for k, v in info.items() if v})
        if tv_channels:
            result["tv_channels"] = tv_channels

        # Find and navigate to lineup page
        await _scrape_lineups_via_tab(page, result)

        return result

    finally:
        await page.close()


async def _scrape_lineups_via_tab(page: Page, result: dict):
    """Find the lineup tab link and navigate to the lineup page."""
    try:
        # Find the lineup link href from the match page
        # URL format: .../oversigt/opstilling/?mid=...
        lineup_url = await page.evaluate("""() => {
            const links = document.querySelectorAll('a');
            for (const a of links) {
                const href = a.href || '';
                const text = a.textContent.trim().toLowerCase();
                if (href.includes('opstilling') || href.includes('lineup') ||
                    text === 'opstilling' || text === 'lineups') {
                    return a.href;
                }
            }
            // Return all link info for debugging if not found
            return null;
        }""")

        if not lineup_url:
            # Log available links for debugging
            tabs = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('a')).map(
                    a => ({text: a.textContent.trim(), href: a.href})
                ).filter(a => a.text.length > 0 && a.text.length < 40);
            }""")
            logger.warning(f"Lineup link not found. Available links: {tabs[:25]}")
            return

        logger.info(f"Navigating to lineup page: {lineup_url}")
        await page.goto(lineup_url, wait_until="domcontentloaded", timeout=20000)
        await _dismiss_cookie_banner(page)

        # Wait for actual lineup content (not skeleton loading placeholders)
        # lf__skeleton appears immediately but contains no data;
        # lf__sidesBox / lf__participant only appear when real data loads
        content_found = False
        for selector in ['[class*="lf__sidesBox"]', '[class*="lf__participant"]',
                         '[class*="lineup"]', '[class*="formation"]']:
            try:
                await page.wait_for_selector(selector, timeout=15000)
                logger.info(f"Lineup content loaded with selector: {selector}")
                content_found = True
                break
            except Exception:
                continue

        if not content_found:
            debug = await page.evaluate("""() => {
                const classes = new Set();
                document.querySelectorAll('[class]').forEach(el => {
                    el.classList.forEach(c => {
                        if (c.includes('lf') || c.includes('lineup') ||
                            c.includes('side') || c.includes('participant') ||
                            c.includes('formation') || c.includes('player')) {
                            classes.add(c);
                        }
                    });
                });
                return {
                    url: window.location.href,
                    classes: Array.from(classes).slice(0, 30),
                    bodyLen: document.body.innerHTML.length
                };
            }""")
            logger.warning(f"No lineup content found after navigation. Debug: {debug}")
            return

        # Extract lineup data, identifying which side is home vs away
        home_team = result.get("home_team", "")
        away_team = result.get("away_team", "")
        lineups = await page.evaluate("""(teamNames) => {
            const result = { sides: [], debug: {} };

            function extractPlayers(container) {
                const players = [];
                const selectors = [
                    '[class*="lf__participantNew"]',
                    '[class*="lf__participant"]'
                ];
                let playerEls = [];
                for (const sel of selectors) {
                    playerEls = container.querySelectorAll(sel);
                    if (playerEls.length > 0) break;
                }
                playerEls.forEach(el => {
                    const text = el.textContent.trim();
                    if (!text) return;
                    const match = text.match(/^(\\d+)(.+)/);
                    if (match) {
                        players.push({
                            number: match[1],
                            name: match[2].trim(),
                            position: '',
                        });
                    } else {
                        players.push({ number: '', name: text, position: '' });
                    }
                });
                return players;
            }

            // Find team header/label inside each side
            function findTeamLabel(sideEl) {
                // Look for header elements within the side
                const headerSels = [
                    '[class*="lf__header"]',
                    '[class*="header"]',
                    '[class*="teamName"]',
                    '[class*="team"]'
                ];
                for (const sel of headerSels) {
                    const el = sideEl.querySelector(sel);
                    if (el) {
                        const text = el.textContent.trim();
                        if (text) return text;
                    }
                }
                return '';
            }

            let sidesBoxes = document.querySelectorAll('[class*="lf__sidesBox"]');
            if (sidesBoxes.length === 0) {
                sidesBoxes = document.querySelectorAll('[class*="sidesBox"]');
            }

            if (sidesBoxes.length >= 1) {
                let sides = sidesBoxes[0].querySelectorAll('[class*="lf__side"]');
                if (sides.length === 0) {
                    sides = sidesBoxes[0].querySelectorAll('[class*="side"]');
                }
                for (let i = 0; i < sides.length; i++) {
                    result.sides.push({
                        index: i,
                        label: findTeamLabel(sides[i]),
                        players: extractPlayers(sides[i])
                    });
                }
            }

            // Also check for home/away indicators in the page header
            const homeEl = document.querySelector('[class*="duelParticipant__home"] [class*="participantName"]');
            const awayEl = document.querySelector('[class*="duelParticipant__away"] [class*="participantName"]');
            result.debug.pageHome = homeEl ? homeEl.textContent.trim() : '';
            result.debug.pageAway = awayEl ? awayEl.textContent.trim() : '';
            result.debug.sidesCount = sidesBoxes.length;

            return result;
        }""", {"home": home_team, "away": away_team})

        sides = lineups.get("sides", [])
        page_home = lineups.get("debug", {}).get("pageHome", "")
        page_away = lineups.get("debug", {}).get("pageAway", "")

        logger.info(f"Lineup sides: {len(sides)}, "
                     f"page home={page_home}, page away={page_away}, "
                     f"side labels={[s.get('label', '') for s in sides]}")

        if len(sides) >= 2:
            side0 = sides[0]
            side1 = sides[1]

            # Try to match sides to home/away using labels or page header
            # The page header identifies home (left) and away (right),
            # and the lineup sides follow the same left-right order
            side0_label = side0.get("label", "").lower()
            side1_label = side1.get("label", "").lower()
            home_lower = (page_home or home_team).lower()
            away_lower = (page_away or away_team).lower()

            # Check if labels help identify which side is which
            if side0_label and away_lower and away_lower in side0_label:
                # Side 0 is actually away, swap
                logger.info(f"Swapping sides: side0 label '{side0_label}' matches away team")
                result["home_lineup"] = side1.get("players", [])
                result["away_lineup"] = side0.get("players", [])
            elif side1_label and home_lower and home_lower in side1_label:
                # Side 1 is actually home, swap
                logger.info(f"Swapping sides: side1 label '{side1_label}' matches home team")
                result["home_lineup"] = side1.get("players", [])
                result["away_lineup"] = side0.get("players", [])
            else:
                # Default: sides follow page order (home=left=0, away=right=1)
                result["home_lineup"] = side0.get("players", [])
                result["away_lineup"] = side1.get("players", [])

            logger.info(f"Final lineups: {len(result['home_lineup'])} home, "
                         f"{len(result['away_lineup'])} away")

    except Exception as e:
        logger.error(f"Lineup scraping error: {e}")
