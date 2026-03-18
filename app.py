"""Flask web application for browsing and scraping Flashscore.dk fixtures."""

import asyncio
import logging
import traceback
from flask import Flask, render_template, request, jsonify
from scraper import LEAGUES, scrape_fixtures, scrape_match_detail, close_browser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def run_async(coro):
    """Run an async coroutine from synchronous Flask context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


@app.route("/health")
def health():
    """Lightweight health check endpoint for Railway."""
    return "ok", 200


@app.route("/")
def index():
    """Home page with date picker and league selector."""
    return render_template("index.html", leagues=LEAGUES)


@app.route("/api/leagues")
def api_leagues():
    """Return available leagues."""
    return jsonify(
        [{"id": k, "name": v["name"]} for k, v in LEAGUES.items()]
    )


@app.route("/api/fixtures")
def api_fixtures():
    """Scrape and return fixtures for a league and date."""
    league_id = request.args.get("league", "superliga")
    date_str = request.args.get("date", "")

    if not date_str:
        return jsonify({"error": "Date parameter is required"}), 400

    if league_id not in LEAGUES:
        return jsonify({"error": f"Unknown league: {league_id}"}), 400

    try:
        logger.info(f"Scraping fixtures: league={league_id}, date={date_str}")
        fixtures = run_async(scrape_fixtures(league_id, date_str))
        logger.info(f"Got {len(fixtures)} fixtures")
        return jsonify(fixtures)
    except Exception as e:
        logger.error(f"Fixtures error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route("/match/<match_id>")
def match_page(match_id):
    """Match detail page (renders template, data loaded via JS)."""
    return render_template("match.html", match_id=match_id)


@app.route("/api/match/<match_id>")
def api_match(match_id):
    """Scrape and return match detail (lineups + TV channels)."""
    try:
        logger.info(f"Scraping match detail: {match_id}")
        detail = run_async(scrape_match_detail(match_id))
        return jsonify(detail)
    except Exception as e:
        logger.error(f"Match detail error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route("/debug/lineup/<match_id>")
def debug_lineup(match_id):
    """Debug: dump lineup DOM after navigating to lineup page."""
    try:
        detail = run_async(_debug_lineup(match_id))
        return jsonify(detail)
    except Exception as e:
        logger.error(f"Debug error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


async def _debug_lineup(match_id):
    from scraper import get_browser, _dismiss_cookie_banner, BASE_URL
    browser = await get_browser()
    page = await browser.new_page()
    try:
        # First load the match page to get the lineup URL
        url = f"{BASE_URL}/kamp/{match_id}/"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(3000)

        lineup_url = await page.evaluate("""() => {
            for (const a of document.querySelectorAll('a')) {
                if (a.textContent.trim() === 'Opstilling') return a.href;
            }
            return null;
        }""")

        if lineup_url:
            await page.goto(lineup_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(5000)

        debug = await page.evaluate("""() => {
            const info = {};
            info.url = window.location.href;

            // Get ALL elements with their class names (limited)
            info.lf_elements = [];
            document.querySelectorAll('[class*="lf__"]').forEach(el => {
                info.lf_elements.push({
                    tag: el.tagName,
                    cls: el.className.substring(0, 120),
                    text: el.textContent.trim().substring(0, 80),
                    kids: el.children.length,
                });
            });

            // lineup/formation elements
            info.lineup_els = [];
            document.querySelectorAll('[class*="lineup"], [class*="Lineup"], [class*="formation"], [class*="Formation"]').forEach(el => {
                info.lineup_els.push({
                    tag: el.tagName,
                    cls: el.className.substring(0, 120),
                    kids: el.children.length,
                    text: el.textContent.trim().substring(0, 100),
                });
            });

            // section elements
            info.sections = [];
            document.querySelectorAll('section').forEach(el => {
                const cls = el.className;
                if (cls.includes('lf') || cls.includes('lineup') || cls.includes('formation')) {
                    info.sections.push({
                        cls: cls.substring(0, 120),
                        text: el.textContent.trim().substring(0, 200),
                    });
                }
            });

            // wcl-participant elements (player names)
            info.participants = [];
            document.querySelectorAll('[class*="wcl-participant"]').forEach(el => {
                info.participants.push({
                    cls: el.className.substring(0, 100),
                    text: el.textContent.trim().substring(0, 50),
                });
            });

            return info;
        }""")

        debug["lineup_url"] = lineup_url
        return debug
    finally:
        await page.close()


@app.teardown_appcontext
def shutdown_browser(exception=None):
    """Clean up browser on app shutdown."""
    pass  # Browser cleanup handled by close_browser()


if __name__ == "__main__":
    import os
    import sys
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask on 0.0.0.0:{port}", flush=True)
    sys.stdout.flush()
    try:
        app.run(host="0.0.0.0", port=port)
    finally:
        run_async(close_browser())
