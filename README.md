# Stock Bot

A small Python script that emails you a daily AI-written summary of a few stocks: it pulls the latest quotes from Alpha Vantage, asks Claude for a short analysis, and sends the result through Gmail.

> **Informational only - not financial advice.** The analysis is generated automatically by an AI model from delayed market data. Every email ends with this disclaimer.

## What it does

Instead of checking several tickers by hand each morning, run the bot on a schedule and get one email with:

- for each stock: what happened today, a 30-day price view, a 1-5 rating and one key risk
- a "top pick" line
- a list of any stocks whose data could not be fetched (they are never sent to Claude as if they were valid)

## Features

- Alpha Vantage `GLOBAL_QUOTE` lookups with clear handling of rate-limit notices (`Note` / `Information`), API errors (`Error Message`), empty quotes and network failures
- Partial failures are tolerated: failed tickers are excluded from the analysis and listed in the email; if no ticker has data, Claude is not called and the script exits with an error
- Configuration validated at startup, with a clear message listing any missing variables
- API keys are never written to logs or emails (HTTP errors are reported by status code only)
- Non-zero exit code on any failure, so schedulers can detect it
- Tickers, Claude model and token limit configurable through environment variables

## Tech stack

Python 3, [`anthropic`](https://pypi.org/project/anthropic/) SDK, `requests`, `python-dotenv`, standard-library `smtplib`; tests with `pytest`.

## Requirements

- Python 3.10+ (developed on 3.14)
- An [Anthropic API key](https://console.anthropic.com/)
- A free [Alpha Vantage API key](https://www.alphavantage.co/support/#api-key)
- A Gmail account with 2-Step Verification and an **app password** (Google account > Security > App passwords). Your normal Gmail password will not work over SMTP.

## Install

```bash
git clone https://github.com/fadlonerez-sketch/stock-bot.git
cd stock-bot
python -m venv .venv
.venv\Scripts\activate          # Windows;  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in the values
```

## Configuration

Set these in `.env` (git-ignored) or in the environment:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Anthropic API key |
| `ALPHA_VANTAGE_API_KEY` | yes | Alpha Vantage API key |
| `EMAIL_SENDER` | yes | Gmail address that sends the email |
| `EMAIL_PASSWORD` | yes | Gmail app password for that address |
| `EMAIL_RECEIVER` | yes | Address that receives the summary |
| `STOCK_TICKERS` | no | Comma-separated symbols, default `AAPL,NVDA,MSFT,TSLA,AMZN` |
| `CLAUDE_MODEL` | no | Claude model id, default `claude-sonnet-4-5` |
| `CLAUDE_MAX_TOKENS` | no | Maximum length of Claude's answer, default `1000` |

## Run

```bash
python main.py
```

Progress is logged to the console. Exit code `0` means the email was sent; `1` means something failed (the log says what).

### Run it daily

- **Windows (Task Scheduler):** Create Basic Task > Daily > Action "Start a program". Program: the full path to `.venv\Scripts\python.exe`; arguments: `main.py`; "Start in": the project folder (so `.env` is found).
- **macOS / Linux (cron):** `crontab -e`, then for example (weekdays at 17:30):
  ```
  30 17 * * 1-5 cd /path/to/stock-bot && .venv/bin/python main.py >> bot.log 2>&1
  ```

## Run tests

All external services (Alpha Vantage, Anthropic, Gmail SMTP) are mocked, so the tests need no keys and make no network calls.

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Latest local run: **23 passed**. The tests cover quote parsing, rate-limit and error detection, missing or invalid configuration, excluding failed stocks, Claude API errors, truncated answers, SMTP failures and the disclaimer. They also run in GitHub Actions ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)).

## Project structure

```
main.py               the bot: config, Alpha Vantage, Claude, email, main()
tests/test_main.py    pytest suite (fully mocked)
.env.example          template for the required settings
requirements.txt      runtime dependencies (pinned)
requirements-dev.txt  + pytest
```

## Known limitations

- Alpha Vantage's free tier is heavily rate-limited (a small number of requests per minute and per day). The bot makes one request per ticker, so keep the list short and run it at most a few times a day; rate-limited tickers are reported as unavailable in the email.
- `GLOBAL_QUOTE` returns the latest end-of-day / delayed quote, not real-time prices.
- The analysis is based only on the price, change and volume of one day; Claude has no news or fundamentals, so treat its targets and ratings as a conversation starter, not a recommendation.
- Sending is hard-wired to Gmail's SMTP server (`smtp.gmail.com:587`).

## License

[MIT](LICENSE) (c) 2026 fadlonerez-sketch
