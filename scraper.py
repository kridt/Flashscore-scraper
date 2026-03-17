"""Flashscore.dk scraper using Playwright for fixture lists, lineups, and TV channels."""

import asyncio
from playwright.async_api import async_playwright, Browser, Page

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
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
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
        # Navigate to the league fixtures page
        url = f"{BASE_URL}{league['path']}/kampe/"
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await _dismiss_cookie_banner(page)

        # Wait for match content to load
        await page.wait_for_timeout(2000)

        # Try to navigate to the correct date by using the calendar if needed
        # Flashscore shows upcoming fixtures by default, grouped by round/date

        # Extract all match elements
        fixtures = []

        # Flashscore renders matches in div elements with specific classes
        # Wait for the events container
        await page.wait_for_selector(".sportName", timeout=10000)

        # Get all event rows
        matches = await page.query_selector_all("div.event__match")

        for match in matches:
            try:
                # Extract match ID from the element's id attribute (format: "g_1_XXXXX")
                match_id_attr = await match.get_attribute("id")
                match_id = match_id_attr.replace("g_1_", "") if match_id_attr else None

                if not match_id:
                    continue

                # Extract team names
                home_el = await match.query_selector(".event__participant--home")
                away_el = await match.query_selector(".event__participant--away")

                home_team = (await home_el.inner_text()).strip() if home_el else "Unknown"
                away_team = (await away_el.inner_text()).strip() if away_el else "Unknown"

                # Extract time
                time_el = await match.query_selector(".event__time")
                match_time = (await time_el.inner_text()).strip() if time_el else ""

                # Extract score if available
                home_score_el = await match.query_selector(".event__score--home")
                away_score_el = await match.query_selector(".event__score--away")
                score = None
                if home_score_el and away_score_el:
                    hs = (await home_score_el.inner_text()).strip()
                    as_ = (await away_score_el.inner_text()).strip()
                    if hs and as_:
                        score = f"{hs} - {as_}"

                # Filter by date if the time contains the target date
                # Flashscore shows date in the time column for non-today matches
                fixtures.append({
                    "match_id": match_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "time": match_time,
                    "score": score,
                    "url": f"{BASE_URL}/kamp/{match_id}/",
                })
            except Exception:
                continue

        return fixtures

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
        # Navigate to match page
        url = f"{BASE_URL}/kamp/{match_id}/"
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(1500)

        # Extract team names from the match header
        try:
            home_el = await page.query_selector(".duelParticipant__home .participant__participantName")
            away_el = await page.query_selector(".duelParticipant__away .participant__participantName")
            if home_el:
                result["home_team"] = (await home_el.inner_text()).strip()
            if away_el:
                result["away_team"] = (await away_el.inner_text()).strip()
        except Exception:
            pass

        # Extract time/date
        try:
            time_el = await page.query_selector(".duelParticipant__startTime")
            if time_el:
                result["time"] = (await time_el.inner_text()).strip()
        except Exception:
            pass

        # Extract score
        try:
            score_el = await page.query_selector(".detailScore__wrapper")
            if score_el:
                score_text = (await score_el.inner_text()).strip()
                if score_text and score_text != "-":
                    result["score"] = score_text
        except Exception:
            pass

        # Extract TV channels from the match summary/info section
        await _scrape_tv_channels(page, result)

        # Navigate to lineups tab
        await _scrape_lineups(page, result)

        return result

    finally:
        await page.close()


async def _scrape_tv_channels(page: Page, result: dict):
    """Extract TV channel information from the match page."""
    try:
        # TV info is often in the match info/summary section
        # Look for broadcast/TV elements
        tv_selectors = [
            ".mi__item--tv .mi__item__val",
            "[class*='tv'] [class*='text']",
            ".matchInfoTvItem",
            ".mi__item:has(.tv) .mi__item__val",
        ]

        for selector in tv_selectors:
            try:
                elements = await page.query_selector_all(selector)
                if elements:
                    for el in elements:
                        text = (await el.inner_text()).strip()
                        if text:
                            result["tv_channels"].append(text)
                    if result["tv_channels"]:
                        return
            except Exception:
                continue

        # Fallback: look for any elements containing known Danish TV channel names
        # by scanning the page content
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

    except Exception:
        pass


async def _scrape_lineups(page: Page, result: dict):
    """Navigate to and extract lineup information."""
    try:
        # Click the lineups tab - Danish: "Startopstillinger" or "Opstillinger"
        lineup_tab = None
        tab_selectors = [
            "a[href*='startopstillinger']",
            "a[href*='lineups']",
            "button:has-text('Startopstillinger')",
            "a:has-text('Startopstillinger')",
            "a:has-text('Opstillinger')",
        ]

        for selector in tab_selectors:
            try:
                tab = page.locator(selector).first
                if await tab.is_visible(timeout=1000):
                    lineup_tab = tab
                    break
            except Exception:
                continue

        if not lineup_tab:
            # Try clicking through available tabs to find lineups
            tabs = await page.query_selector_all(".tabs__tab a, .subTabs a")
            for tab in tabs:
                text = (await tab.inner_text()).strip().lower()
                if "opstilling" in text or "lineup" in text or "startopstilling" in text:
                    lineup_tab = tab
                    break

        if lineup_tab:
            await lineup_tab.click()
            await page.wait_for_timeout(2000)

        # Extract lineup data
        # Flashscore lineups are typically in a section with home/away columns

        # Try the standard lineup container selectors
        lineup_selectors = [
            ".lf__side",          # lineup formation side
            ".lineupsSide",
            ".section--lineups",
        ]

        for selector in lineup_selectors:
            sides = await page.query_selector_all(selector)
            if len(sides) >= 2:
                result["home_lineup"] = await _extract_players(sides[0])
                result["away_lineup"] = await _extract_players(sides[1])
                break

        # If the above didn't work, try individual player elements
        if not result["home_lineup"]:
            # Try extracting from lineup rows
            lineup_rows = await page.query_selector_all(".lf__lineupRow, .lineupRow")
            if lineup_rows:
                # Split into home and away based on position or container
                home_section = await page.query_selector(".section--homeTeam, [class*='home'] .lf__lineupRow")
                away_section = await page.query_selector(".section--awayTeam, [class*='away'] .lf__lineupRow")

                if home_section:
                    result["home_lineup"] = await _extract_players(home_section)
                if away_section:
                    result["away_lineup"] = await _extract_players(away_section)

        # Extract substitutes
        sub_selectors = [
            ".lf__subs",
            ".lineupsSubs",
        ]

        for selector in sub_selectors:
            subs = await page.query_selector_all(selector)
            if len(subs) >= 2:
                result["home_substitutes"] = await _extract_players(subs[0])
                result["away_substitutes"] = await _extract_players(subs[1])
                break

    except Exception:
        pass


async def _extract_players(container) -> list[dict]:
    """Extract player information from a lineup container element."""
    players = []
    try:
        # Look for individual player elements
        player_selectors = [
            ".lf__cell",
            ".lineupPlayer",
            "[class*='player']",
        ]

        player_elements = []
        for selector in player_selectors:
            player_elements = await container.query_selector_all(selector)
            if player_elements:
                break

        for player_el in player_elements:
            try:
                # Extract player name
                name_el = await player_el.query_selector(
                    ".lf__cellName, .playerName, [class*='name']"
                )
                name = (await name_el.inner_text()).strip() if name_el else ""

                # Extract shirt number
                number_el = await player_el.query_selector(
                    ".lf__cellNumber, .playerNumber, [class*='number']"
                )
                number = (await number_el.inner_text()).strip() if number_el else ""

                # Extract position if available
                pos_el = await player_el.query_selector(
                    ".lf__cellPosition, .playerPosition, [class*='position']"
                )
                position = (await pos_el.inner_text()).strip() if pos_el else ""

                if name:
                    players.append({
                        "name": name,
                        "number": number,
                        "position": position,
                    })
            except Exception:
                continue

    except Exception:
        pass

    return players
