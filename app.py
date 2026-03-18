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
    """Debug endpoint to inspect lineup tab DOM."""
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
        url = f"{BASE_URL}/kamp/{match_id}/"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(3000)

        # Click Opstilling tab
        clicked = await page.evaluate("""() => {
            const buttons = document.querySelectorAll('button[role="tab"]');
            for (const btn of buttons) {
                if (btn.textContent.trim() === 'Opstilling') {
                    btn.click();
                    return btn.textContent.trim();
                }
            }
            return null;
        }""")

        await page.wait_for_timeout(3000)

        debug = await page.evaluate("""() => {
            const info = {};

            // All elements with lf__ in class
            info.lf_elements = [];
            document.querySelectorAll('[class*="lf__"]').forEach(el => {
                info.lf_elements.push({
                    tag: el.tagName,
                    className: el.className.substring(0, 100),
                    text: el.textContent.trim().substring(0, 100),
                    childCount: el.children.length,
                });
            });

            // All elements with lineup in class
            info.lineup_elements = [];
            document.querySelectorAll('[class*="lineup"], [class*="Lineup"]').forEach(el => {
                info.lineup_elements.push({
                    tag: el.tagName,
                    className: el.className.substring(0, 100),
                    text: el.textContent.trim().substring(0, 200),
                    childCount: el.children.length,
                });
            });

            // All elements with formation in class
            info.formation_elements = [];
            document.querySelectorAll('[class*="formation"], [class*="Formation"]').forEach(el => {
                info.formation_elements.push({
                    tag: el.tagName,
                    className: el.className.substring(0, 100),
                    childCount: el.children.length,
                });
            });

            // The section content after clicking Opstilling
            const sections = document.querySelectorAll('section');
            info.sections = [];
            sections.forEach(s => {
                if (s.textContent.includes('Opstilling') || s.className.includes('lineup') || s.className.includes('lf')) {
                    info.sections.push({
                        className: s.className.substring(0, 100),
                        text: s.textContent.trim().substring(0, 300),
                    });
                }
            });

            info.clicked = arguments && arguments[0];
            return info;
        }""")

        debug["clicked_tab"] = clicked
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
