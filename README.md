# Card Arbitrage Bot

Python project for finding graded trading-card listings on eBay, extracting certification information from listing images, comparing Card Ladder valuations, and sending potential deals to Discord.

## Why I Started This

A friend of mine who buys and sells sports cards spends hours scrolling through eBay, comparing prices, and looking for undervalued cards. I became interested in his process and, after learning more about his workflow, realized I could automate much of it.

The project started as a rough Python script and evolved through numerous versions as I added new features, improved the automation, and worked through technical challenges. The system has now tracked over 100,000 eBay listings, using AI-powered image recognition and pricing data to identify potential arbitrage opportunities.

My favorite part of this project has been taking a real world problem and building something that can save hours of repetitive work by using cool tools like Python, APIs, and AI powered automation.

## Files

- `run_scanner.py`: Selenium Chrome login and session monitoring; entry point for the current scanner workflow.
- `card_scanner.py`: eBay listing retrieval, image analysis, valuation lookup, profitability filtering, Discord alerts, and duplicate tracking.
- `profit_tracker.py`: separate Discord bot that records purchases in Google Sheets when a user replies to an alert with `!bought <price>`.

The scanner creates `seen_items.txt` locally to remember listing IDs. That file and personal purchase records are excluded from this repository. Existing cache data can be copied into the project folder locally.

## Setup

Use Python and an installed Chrome browser. Run commands from this project folder:

```bash
python -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in your local `.env` with your own credentials. The scanner uses the eBay, Card Ladder, OpenAI, and Discord webhook settings. The optional tracker uses the Discord bot token, Google Sheet name, and local Google service-account JSON file. Share the sheet with that service account and enable the bot's Message Content intent in Discord.

```bash
# Test Selenium login without scanning
python run_scanner.py --login-test

# Start the scanner
python run_scanner.py

# Optional: run in another terminal
python profit_tracker.py
```

Keep the Chrome window open during scanning. `--manual-login` is available when needed. Verification may still require interaction or fail; this project does not solve verification challenges.

The original scanner also contains a Playwright login path. Use `run_scanner.py` for the Selenium workflow above; its Playwright import remains a dependency of the original scanner module.

## Configuration and limitations

Profit thresholds and the resume offset are near the top of `card_scanner.py`: `MIN_PROFIT_DOLLARS`, `MIN_PROFIT_MARGIN`, and `MANUAL_OFFSET_JUMP`. The uploaded version retains its existing offset of 54600. Review it before running a fresh scan; the eBay Browse result window is limited and this large offset can fail. Query partitioning is not implemented here.

Valuation-based profit estimates are screening estimates, not guaranteed resale proceeds. The project sends alerts; it does not purchase cards. Purchase tracking is user-triggered, and sold prices are entered separately.

This repository preserves the existing application logic with credentials moved to environment variables. It is not a completed security audit. The tracker should remain in a trusted private Discord channel: it does not currently restrict commands to an allowlist or independently verify the source of referenced embeds. It writes Sheets values using USER_ENTERED, so untrusted text can be interpreted as formulas. The cache records IDs before processing, so its count is not proof of completed analyses.

## Credentials

Never commit `.env`, Google service-account credentials, browser cookies, tokens, or private records. `.env.example` contains placeholders only. Rotate any credentials that have been shared or exposed; removing them from source does not invalidate them.

The packaging changes were syntax-checked. Live login, scanning, and Google Sheets writes were not tested as part of publication preparation.
