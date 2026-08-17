import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup


DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
URLS_FILE = "urls.yaml"
AUTO_URLS_FILE = "auto_urls.json"
DATA_FILE = "last_data.json"
DATE_RE = re.compile(
    r"20\d{2}(?:[./-]\d{1,2}[./-]\d{1,2}|年\s*\d{1,2}月\s*\d{1,2}日)"
)


def load_urls():
    with open(URLS_FILE, "r", encoding="utf-8") as file:
        sites = yaml.safe_load(file)["sites"]
    if os.path.exists(AUTO_URLS_FILE):
        with open(AUTO_URLS_FILE, "r", encoding="utf-8") as file:
            auto_data = json.load(file)
        sites.extend(auto_data.get("sites", []))
    # 固定銘柄と自動銘柄で同じURLがあった場合は固定銘柄を優先する。
    return list({site["url"]: site for site in reversed(sites)}.values())


def load_last_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def fetch_static_news(url):
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (news-checker)"},
        timeout=30,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.text, "html.parser")
    items = []

    for link_tag in soup.find_all("a", href=True):
        element = link_tag
        text = ""
        for _ in range(5):
            element = element.parent
            if element is None:
                break
            candidate = element.get_text(separator=" ", strip=True)
            if DATE_RE.search(candidate):
                text = candidate
                break
        title = link_tag.get_text(separator=" ", strip=True)
        date_match = DATE_RE.search(text)
        if date_match and title:
            items.append(
                {
                    "date": date_match.group(0),
                    "title": title[:250],
                    "text": f"{date_match.group(0)} {title}"[:300],
                    "link": urljoin(url, link_tag["href"]),
                }
            )
    return deduplicate(items)


def fetch_dynamic_news(url):
    """JavaScriptでニュースが表示されるサイト（前澤HDなど）を取得する。"""
    from playwright.sync_api import sync_playwright

    items = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (news-checker)")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        soup = BeautifulSoup(page.content(), "html.parser")
        for link_tag in soup.find_all("a", href=True):
            element = link_tag
            text = ""
            for _ in range(5):
                element = element.parent
                if element is None:
                    break
                candidate = element.get_text(separator=" ", strip=True)
                if DATE_RE.search(candidate):
                    text = candidate
                    break
            title = link_tag.get_text(separator=" ", strip=True)
            date_match = DATE_RE.search(text)
            if date_match and title:
                items.append(
                    {
                        "date": date_match.group(0),
                        "title": title[:250],
                        "text": f"{date_match.group(0)} {title}"[:300],
                        "link": urljoin(url, link_tag["href"]),
                    }
                )
        browser.close()
    return deduplicate(items)


def deduplicate(items):
    unique = []
    seen = set()
    for item in items:
        key = item["link"] or (item.get("date", ""), item.get("title", item["text"]))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def fetch_news(site):
    if site.get("dynamic", False):
        return fetch_dynamic_news(site["url"])
    return fetch_static_news(site["url"])


def find_new_items(old_items, new_items):
    # 以前の保存形式との互換性を保つため、リンクがある記事はリンクを
    # 一意キーにする。表示文を改善しても既存記事を再通知しない。
    def identity(item):
        link = item.get("link", "").strip()
        if link:
            return ("link", link)
        title = item.get("title") or item.get("text", "")
        return ("text", re.sub(r"\s+", " ", title).strip())

    old_keys = {identity(item) for item in old_items}
    return [item for item in new_items if identity(item) not in old_keys]


def send_discord(site_name, new_items):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")

    header = f"📢 **【{site_name}】ニュースリリース更新**\n新着 **{len(new_items)}件**"
    messages = []
    current = header

    # 件数制限を設けず、Discordの1投稿2,000文字制限に合わせて分割する。
    for item in new_items:
        date = item.get("date", "")
        title = item.get("title") or item.get("text", "更新内容を確認してください")
        block = f"\n\n📅 {date or '日付記載なし'}\n📄 **{title[:250]}**"
        if item.get("link"):
            block += f"\n🔗 {item['link']}"
        if len(current) + len(block) > 1900:
            messages.append(current)
            current = f"📢 **【{site_name}】続き**" + block
        else:
            current += block
    messages.append(current)

    for message in messages:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=20,
        )
        response.raise_for_status()


def main():
    print(f"=== チェック開始: {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    last_data = load_last_data()
    new_data = dict(last_data)

    sites = load_urls()
    # 全上場企業を現実的な時間で確認できるよう、ページ取得だけを並列化する。
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_news, site): site for site in sites}
        fetched = {}
        for future in as_completed(futures):
            site = futures[future]
            try:
                fetched[site["url"]] = (future.result(), None)
            except Exception as error:
                fetched[site["url"]] = (None, error)

    for site in sites:
        name = site["name"]
        url = site["url"]
        print(f"チェック中: {name} ({url})")
        try:
            items, fetch_error = fetched[url]
            if fetch_error:
                raise fetch_error
            if not items:
                raise RuntimeError("ニュースを1件も取得できませんでした")

            old_items = last_data.get(url)
            if old_items is None:
                new_data[url] = items
                print(f"  → 初回取得。{len(items)}件を保存しました（通知なし）。")
                continue

            new_items = find_new_items(old_items, items)
            if new_items:
                send_discord(name, new_items)
                # Discord送信に成功した場合だけ既読状態を更新する。
                new_data[url] = items
                print(f"  → {len(new_items)}件の新着を通知しました。")
            else:
                new_data[url] = items
                print("  → 変化なし")
        except Exception as error:
            # 取得失敗時は前回データを消さず、翌日にもう一度試す。
            print(f"  → エラー: {error}")

    save_data(new_data)
    print("=== チェック完了 ===")


if __name__ == "__main__":
    main()
