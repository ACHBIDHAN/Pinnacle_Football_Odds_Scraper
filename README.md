# Pinnacle Football Odds Scraper

Scrapes **all available betting markets** from Pinnacle's football/soccer match pages and saves them as clean, structured JSON — no API key required.

Captures every market visible in the "All" tab: moneyline, Asian handicap (all alternate lines), totals (all lines), team totals, BTTS, double chance, correct score, HT/FT, winning margin, corners, bookings, player props, and more.

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

---

## Setup

```bash
# Install dependencies
uv pip install -r requirements.txt

# Install the Chromium browser used for scraping
uv run playwright install chromium
```

---

## Usage

1. Open `pinnacle_scraper.py` and edit the `MATCHES_TO_TRACK` list at the top:

```python
MATCHES_TO_TRACK = [
    ("Barcelona", "Getafe"),
    ("Liverpool", "Crystal Palace"),
    # ("Arsenal", "Chelsea"),
]
```

Add one tuple per match: `("Home Team", "Away Team")`. Partial names work — `"Barcelona"` will match `"FC Barcelona"`.

2. Run the scraper:

```bash
uv run --with playwright python pinnacle_scraper.py
```

3. Results are printed to the terminal and saved to `odds_output/pinnacle_odds_YYYYMMDD_HHMMSS.json`.

---

## Output format

```json
[
  {
    "match": "Getafe vs Barcelona",
    "league": "Spain - La Liga",
    "starts_at": "2026-04-25T14:15:00Z",
    "markets": {
      "Moneyline": { "Getafe": 5.44, "Barcelona": 1.6494, "Draw": 4.06 },
      "Asian Handicap": {
        "Getafe (-0.25)": 4.77,
        "Barcelona (+0.25)": 1.21,
        "Getafe (+0.5)": 2.36,
        "Barcelona (-0.5)": 1.65
      },
      "Total Goals": {
        "Over 2.0": 1.44, "Under 2.0": 2.93,
        "Over 2.5": 1.93, "Under 2.5": 1.96,
        "Over 3.0": 2.58, "Under 3.0": 1.54
      },
      "Both Teams To Score?": { "Yes": 1.93, "No": 1.92 },
      "Half-Time/Full-Time": {
        "Getafe - Getafe": 10.31,
        "Barcelona - Barcelona": 2.51
      },
      "Correct Score": { "Getafe 0, Barcelona 1": 7.69 },
      "Mohamed Salah To Score": { "Yes": 2.62, "No": 1.50 }
    }
  }
]
```

All odds are **decimal format**. Every market uses real team names — no "home" / "away" labels.

---

## How it works

1. Loads Pinnacle's soccer matchups page to resolve each requested match to its URL and internal ID.
2. Navigates to each match detail page with a headless Chromium browser (via Playwright).
3. Intercepts all API responses from `arcadia.pinnacle.com` — both the standard market data and the `/matchups/{id}/related` endpoint which encodes special markets (BTTS, double chance, HT/FT, etc.) as related sub-matchups with participant-ID-based prices.
4. Decodes and structures all captured data into the output JSON.

Two debug files are written on each run (`pinnacle_raw_markets.json`, `pinnacle_raw_related.json`) for inspection if needed — they are excluded from version control by `.gitignore`.
