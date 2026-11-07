"""Daily stock summary bot.

Fetches the latest quote for a list of tickers from Alpha Vantage, asks Claude for a short
analysis of the stocks that returned valid data, and emails the result through Gmail SMTP.
Stocks whose data could not be fetched are left out of the analysis and listed in the email.
"""
import logging
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import requests
from dotenv import load_dotenv

log = logging.getLogger("stock_bot")

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

REQUIRED_ENV = (
    "ANTHROPIC_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "EMAIL_SENDER",
    "EMAIL_PASSWORD",
    "EMAIL_RECEIVER",
)
DEFAULT_TICKERS = ("AAPL", "NVDA", "MSFT", "TSLA", "AMZN")
DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 1000

DISCLAIMER = (
    "Not financial advice: this summary is generated automatically by an AI model from "
    "delayed market data, for informational purposes only. Do your own research."
)


class BotError(Exception):
    """An error that should stop the bot with a clear message and a non-zero exit code."""


class ConfigError(BotError):
    pass


class QuoteError(Exception):
    """Alpha Vantage did not return usable data for one symbol."""


class AnalysisError(BotError):
    pass


class EmailError(BotError):
    pass


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    alpha_vantage_api_key: str
    email_sender: str
    email_password: str
    email_receiver: str
    tickers: tuple = DEFAULT_TICKERS
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS


def load_config(env=None):
    """Build the configuration from environment variables; raise ConfigError if anything is wrong."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    tickers_raw = env.get("STOCK_TICKERS", "").strip()
    if tickers_raw:
        tickers = tuple(dict.fromkeys(t.strip().upper() for t in tickers_raw.split(",") if t.strip()))
        if not tickers:
            raise ConfigError("STOCK_TICKERS is set but contains no ticker symbols.")
    else:
        tickers = DEFAULT_TICKERS

    max_tokens_raw = env.get("CLAUDE_MAX_TOKENS", "").strip()
    try:
        max_tokens = int(max_tokens_raw) if max_tokens_raw else DEFAULT_MAX_TOKENS
    except ValueError:
        raise ConfigError(f"CLAUDE_MAX_TOKENS must be a whole number, got {max_tokens_raw!r}.") from None
    if max_tokens <= 0:
        raise ConfigError("CLAUDE_MAX_TOKENS must be greater than 0.")

    return Config(
        anthropic_api_key=env["ANTHROPIC_API_KEY"].strip(),
        alpha_vantage_api_key=env["ALPHA_VANTAGE_API_KEY"].strip(),
        email_sender=env["EMAIL_SENDER"].strip(),
        email_password=env["EMAIL_PASSWORD"].strip(),
        email_receiver=env["EMAIL_RECEIVER"].strip(),
        tickers=tickers,
        model=env.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL,
        max_tokens=max_tokens,
    )


# ----------------------------------------------------------------------------- market data
def parse_quote(symbol, data):
    """Turn an Alpha Vantage GLOBAL_QUOTE response into a dict, or raise QuoteError explaining why not."""
    if not isinstance(data, dict):
        raise QuoteError("unexpected response format")
    # Alpha Vantage answers HTTP 200 with one of these keys instead of data when a request is
    # rate-limited ("Note" / "Information") or invalid ("Error Message").
    if "Note" in data or "Information" in data:
        raise QuoteError("Alpha Vantage rate limit or API notice: " + str(data.get("Note") or data.get("Information")))
    if "Error Message" in data:
        raise QuoteError("Alpha Vantage error: " + str(data["Error Message"]))

    quote = data.get("Global Quote")
    if not quote:
        raise QuoteError("no quote returned (unknown symbol?)")
    price = quote.get("05. price")
    try:
        float(price)
    except (TypeError, ValueError):
        raise QuoteError("quote has no valid price") from None

    return {
        "symbol": symbol,
        "price": price,
        "change": quote.get("09. change", "N/A"),
        "change_percent": quote.get("10. change percent", "N/A"),
        "volume": quote.get("06. volume", "N/A"),
    }


def get_stock_data(symbol, api_key):
    """Fetch one quote. Raises QuoteError on network problems or unusable data."""
    try:
        response = requests.get(
            ALPHA_VANTAGE_URL,
            params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": api_key},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except requests.HTTPError as e:
        # Only the status code: the exception text contains the request URL, including the API key.
        status = e.response.status_code if e.response is not None else "unknown"
        raise QuoteError(f"HTTP error {status}") from None
    except requests.RequestException as e:
        raise QuoteError(f"network error ({type(e).__name__})") from None
    except ValueError:
        raise QuoteError("response was not valid JSON") from None
    return parse_quote(symbol, data)


def fetch_all(tickers, api_key):
    """Return (valid quotes, {symbol: reason} for every symbol without usable data)."""
    stocks, unavailable = [], {}
    for symbol in tickers:
        try:
            stocks.append(get_stock_data(symbol, api_key))
            log.info("Fetched %s", symbol)
        except QuoteError as e:
            unavailable[symbol] = str(e)
            log.warning("No data for %s: %s", symbol, e)
    return stocks, unavailable


# ----------------------------------------------------------------------------- analysis
def build_prompt(stocks):
    stocks_text = "\n".join(
        f"Symbol: {s['symbol']}\nPrice: ${s['price']}\n"
        f"Change: {s['change']} ({s['change_percent']})\nVolume: {s['volume']}\n"
        for s in stocks
    )
    return (
        "You are a stock market analyst. For each stock below:\n"
        "1) What happened today (2 sentences max)\n"
        "2) 30-day price target with percentage upside/downside\n"
        "3) Rating: 1-5 (1=Strong Sell, 5=Strong Buy)\n"
        "4) One key risk to watch\n\n"
        "End with: TOP PICK TODAY: [symbol] - one sentence why.\n"
        "Keep each analysis under 100 words. Be direct and specific.\n\n"
        f"Stocks:\n{stocks_text}"
    )


def analyze_stocks(stocks, config, client=None):
    """Ask Claude for the analysis. Raises AnalysisError if the API call fails or returns no text."""
    client = client or anthropic.Anthropic(api_key=config.anthropic_api_key)
    try:
        message = client.messages.create(
            model=config.model,
            max_tokens=config.max_tokens,
            messages=[{"role": "user", "content": build_prompt(stocks)}],
        )
    except anthropic.APIError as e:
        raise AnalysisError(f"Claude API call failed: {e}") from e

    text = "".join(getattr(block, "text", "") for block in message.content).strip()
    if not text:
        raise AnalysisError("Claude returned an empty response.")
    if getattr(message, "stop_reason", None) == "max_tokens":
        log.warning("Claude's answer was cut off at CLAUDE_MAX_TOKENS=%d; consider raising it.", config.max_tokens)
        text += "\n\n[Analysis truncated: the response reached the token limit.]"
    return text


# ----------------------------------------------------------------------------- email
def build_email_body(analysis, unavailable):
    parts = [analysis.strip()]
    if unavailable:
        lines = "\n".join(f"- {symbol}: {reason}" for symbol, reason in unavailable.items())
        parts.append("Data unavailable (not included in the analysis):\n" + lines)
    parts.append("---\n" + DISCLAIMER)
    return "\n\n".join(parts) + "\n"


def send_email(config, subject, body):
    msg = MIMEMultipart()
    msg["From"] = config.email_sender
    msg["To"] = config.email_receiver
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())  # verify Gmail's certificate
            server.login(config.email_sender, config.email_password)
            server.sendmail(config.email_sender, [config.email_receiver], msg.as_string())
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError(f"Sending the email failed: {e}") from e


# ----------------------------------------------------------------------------- main
def run(config):
    log.info("Fetching quotes for %s", ", ".join(config.tickers))
    stocks, unavailable = fetch_all(config.tickers, config.alpha_vantage_api_key)
    if not stocks:
        raise BotError("Could not fetch data for any stock; not calling Claude or sending email.")

    log.info("Analyzing %d stock(s) with %s", len(stocks), config.model)
    analysis = analyze_stocks(stocks, config)
    body = build_email_body(analysis, unavailable)

    log.info("Sending email")
    send_email(config, "Daily Stock Analysis", body)
    log.info("Email sent")
    return body


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    try:
        run(load_config())
    except BotError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
