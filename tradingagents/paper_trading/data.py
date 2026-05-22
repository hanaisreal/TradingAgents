from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yfinance as yf


_UA = "tradingagents-paper/0.1 (+https://github.com/hanaisreal/TradingAgents)"
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class NewsItem:
    source: str
    title: str
    url: str = ""
    published_at: str = ""
    summary: str = ""
    sentiment_hint: int = 0


@dataclass
class MarketSnapshot:
    ticker: str
    price: float
    previous_close: float | None
    change_pct: float | None
    volume: float | None
    timestamp: str
    quote_source: str
    news: list[NewsItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["news"] = [asdict(item) for item in self.news]
        return payload


def _clean(text: str) -> str:
    return unescape(_TAG_RE.sub("", text or "")).replace("\n", " ").strip()


def _request_json(url: str, headers: dict[str, str] | None = None, timeout: float = 4.0) -> dict[str, Any]:
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json", **(headers or {})})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _sentiment_hint(text: str) -> int:
    lower = text.lower()
    positive = ("beat", "surge", "rally", "upgrade", "record", "호재", "상승", "급등", "최대", "돌파")
    negative = ("miss", "plunge", "downgrade", "lawsuit", "probe", "악재", "하락", "급락", "부진", "리콜")
    return sum(word in lower for word in positive) - sum(word in lower for word in negative)


def fetch_quote_yfinance(ticker: str, timeout: float = 4.0) -> tuple[float, float | None, float | None, float | None, str, list[str]]:
    notes: list[str] = []
    ticker_obj = yf.Ticker(ticker)
    price = previous_close = volume = None
    try:
        fast = ticker_obj.fast_info
        price = getattr(fast, "last_price", None) or fast.get("last_price")
        previous_close = getattr(fast, "previous_close", None) or fast.get("previous_close")
        volume = getattr(fast, "last_volume", None) or fast.get("last_volume")
    except Exception as exc:  # pragma: no cover - yfinance shape varies
        notes.append(f"yfinance fast_info unavailable: {type(exc).__name__}")

    if price is None:
        hist = ticker_obj.history(period="2d", interval="1m", timeout=timeout)
        if not hist.empty:
            price = float(hist["Close"].dropna().iloc[-1])
            volume = float(hist["Volume"].dropna().iloc[-1]) if "Volume" in hist else None
            if len(hist["Close"].dropna()) > 1:
                previous_close = float(hist["Close"].dropna().iloc[0])

    if price is None:
        raise RuntimeError(f"No quote returned for {ticker}")

    price_f = float(price)
    prev_f = float(previous_close) if previous_close else None
    change_pct = ((price_f - prev_f) / prev_f * 100.0) if prev_f else None
    return price_f, prev_f, change_pct, float(volume) if volume else None, "yfinance", notes


def fetch_naver_news(query: str, limit: int = 8, timeout: float = 4.0) -> tuple[list[NewsItem], str | None]:
    client_id = os.getenv("NAVER_NEWS_CLIENT_ID") or os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_NEWS_CLIENT_SECRET") or os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return [], "Naver Search API credentials missing"
    qs = urlencode({"query": query, "display": min(limit, 20), "sort": "date"})
    try:
        payload = _request_json(
            f"https://openapi.naver.com/v1/search/news.json?{qs}",
            headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
            timeout=timeout,
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return [], f"Naver news unavailable: {type(exc).__name__}"
    items = []
    for item in payload.get("items", [])[:limit]:
        title = _clean(item.get("title", ""))
        summary = _clean(item.get("description", ""))
        items.append(NewsItem(
            source="naver",
            title=title,
            url=item.get("originallink") or item.get("link", ""),
            published_at=item.get("pubDate", ""),
            summary=summary,
            sentiment_hint=_sentiment_hint(f"{title} {summary}"),
        ))
    return items, None


def fetch_yfinance_news(ticker: str, limit: int = 8) -> tuple[list[NewsItem], str | None]:
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception as exc:  # pragma: no cover - network dependent
        return [], f"yfinance news unavailable: {type(exc).__name__}"
    items: list[NewsItem] = []
    for item in raw[:limit]:
        title = _clean(item.get("title", ""))
        published_at = ""
        if item.get("providerPublishTime"):
            published_at = datetime.fromtimestamp(item["providerPublishTime"], timezone.utc).isoformat()
        items.append(NewsItem(
            source=f"yfinance/{item.get('publisher', 'unknown')}",
            title=title,
            url=item.get("link", ""),
            published_at=published_at,
            summary="",
            sentiment_hint=_sentiment_hint(title),
        ))
    return items, None


def fetch_x_recent(query: str, limit: int = 10, timeout: float = 4.0) -> tuple[list[NewsItem], str | None]:
    token = os.getenv("X_BEARER_TOKEN") or os.getenv("TWITTER_BEARER_TOKEN")
    if not token:
        return [], "X/Twitter bearer token missing"
    qs = urlencode({
        "query": f"{query} -is:retweet lang:ko OR lang:en",
        "max_results": max(10, min(limit, 100)),
        "tweet.fields": "created_at,public_metrics,lang",
    })
    try:
        payload = _request_json(
            f"https://api.x.com/2/tweets/search/recent?{qs}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return [], f"X recent search unavailable: {type(exc).__name__}"
    items = []
    for item in payload.get("data", [])[:limit]:
        title = _clean(item.get("text", ""))
        items.append(NewsItem(
            source="x",
            title=title[:220],
            url=f"https://x.com/i/web/status/{item.get('id')}",
            published_at=item.get("created_at", ""),
            sentiment_hint=_sentiment_hint(title),
        ))
    return items, None


def fetch_google_news_rss(query: str, limit: int = 8, timeout: float = 4.0) -> tuple[list[NewsItem], str | None]:
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/rss+xml"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        return [], f"Google News RSS unavailable: {type(exc).__name__}"
    titles = re.findall(r"<item>.*?<title><!\[CDATA\[(.*?)\]\]></title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>", text, re.S)
    items = []
    for title, link, published in titles[:limit]:
        items.append(NewsItem(
            source="google-news-rss",
            title=_clean(title),
            url=_clean(link),
            published_at=published,
            sentiment_hint=_sentiment_hint(title),
        ))
    return items, None


def collect_market_snapshot(ticker: str, news_query: str | None = None, include_social: bool = True) -> MarketSnapshot:
    price, prev, change, volume, source, notes = fetch_quote_yfinance(ticker)
    query = news_query or ticker
    news: list[NewsItem] = []
    for fetcher in (fetch_naver_news, fetch_google_news_rss):
        items, note = fetcher(query)
        news.extend(items)
        if note:
            notes.append(note)
    yf_items, yf_note = fetch_yfinance_news(ticker)
    news.extend(yf_items)
    if yf_note:
        notes.append(yf_note)
    if include_social:
        x_items, x_note = fetch_x_recent(query)
        news.extend(x_items)
        if x_note:
            notes.append(x_note)
    # Dedupe by normalized title to keep dashboard small and fast.
    seen = set()
    deduped: list[NewsItem] = []
    for item in news:
        key = item.title.lower()[:120]
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    return MarketSnapshot(
        ticker=ticker,
        price=price,
        previous_close=prev,
        change_pct=change,
        volume=volume,
        timestamp=datetime.now(timezone.utc).isoformat(),
        quote_source=source,
        news=deduped[:30],
        notes=notes,
    )
