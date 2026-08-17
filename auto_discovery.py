"""日本の全上場企業の公式ニュースページを自動管理する。

urls.yaml はユーザー指定の固定銘柄であり、このスクリプトは変更しない。
自動抽出した銘柄だけを auto_urls.json に保存する。
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from yfinance import EquityQuery


OUTPUT_FILE = "auto_urls.json"
TIMEOUT = 15
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; news-checker/1.0)"}
DATE_RE = re.compile(
    r"20\d{2}(?:[./-]\d{1,2}[./-]\d{1,2}|年\s*\d{1,2}月\s*\d{1,2}日)"
)
NEWS_WORDS = ("ニュースリリース", "irニュース", "ir情報", "適時開示", "news release")


def load_previous():
    if not os.path.exists(OUTPUT_FILE):
        return {"sites": []}
    with open(OUTPUT_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def screen_japanese_stocks():
    # PER・PBRでは絞らず、日本地域の全株式を取得する。
    query = EquityQuery("eq", ["region", "jp"])
    quotes = []
    offset = 0
    while True:
        result = yf.screen(
            query,
            offset=offset,
            size=250,
            sortField="ticker",
            sortAsc=True,
        )
        page = result.get("quotes", [])
        quotes.extend(page)
        if len(page) < 250:
            break
        offset += len(page)
        if offset >= 5000:
            break
    screened = {quote["symbol"]: quote for quote in quotes if quote.get("symbol")}
    if not screened:
        raise RuntimeError("スクリーニング結果が空のため、前回データを維持します")
    return screened


def same_official_domain(home_url, candidate_url):
    home = urlparse(home_url).netloc.lower().removeprefix("www.")
    candidate = urlparse(candidate_url).netloc.lower().removeprefix("www.")
    return bool(home and candidate and (candidate == home or candidate.endswith("." + home)))


def fetch_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    return BeautifulSoup(response.text, "html.parser")


def ranked_links(base_url, soup):
    candidates = []
    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True).lower()
        href = urljoin(base_url, link["href"])
        if not href.startswith("http") or not same_official_domain(base_url, href):
            continue
        score = 0
        if "ニュースリリース" in text or "news release" in text:
            score += 100
        if "irニュース" in text:
            score += 90
        if "ir" in text or "投資家" in text:
            score += 30
        path = urlparse(href).path.lower()
        if "/news" in path or "/release" in path:
            score += 40
        if score:
            candidates.append((score, href))
    return [url for _, url in sorted(set(candidates), reverse=True)]


def page_looks_like_news(url):
    try:
        soup = fetch_soup(url)
    except Exception:
        return False
    text = soup.get_text(" ", strip=True)
    dated_links = 0
    for link in soup.find_all("a", href=True):
        parent_text = ""
        element = link
        for _ in range(5):
            element = element.parent
            if element is None:
                break
            parent_text = element.get_text(" ", strip=True)
            if DATE_RE.search(parent_text):
                dated_links += 1
                break
    return dated_links >= 2 and any(word in text.lower() for word in NEWS_WORDS)


def discover_one(symbol, quote, previous_by_symbol):
    old = previous_by_symbol.get(symbol)
    if old and page_looks_like_news(old["url"]):
        return old

    info = yf.Ticker(symbol).get_info()
    home_url = info.get("website")
    name = info.get("shortName") or quote.get("shortName") or symbol
    if not home_url:
        raise RuntimeError("公式サイトURLなし")

    home_soup = fetch_soup(home_url)
    first_candidates = ranked_links(home_url, home_soup)[:12]
    second_candidates = []
    for candidate in first_candidates[:5]:
        if page_looks_like_news(candidate):
            return {
                "name": name,
                "url": candidate,
                "symbol": symbol,
                "auto": True,
            }
        try:
            second_candidates.extend(ranked_links(candidate, fetch_soup(candidate))[:8])
        except Exception:
            pass

    for candidate in second_candidates:
        if page_looks_like_news(candidate):
            return {
                "name": name,
                "url": candidate,
                "symbol": symbol,
                "auto": True,
            }
    raise RuntimeError("公式ニュースページを確認できず")


def main():
    previous = load_previous()
    previous_by_symbol = {
        site["symbol"]: site for site in previous.get("sites", []) if site.get("symbol")
    }
    quotes = screen_japanese_stocks()
    sites = []
    unresolved = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(discover_one, symbol, quote, previous_by_symbol): symbol
            for symbol, quote in quotes.items()
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                sites.append(future.result())
            except Exception as error:
                # 一時的な通信失敗では、条件内の既存URLを消さない。
                if symbol in previous_by_symbol:
                    sites.append(previous_by_symbol[symbol])
                unresolved.append({"symbol": symbol, "reason": str(error)[:150]})

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {"market": "Japan", "scope": "all listed equities"},
        "sites": sorted(sites, key=lambda site: site["symbol"]),
        "unresolved": sorted(unresolved, key=lambda item: item["symbol"]),
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    print(f"日本株: {len(quotes)} / URL確認済み: {len(sites)} / 保留: {len(unresolved)}")


if __name__ == "__main__":
    main()
