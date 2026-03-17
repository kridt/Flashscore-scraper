"""Flask web application for browsing and scraping Flashscore.dk fixtures."""

import asyncio
from flask import Flask, render_template, request, jsonify
from scraper import LEAGUES, scrape_fixtures, scrape_match_detail, close_browser

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
        fixtures = run_async(scrape_fixtures(league_id, date_str))
        return jsonify(fixtures)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/match/<match_id>")
def match_page(match_id):
    """Match detail page (renders template, data loaded via JS)."""
    return render_template("match.html", match_id=match_id)


@app.route("/api/match/<match_id>")
def api_match(match_id):
    """Scrape and return match detail (lineups + TV channels)."""
    try:
        detail = run_async(scrape_match_detail(match_id))
        return jsonify(detail)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.teardown_appcontext
def shutdown_browser(exception=None):
    """Clean up browser on app shutdown."""
    pass  # Browser cleanup handled by close_browser()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    try:
        app.run(host="0.0.0.0", port=port)
    finally:
        run_async(close_browser())
