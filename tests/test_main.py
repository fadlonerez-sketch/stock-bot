"""Tests for the stock bot. Alpha Vantage, Anthropic and SMTP are all mocked: no network access."""
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import requests

import main as bot

ENV = {
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "ALPHA_VANTAGE_API_KEY": "test-av-key",
    "EMAIL_SENDER": "sender@example.com",
    "EMAIL_PASSWORD": "app-password",
    "EMAIL_RECEIVER": "receiver@example.com",
}


def quote(price="123.45"):
    return {"Global Quote": {
        "01. symbol": "X", "05. price": price, "06. volume": "1000",
        "09. change": "1.20", "10. change percent": "0.98%",
    }}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Error for url: https://www.alphavantage.co/query?apikey=test-av-key",
                response=self)

    def json(self):
        return self.payload


class FakeAnthropic:
    """Stands in for anthropic.Anthropic; records calls and returns a canned message or raises."""
    calls = []
    error = None
    text = "AAPL looks fine.\nTOP PICK TODAY: AAPL - steady."
    stop_reason = "end_turn"

    def __init__(self, api_key=None):
        self.messages = self

    def create(self, **kwargs):
        FakeAnthropic.calls.append(kwargs)
        if FakeAnthropic.error:
            raise FakeAnthropic.error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=FakeAnthropic.text)],
                               stop_reason=FakeAnthropic.stop_reason)


class FakeSMTP:
    sent = []
    error = None

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if FakeSMTP.error:
            raise FakeSMTP.error

    def sendmail(self, sender, receivers, message):
        FakeSMTP.sent.append((sender, receivers, message))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No real .env, network, Claude or SMTP in any test."""
    for name in list(ENV) + ["STOCK_TICKERS", "CLAUDE_MODEL", "CLAUDE_MAX_TOKENS"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bot, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(bot.anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(bot.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(bot.requests, "get", lambda *a, **k: pytest.fail("unexpected network call"))
    FakeAnthropic.calls, FakeAnthropic.error, FakeAnthropic.stop_reason = [], None, "end_turn"
    FakeSMTP.sent, FakeSMTP.error = [], None


def set_env(monkeypatch, **extra):
    for k, v in {**ENV, **extra}.items():
        monkeypatch.setenv(k, v)


def serve(monkeypatch, responses):
    """Make requests.get answer per symbol from a {symbol: FakeResponse | Exception} map."""
    def fake_get(url, params=None, timeout=None):
        r = responses[params["symbol"]]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(bot.requests, "get", fake_get)


def sent_body():
    assert len(FakeSMTP.sent) == 1
    return FakeSMTP.sent[0][2]


# ----------------------------------------------------------------------------- quote parsing
def test_parse_quote_valid():
    q = bot.parse_quote("AAPL", quote("189.50"))
    assert q == {"symbol": "AAPL", "price": "189.50", "change": "1.20", "change_percent": "0.98%", "volume": "1000"}


@pytest.mark.parametrize("payload, expected", [
    ({"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is ..."}, "rate limit"),
    ({"Information": "You have reached the API rate limit ..."}, "rate limit"),
    ({"Error Message": "Invalid API call."}, "Alpha Vantage error: Invalid API call."),
    ({"Global Quote": {}}, "no quote returned"),
    ({}, "no quote returned"),
    (quote(price=""), "no valid price"),
    ([], "unexpected response format"),
])
def test_parse_quote_rejects_unusable_responses(payload, expected):
    with pytest.raises(bot.QuoteError, match=expected):
        bot.parse_quote("AAPL", payload)


def test_http_error_does_not_leak_api_key(monkeypatch):
    serve(monkeypatch, {"AAPL": FakeResponse({}, status=500)})
    with pytest.raises(bot.QuoteError) as exc:
        bot.get_stock_data("AAPL", "test-av-key")
    assert "500" in str(exc.value) and "test-av-key" not in str(exc.value)


def test_network_error_is_reported(monkeypatch):
    serve(monkeypatch, {"AAPL": requests.ConnectionError("boom apikey=test-av-key")})
    with pytest.raises(bot.QuoteError, match="network error") as exc:
        bot.get_stock_data("AAPL", "test-av-key")
    assert "test-av-key" not in str(exc.value)


# ----------------------------------------------------------------------------- configuration
def test_missing_env_vars_are_listed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    with pytest.raises(bot.ConfigError) as exc:
        bot.load_config()
    for name in ["ALPHA_VANTAGE_API_KEY", "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER"]:
        assert name in str(exc.value)
    assert "ANTHROPIC_API_KEY" not in str(exc.value)


def test_main_exits_non_zero_without_config(caplog):
    assert bot.main() == 1
    assert "Missing required environment variable" in caplog.text


def test_defaults(monkeypatch):
    set_env(monkeypatch)
    cfg = bot.load_config()
    assert cfg.tickers == bot.DEFAULT_TICKERS
    assert cfg.model == "claude-sonnet-4-5"
    assert cfg.max_tokens == 1000


def test_overrides(monkeypatch):
    set_env(monkeypatch, STOCK_TICKERS=" msft, goog ,MSFT,", CLAUDE_MODEL="my-model", CLAUDE_MAX_TOKENS="2000")
    cfg = bot.load_config()
    assert cfg.tickers == ("MSFT", "GOOG")
    assert cfg.model == "my-model"
    assert cfg.max_tokens == 2000


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_invalid_max_tokens(monkeypatch, value):
    set_env(monkeypatch, CLAUDE_MAX_TOKENS=value)
    with pytest.raises(bot.ConfigError, match="CLAUDE_MAX_TOKENS"):
        bot.load_config()


# ----------------------------------------------------------------------------- full run
def test_partial_failure_excludes_failed_stocks(monkeypatch):
    set_env(monkeypatch, STOCK_TICKERS="AAPL,NVDA,MSFT")
    serve(monkeypatch, {
        "AAPL": FakeResponse(quote("189.50")),
        "NVDA": FakeResponse({"Note": "rate limited"}),
        "MSFT": FakeResponse({"Error Message": "Invalid API call."}),
    })
    assert bot.main() == 0

    prompt = FakeAnthropic.calls[0]["messages"][0]["content"]
    assert "AAPL" in prompt and "NVDA" not in prompt and "MSFT" not in prompt
    body = sent_body()
    assert "Data unavailable" in body
    assert "NVDA: Alpha Vantage rate limit" in body
    assert "MSFT: Alpha Vantage error" in body


def test_no_data_does_not_call_claude(monkeypatch, caplog):
    set_env(monkeypatch, STOCK_TICKERS="AAPL,NVDA")
    serve(monkeypatch, {"AAPL": FakeResponse({"Information": "limit"}), "NVDA": FakeResponse({})})
    assert bot.main() == 1
    assert FakeAnthropic.calls == []
    assert FakeSMTP.sent == []
    assert "Could not fetch data for any stock" in caplog.text


def test_claude_error_is_reported_and_no_email_sent(monkeypatch, caplog):
    set_env(monkeypatch, STOCK_TICKERS="AAPL")
    serve(monkeypatch, {"AAPL": FakeResponse(quote())})
    FakeAnthropic.error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    assert bot.main() == 1
    assert FakeSMTP.sent == []
    assert "Claude API call failed" in caplog.text


def test_truncated_answer_is_marked(monkeypatch):
    set_env(monkeypatch, STOCK_TICKERS="AAPL")
    serve(monkeypatch, {"AAPL": FakeResponse(quote())})
    FakeAnthropic.stop_reason = "max_tokens"
    assert bot.main() == 0
    assert "Analysis truncated" in sent_body()


def test_email_contains_analysis_and_disclaimer(monkeypatch):
    set_env(monkeypatch, STOCK_TICKERS="AAPL", CLAUDE_MODEL="my-model", CLAUDE_MAX_TOKENS="1500")
    serve(monkeypatch, {"AAPL": FakeResponse(quote())})
    assert bot.main() == 0
    assert FakeAnthropic.calls[0]["model"] == "my-model"
    assert FakeAnthropic.calls[0]["max_tokens"] == 1500
    sender, receivers, message = FakeSMTP.sent[0]
    assert sender == "sender@example.com" and receivers == ["receiver@example.com"]
    assert "TOP PICK TODAY" in message
    assert "Not financial advice" in message
    assert "Data unavailable" not in message


def test_smtp_failure_exits_non_zero(monkeypatch, caplog):
    set_env(monkeypatch, STOCK_TICKERS="AAPL")
    serve(monkeypatch, {"AAPL": FakeResponse(quote())})
    FakeSMTP.error = bot.smtplib.SMTPAuthenticationError(535, b"bad credentials")
    assert bot.main() == 1
    assert "Sending the email failed" in caplog.text
