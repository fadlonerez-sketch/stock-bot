import os
import anthropic
import requests
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

TOP_STOCKS = ["AAPL", "NVDA", "MSFT", "TSLA", "AMZN"]


def get_stock_data(symbol):
    try:
        response = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": ALPHA_VANTAGE_API_KEY},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        print(f"Network error for {symbol}: {e}")
        return None

    quote = data.get("Global Quote", {})
    if not quote:
        print(f"No data returned for {symbol}")
        return None

    return {
        "symbol": symbol,
        "price": quote.get("05. price", "N/A"),
        "change": quote.get("09. change", "N/A"),
        "change_percent": quote.get("10. change percent", "N/A"),
        "volume": quote.get("06. volume", "N/A"),
    }


def analyze_stocks(stocks_data):
    stocks_text = "\n".join(
        f"Symbol: {s['symbol']}\nPrice: ${s['price']}\n"
        f"Change: {s['change']} ({s['change_percent']})\nVolume: {s['volume']}\n"
        for s in stocks_data
    )

    prompt = (
        "You are a stock market analyst. For each stock below:\n"
        "1) What happened today (2 sentences max)\n"
        "2) 30-day price target with percentage upside/downside\n"
        "3) Rating: 1-5 (1=Strong Sell, 5=Strong Buy)\n"
        "4) One key risk to watch\n\n"
        "End with: TOP PICK TODAY: [symbol] - one sentence why.\n"
        "Keep each analysis under 100 words. Be direct and specific.\n\n"
        f"Stocks:\n{stocks_text}"
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


def send_email(analysis):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = "Daily Stock Analysis"
    msg.attach(MIMEText(analysis, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls(context=ssl.create_default_context())  # verify Gmail's certificate
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        print("Email sent successfully!")
    except smtplib.SMTPException as e:
        print(f"Email error: {e}")


if __name__ == "__main__":
    print("Fetching stock data...")
    stocks_data = [d for symbol in TOP_STOCKS if (d := get_stock_data(symbol))]

    if not stocks_data:
        print("Error: could not fetch any stock data.")
    else:
        print(f"Analyzing {len(stocks_data)} stocks with Claude...")
        analysis = analyze_stocks(stocks_data)
        print("\nDaily Stock Analysis:\n")
        print(analysis)
        print("\nSending email...")
        send_email(analysis)
