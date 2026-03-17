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
    # Pattern: initialFeeds["key"] = "data" or initialFeeds['key'] = 'data'
    feed_strings = re.findall(
        r'initialFeeds\s*\[\s*["\'][^"\']*["\']\s*\]\s*=\s*["\'](.+?)["\'](?:\s*[;])',
        html,
        re.DOTALL,
    )

    if not feed_strings:
        # Fallback: try to find any large data string with the ÷ delimiter
        feed_strings = re.findall(r'["\']([^"\']*AA÷[^"\']{50,})["\']', html)

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


async def scrape_fixtures(league_id: str, date_str: str) -> list[dict]:
    """
    Scrape fixtures for a given league and date.

    Args:
        league_id: Key from LEAGUES dict (e.g. "superliga")
        date_str: Date in YYYY-MM-DD format

    Returns:
        List of fixture dicts with match_id, home_team, away_team, time, score, url
    """
    league = LEAGUES.get(league_id)
    if not league:
        return []

    browser = await get_browser()
    page = await browser.new_page()

    try:
        # Try both fixtures and results pages to find matches for the date
        urls_to_try = [
            f"{BASE_URL}{league['path']}/kampe/",
            f"{BASE_URL}{league['path']}/resultater/",
        ]

        all_matches = []
        seen_ids = set()

        for url in urls_to_try:
            logger.info(f"Fetching {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _dismiss_cookie_banner(page)
            # Give JS a moment to set variables, but don't wait for full render
            await page.wait_for_timeout(1000)

            html = await page.content()
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

    finally:
        await page.close()


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
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(2000)

        # Extract basic match info using JS evaluation for robustness
        info = await page.evaluate("""() => {
            const result = {};

            // Team names - try multiple selector patterns
            const homeSelectors = [
                '.duelParticipant__home .participant__participantName a',
                '.duelParticipant__home .participant__participantName',
                '[class*="home"] [class*="participantName"]',
            ];
            const awaySelectors = [
                '.duelParticipant__away .participant__participantName a',
                '.duelParticipant__away .participant__participantName',
                '[class*="away"] [class*="participantName"]',
            ];

            for (const sel of homeSelectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) {
                    result.home_team = el.textContent.trim();
                    break;
                }
            }
            for (const sel of awaySelectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) {
                    result.away_team = el.textContent.trim();
                    break;
                }
            }

            // Score
            const scoreEl = document.querySelector('[class*="detailScore"] [class*="wrapper"]') ||
                           document.querySelector('[class*="detailScore"]');
            if (scoreEl) {
                const text = scoreEl.textContent.trim().replace(/\\s+/g, ' ');
                if (text && text !== '-') result.score = text;
            }

            // Start time
            const timeEl = document.querySelector('[class*="startTime"]') ||
                          document.querySelector('[class*="matchTime"]');
            if (timeEl) result.time = timeEl.textContent.trim();

            return result;
        }""")

        result.update({k: v for k, v in info.items() if v})

        # Extract TV channels
        await _scrape_tv_channels(page, result)

        # Extract lineups
        await _scrape_lineups(page, result)

        return result

    finally:
        await page.close()


async def _scrape_tv_channels(page: Page, result: dict):
    """Extract TV channel information from the match page."""
    try:
        channels = await page.evaluate("""() => {
            const channels = [];
            // Try various selectors for TV info
            const selectors = [
                '[class*="tv"] [class*="text"]',
                '[class*="tv"] [class*="val"]',
                '[class*="broadcast"]',
                '.mi__item--tv .mi__item__val',
            ];
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                els.forEach(el => {
                    const text = el.textContent.trim();
                    if (text) channels.push(text);
                });
                if (channels.length > 0) break;
            }
            return channels;
        }""")

        if channels:
            result["tv_channels"] = channels
            return

        # Fallback: scan page text for known Danish TV channels
        content = await page.content()
        known_channels = [
            "TV2 Sport", "TV 2 Sport", "Viaplay", "TV3 Sport", "TV3+",
            "Canal 9", "Eurosport", "6'eren", "TV 2", "DR1", "DR2",
            "Kanal 5", "Discovery+",
        ]
        for channel in known_channels:
            if channel.lower() in content.lower():
                if channel not in result["tv_channels"]:
                    result["tv_channels"].append(channel)

    except Exception as e:
        logger.error(f"TV channel scraping error: {e}")


async def _scrape_lineups(page: Page, result: dict):
    """Navigate to and extract lineup information."""
    try:
        # Try to click the lineups tab
        clicked = await page.evaluate("""() => {
            // Find lineup tab by text content
            const links = document.querySelectorAll('a, button');
            for (const link of links) {
                const text = link.textContent.trim().toLowerCase();
                if (text.includes('opstilling') || text.includes('lineup') || text.includes('startopstilling')) {
                    link.click();
                    return true;
                }
            }
            return false;
        }""")

        if clicked:
            await page.wait_for_timeout(2000)

        # Extract lineup data using JS
        lineups = await page.evaluate("""() => {
            const result = { home: [], away: [], homeSubs: [], awaySubs: [] };

            // Try various lineup container selectors
            const sideSelectors = ['.lf__side', '[class*="lineupsSide"]', '[class*="lineup__side"]'];
            let sides = [];
            for (const sel of sideSelectors) {
                sides = document.querySelectorAll(sel);
                if (sides.length >= 2) break;
            }

            function extractPlayers(container) {
                const players = [];
                const playerSelectors = ['.lf__cell', '[class*="lineupPlayer"]', '[class*="player"]'];
                let playerEls = [];
                for (const sel of playerSelectors) {
                    playerEls = container.querySelectorAll(sel);
                    if (playerEls.length > 0) break;
                }
                playerEls.forEach(el => {
                    const nameEl = el.querySelector('[class*="Name"]') || el.querySelector('[class*="name"]');
                    const numEl = el.querySelector('[class*="Number"]') || el.querySelector('[class*="number"]');
                    const name = nameEl ? nameEl.textContent.trim() : el.textContent.trim();
                    const number = numEl ? numEl.textContent.trim() : '';
                    if (name) players.push({ name, number, position: '' });
                });
                return players;
            }

            if (sides.length >= 2) {
                result.home = extractPlayers(sides[0]);
                result.away = extractPlayers(sides[1]);
            }

            // Substitutes
            const subSelectors = ['.lf__subs', '[class*="lineupsSubs"]', '[class*="lineup__subs"]'];
            let subs = [];
            for (const sel of subSelectors) {
                subs = document.querySelectorAll(sel);
                if (subs.length >= 2) break;
            }
            if (subs.length >= 2) {
                result.homeSubs = extractPlayers(subs[0]);
                result.awaySubs = extractPlayers(subs[1]);
            }

            return result;
        }""")

        result["home_lineup"] = lineups.get("home", [])
        result["away_lineup"] = lineups.get("away", [])
        result["home_substitutes"] = lineups.get("homeSubs", [])
        result["away_substitutes"] = lineups.get("awaySubs", [])

    except Exception as e:
        logger.error(f"Lineup scraping error: {e}")
