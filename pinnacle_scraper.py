"""
Pinnacle targeted odds scraper.
- Navigates to each match detail page and captures all market data
- Also captures /matchups/{id}/related to decode special market prices
  (BTTS, Double Chance, HT/FT etc. use participantId-based prices)
- Outputs decimal odds only, real team names everywhere
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Response


# ---------------------------------------------------------------------------
# CONFIGURE YOUR MATCHES HERE
# ---------------------------------------------------------------------------

MATCHES_TO_TRACK = [
    ("Liverpool", "Crystal Palace"),
    ("Fulham", "Aston Villa"),          # Premier League - 12:30 BST
("Liverpool", "Crystal Palace"),    # Premier League - 15:00 BST
("West Ham United", "Everton"),     # Premier League - 15:00 BST
("Wolverhampton Wanderers", "Tottenham Hotspur"),  # Premier League - 15:00 BST
("Arsenal", "Newcastle United"),    # Premier League - 17:30 BST
("Manchester City", "Southampton"), # FA Cup Semi-Final - 17:15 BST (Wembley)

("Deportivo Alavés", "Mallorca"),  # La Liga - ~13:00 local
("Getafe", "Barcelona"),            # La Liga - ~15:15 local
("Valencia", "Girona"),             # La Liga - ~17:30 local
("Atlético Madrid", "Athletic Bilbao"),  # La Liga - ~20:00 local

("Bologna", "Roma"),                # Serie A (prominent Saturday fixture)
("Hellas Verona", "Lecce"),         # Serie A
("Parma", "Pisa"),                  # Serie A

("Mainz 05", "Bayern Munich"),      # Bundesliga (highlight)
# Additional Bundesliga matches typically kick off simultaneously around 15:30 local (e.g. involving Leverkusen, Dortmund, Leipzig, etc.)

("Angers", "Paris Saint-Germain"),  # Ligue 1 (PSG highlight)
("Lyon", "Auxerre"),                # Ligue 1
("Toulouse", "Monaco"),             # Ligue 1

("Benfica", "Moreirense"),          # Primeira Liga - evening highlight
("Vitória Guimarães", "Rio Ave")    # Primeira Liga
]

# ---------------------------------------------------------------------------

OUTPUT_DIR = Path("odds_output")
RAW_DEBUG_FILE = Path("pinnacle_raw_markets.json")
RAW_RELATED_DEBUG = Path("pinnacle_raw_related.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


def american_to_decimal(american: float) -> float:
    if american > 0:
        return round(american / 100 + 1, 4)
    return round(100 / abs(american) + 1, 4)


# ---------------------------------------------------------------------------
# Browser scraping
# ---------------------------------------------------------------------------

def scrape_matches(targets: list[tuple[str, str]]) -> tuple[list[dict], list[dict], dict]:
    all_matchups: list[dict] = []
    all_markets: list[dict] = []
    all_related: dict[int, list] = {}   # main_matchup_id → related matchup objects
    seen_mkt: set = set()
    captured_api_key: list[str] = []

    def on_response(response: Response):
        url = response.url
        if "arcadia.pinnacle.com" not in url:
            return
        try:
            if not captured_api_key:
                key = response.request.headers.get("x-api-key", "")
                if key:
                    captured_api_key.append(key)

            path = urlparse(url).path
            body = response.json()
            if not isinstance(body, list) or not body:
                return

            # Must check /related BEFORE /matchups to avoid routing related data
            # into the main matchups list
            if "/related" in path and "/matchups/" in path:
                m = re.search(r"/matchups/(\d+)/related", path)
                if m:
                    main_id_val = int(m.group(1))
                    if main_id_val not in all_related:
                        all_related[main_id_val] = body
            elif "/matchups" in path:
                all_matchups.extend(body)
            elif "/markets" in path:
                for item in body:
                    k = (
                        item.get("matchupId"), item.get("type"),
                        item.get("period"), item.get("side"),
                        item.get("key"), item.get("isAlternate"),
                    )
                    if k not in seen_mkt:
                        seen_mkt.add(k)
                        all_markets.append(item)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = ctx.new_page()
        page.on("response", on_response)

        print("Loading Pinnacle soccer matchups page …")
        page.goto("https://www.pinnacle.com/en/soccer/matchups/",
                  wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2_000)

        seen_ids: set = set()
        unique: list[dict] = []
        for m in all_matchups:
            mid = m.get("id")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                unique.append(m)

        target_matchups = []
        target_hrefs: dict[int, str] = {}
        for home_q, away_q in targets:
            m = find_matchup(unique, home_q, away_q)
            if m:
                target_matchups.append(m)
                mid = m["id"]
                link = page.query_selector(f'a[href*="{mid}"]')
                if link:
                    href = link.get_attribute("href") or ""
                    target_hrefs[mid] = href

        for matchup in target_matchups:
            mid = matchup["id"]
            parts = matchup.get("participants", [])
            home_name = next((p["name"] for p in parts if p.get("alignment") == "home"), "")
            away_name = next((p["name"] for p in parts if p.get("alignment") == "away"), "")

            match_url = _build_match_url(matchup)
            print(f"\n  Opening: {match_url}")

            try:
                page.goto(match_url, wait_until="networkidle", timeout=45_000)
            except Exception as e:
                href = target_hrefs.get(mid, "")
                if href:
                    fallback = f"https://www.pinnacle.com{href.rstrip('/')}/#all"
                    print(f"    Trying fallback: {fallback}")
                    try:
                        page.goto(fallback, wait_until="networkidle", timeout=30_000)
                    except Exception as e2:
                        print(f"    Navigation failed: {e2}")
                        continue
                else:
                    print(f"    Navigation failed: {e}")
                    continue

            page.wait_for_timeout(2_000)
            _click_accordions_by_text(page, home_name, away_name)

        browser.close()

    RAW_DEBUG_FILE.write_text(json.dumps(all_markets, indent=2))
    RAW_RELATED_DEBUG.write_text(json.dumps(all_related, indent=2))

    type_summary: dict = {}
    for m in all_markets:
        t = m.get("type", "?")
        if not m.get("isAlternate", False):
            type_summary[t] = type_summary.get(t, 0) + 1
    print("\n  Main-line market types captured:")
    for t, count in sorted(type_summary.items()):
        print(f"    {t:<30} {count} entries")
    print(f"  Related matchup data for {len(all_related)} main matchup(s)")

    seen2: set = set()
    final: list[dict] = []
    for m in all_matchups:
        mid = m.get("id")
        if mid and mid not in seen2:
            seen2.add(mid)
            final.append(m)

    return final, all_markets, all_related


def _click_accordions_by_text(page, home: str, away: str):
    labels = [
        "Both Teams To Score? 1st Half",
        "Both Teams To Score?",
        "Both Teams To Score/Total Goals",
        "Both Teams To Score/Winner",
        "Double Chance 1st Half",
        "Double Chance",
        "Draw No Bet 1st Half",
        "Draw No Bet",
        "Half-Time/Full-Time",
        "Correct Score 1st Half",
        "Correct Score",
        "First Team To Score 1st Half",
        "First Team To Score",
        f"{home} To Score? 1st Half",
        f"{home} To Score?",
        f"{away} To Score? 1st Half",
        f"{away} To Score?",
    ]
    for label in labels:
        try:
            el = page.get_by_text(label, exact=True).first
            el.scroll_into_view_if_needed(timeout=2_000)
            el.click(timeout=2_000)
            page.wait_for_timeout(700)
        except Exception:
            pass

    for _ in range(3):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1_000)
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass


def _build_match_url(matchup: dict) -> str:
    parts = matchup.get("participants", [])
    home = next((p["name"] for p in parts if p.get("alignment") == "home"), "")
    away = next((p["name"] for p in parts if p.get("alignment") == "away"), "")
    league = matchup.get("league", {}).get("name", "")
    league_slug = slugify(league.replace(" - ", "-").replace(" – ", "-"))
    mid = matchup["id"]
    return (
        f"https://www.pinnacle.com/en/soccer/{league_slug}/"
        f"{slugify(home)}-vs-{slugify(away)}/{mid}/#all"
    )


# ---------------------------------------------------------------------------
# Match finding
# ---------------------------------------------------------------------------

def team_matches(query: str, name: str) -> bool:
    return query.lower().strip() in name.lower()


def find_matchup(matchups: list[dict], home_q: str, away_q: str) -> dict | None:
    for m in matchups:
        parts = m.get("participants", [])
        home = next((p["name"] for p in parts if p.get("alignment") == "home"), "")
        away = next((p["name"] for p in parts if p.get("alignment") == "away"), "")
        if team_matches(home_q, home) and team_matches(away_q, away):
            return m
        if team_matches(home_q, away) and team_matches(away_q, home):
            return m
    return None


# ---------------------------------------------------------------------------
# Standard market lookup helpers
# ---------------------------------------------------------------------------

def get_main(markets, matchup_id, mtype, period, side=None):
    """Main line only (isAlternate=False)."""
    return [
        m for m in markets
        if m.get("matchupId") == matchup_id
        and str(m.get("type", "")).lower() == mtype.lower()
        and m.get("period") == period
        and not m.get("isAlternate", False)
        and (side is None or str(m.get("side", "")).lower() == side.lower())
    ]


def _get_rows(markets, matchup_id, mtype, period, side=None):
    """All lines including alternates."""
    return [
        m for m in markets
        if m.get("matchupId") == matchup_id
        and str(m.get("type", "")).lower() == mtype.lower()
        and m.get("period") == period
        and (side is None or str(m.get("side", "")).lower() == side.lower())
    ]


def _ou_all_lines(rows: list) -> dict:
    """
    Collect Over/Under odds from all rows (main + alternate lines).
    Returns {label: decimal} pairs sorted by line value ascending,
    with Over before Under for each line.
    """
    by_line: dict = {}
    for row in rows:
        for p in row.get("prices", []):
            des   = p.get("designation", "")
            pts   = p.get("points")
            price = p.get("price")
            if price is None or pts is None or des not in ("over", "under"):
                continue
            if pts not in by_line:
                by_line[pts] = {}
            by_line[pts][des] = price

    result: dict = {}
    for pts in sorted(by_line.keys()):
        if "over"  in by_line[pts]:
            result[f"Over {pts}"]  = american_to_decimal(by_line[pts]["over"])
        if "under" in by_line[pts]:
            result[f"Under {pts}"] = american_to_decimal(by_line[pts]["under"])
    return result


def _spread_all_lines(rows: list, home: str, away: str) -> dict:
    """
    Collect Asian Handicap odds from all rows (main + alternate lines).
    Returns {label: decimal} pairs sorted by home-team handicap ascending.
    """
    by_line: dict = {}
    for row in rows:
        h_pts = h_price = a_pts = a_price = None
        for p in row.get("prices", []):
            des   = p.get("designation", "")
            pts   = p.get("points")
            price = p.get("price")
            if   des == "home": h_pts, h_price = pts, price
            elif des == "away": a_pts, a_price = pts, price
        if h_pts is not None:
            by_line[h_pts] = (h_price, a_pts, a_price)

    def _fmt(v):
        return f"+{v}" if v > 0 else str(v)

    result: dict = {}
    for h_pts in sorted(by_line.keys()):
        h_price, a_pts, a_price = by_line[h_pts]
        if h_price is not None:
            result[f"{home} ({_fmt(h_pts)})"] = american_to_decimal(h_price)
        if a_pts is not None and a_price is not None:
            result[f"{away} ({_fmt(a_pts)})"] = american_to_decimal(a_price)
    return result


def _decode_prices(prices: list[dict], pid_to_name: dict,
                   home: str, away: str) -> dict:
    """
    Decode a prices list into {label: decimal_odd}.
    Handles both designation-based (standard markets) and
    participantId-based (special/related markets) price objects.
    """
    result = {}
    for p in prices:
        american = p.get("price")
        if american is None:
            continue
        pid = p.get("participantId")
        if pid is not None:
            label = pid_to_name.get(pid, str(pid))
        else:
            des    = p.get("designation", "")
            points = p.get("points")
            if des == "home":   label = home
            elif des == "away": label = away
            elif des == "draw": label = "Draw"
            elif des == "over": label = f"Over {points}"
            elif des == "under":label = f"Under {points}"
            elif des == "yes":  label = "Yes"
            elif des == "no":   label = "No"
            else:               label = des
        result[label] = american_to_decimal(american)
    return result


# ---------------------------------------------------------------------------
# Parse one match — outputs ALL available markets as a flat dict
# ---------------------------------------------------------------------------

def parse_match(matchup: dict, markets: list[dict], related_by_id: dict) -> dict:
    parts = matchup.get("participants", [])
    home  = next((p["name"] for p in parts if p.get("alignment") == "home"), "Home")
    away  = next((p["name"] for p in parts if p.get("alignment") == "away"), "Away")
    mid   = matchup["id"]

    mkt_out: dict = {}   # market_label -> {selection: decimal_odd}

    # ── Standard markets from the main matchup — ALL lines ───────────────
    for period_num, suffix in [(0, ""), (1, " 1st Half")]:
        # Moneyline: only one line, no alternates
        ml = get_main(markets, mid, "moneyline", period_num)
        if ml:
            odds = _decode_prices(ml[0]["prices"], {}, home, away)
            if odds:
                mkt_out[f"Moneyline{suffix}"] = odds

        # Asian Handicap: all alternate lines, sorted by handicap
        sp_rows = _get_rows(markets, mid, "spread", period_num)
        if sp_rows:
            odds = _spread_all_lines(sp_rows, home, away)
            if odds:
                mkt_out[f"Asian Handicap{suffix}"] = odds

        # Total Goals: all alternate lines, sorted by line value
        tot_rows = _get_rows(markets, mid, "total", period_num)
        if tot_rows:
            odds = _ou_all_lines(tot_rows)
            if odds:
                mkt_out[f"Total Goals{suffix}"] = odds

        # Team Total: all alternate lines per team
        for side, team in [("home", home), ("away", away)]:
            tt_rows = _get_rows(markets, mid, "team_total", period_num, side=side)
            if tt_rows:
                odds = _ou_all_lines(tt_rows)
                if odds:
                    mkt_out[f"Team Total {team}{suffix}"] = odds

    # ── Related / special markets ─────────────────────────────────────────
    related_list = related_by_id.get(mid, [])
    for rel in related_list:
        rel_id = rel.get("id")
        if rel_id is None or rel_id == mid:
            continue

        participants = rel.get("participants", [])
        pid_to_name  = {p["id"]: p["name"] for p in participants if "id" in p}
        desc = rel.get("special", {}).get("description", "")

        if desc:
            # Named special market — use description as the label
            rel_mkt = next(
                (m for m in markets
                 if m.get("matchupId") == rel_id
                 and not m.get("isAlternate", False)),
                None,
            )
            if rel_mkt:
                odds = _decode_prices(rel_mkt["prices"], pid_to_name, home, away)
                if odds:
                    mkt_out[desc] = odds
        else:
            # Sub-match (Corners, Bookings) — extract its standard market types
            league_name = rel.get("league", {}).get("name", "")
            if "corner" in league_name.lower():
                tag = "Corners"
            elif "booking" in league_name.lower():
                tag = "Bookings"
            else:
                continue

            for period_num, suffix in [(0, ""), (1, " 1st Half")]:
                for mtype, label in [
                    ("moneyline",  f"Moneyline {tag}{suffix}"),
                    ("spread",     f"Handicap {tag}{suffix}"),
                    ("total",      f"Total {tag}{suffix}"),
                ]:
                    rows = [m for m in markets
                            if m.get("matchupId") == rel_id
                            and m.get("type") == mtype
                            and m.get("period") == period_num
                            and not m.get("isAlternate", False)]
                    if rows:
                        odds = _decode_prices(rows[0]["prices"], pid_to_name, home, away)
                        if odds:
                            mkt_out[label] = odds

                for side, team in [("home", home), ("away", away)]:
                    rows = [m for m in markets
                            if m.get("matchupId") == rel_id
                            and m.get("type") == "team_total"
                            and m.get("period") == period_num
                            and str(m.get("side", "")).lower() == side
                            and not m.get("isAlternate", False)]
                    if rows:
                        odds = _decode_prices(rows[0]["prices"], pid_to_name, home, away)
                        if odds:
                            mkt_out[f"Team Total {team} {tag}{suffix}"] = odds

    print(f"    {len(mkt_out)} markets captured")
    return {
        "match":    f"{home} vs {away}",
        "league":   matchup.get("league", {}).get("name", ""),
        "starts_at":matchup.get("startTime", ""),
        "markets":  mkt_out,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_result(r: dict):
    print(f"\n{'='*68}")
    print(f"  {r['match']}")
    print(f"  {r['league']}  |  Starts: {r['starts_at']}")
    print(f"{'='*68}")
    for market, odds in r["markets"].items():
        print(f"\n  {market}:")
        for sel, dec in odds.items():
            print(f"    {sel:<48} {dec:.4f}")


def save_output(results: list[dict]):
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"pinnacle_odds_{ts}.json"
    path.write_text(json.dumps(results, indent=2))
    print(f"\nSaved -> {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    matchups, markets, related = scrape_matches(MATCHES_TO_TRACK)
    if not matchups:
        print("\nNo matchups captured.")
        return

    found = []
    print()
    for home_q, away_q in MATCHES_TO_TRACK:
        m = find_matchup(matchups, home_q, away_q)
        if m:
            found.append(m)
            parts = m.get("participants", [])
            h = next((p["name"] for p in parts if p.get("alignment") == "home"), home_q)
            a = next((p["name"] for p in parts if p.get("alignment") == "away"), away_q)
            print(f"  [OK] Found: {h} vs {a}")
        else:
            print(f"  [--] Not listed: {home_q} vs {away_q}")

    if not found:
        print("\nNone of the requested matches are on Pinnacle right now.")
        return

    results = [parse_match(m, markets, related) for m in found]
    for r in results:
        print_result(r)
    save_output(results)
    print(f"\nDebug: {RAW_DEBUG_FILE}  |  {RAW_RELATED_DEBUG}")


if __name__ == "__main__":
    main()
