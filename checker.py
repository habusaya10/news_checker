import json
import os
import re
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
        if text and title:
            items.append(
                {
                    "text": text[:300],
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
            if text and title:
                items.append(
                    {
                        "text": text[:300],
                        "link": urljoin(url, link_tag["href"]),
                    }
                )
        browser.close()
    return deduplicate(items)


def deduplicate(items):
    unique = []
    seen = set()
    for item in items:
        key = (item["text"], item["link"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def fetch_news(site):
    if site.get("dynamic", False):
        return fetch_dynamic_news(site["url"])
    return fetch_static_news(site["url"])


def find_new_items(old_items, new_items):
    old_keys = {(item["text"], item.get("link", "")) for item in old_items}
    return [
        item
        for item in new_items
        if (item["text"], item.get("link", "")) not in old_keys
    ]


def send_discord(site_name, new_items):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")

    lines = [f"📢 **【{site_name}】ニュースリリース更新**", "━━━━━━━━━━━━━━━━━━"]
    for item in new_items[:5]:
        lines.append(f"📄 {item['text'][:150]}")
        if item["link"]:
            lines.append(f"🔗 {item['link']}")
        lines.append("")

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": "\n".join(lines)},
        timeout=20,
    )
    response.raise_for_status()


def main():
    print(f"=== チェック開始: {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    last_data = load_last_data()
    new_data = dict(last_data)

    for site in load_urls():
        name = site["name"]
        url = site["url"]
        print(f"チェック中: {name} ({url})")
        try:
            items = fetch_news(site)
            if not items:
                raise RuntimeError("ニュースを1件も取得できませんでした")

            old_items = last_data.get(url)
            new_data[url] = items
            if old_items is None:
                print(f"  → 初回取得。{len(items)}件を保存しました（通知なし）。")
                continue

            new_items = find_new_items(old_items, items)
            if new_items:
                send_discord(name, new_items)
                print(f"  → {len(new_items)}件の新着を通知しました。")
            else:
                print("  → 変化なし")
        except Exception as error:
            # 取得失敗時は前回データを消さず、翌日にもう一度試す。
            print(f"  → エラー: {error}")

    save_data(new_data)
    print("=== チェック完了 ===")


if __name__ == "__main__":
    main()
