import os
import json
import yaml
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ── 設定 ──────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
URLS_FILE = "urls.yaml"
DATA_FILE = "last_data.json"
# ──────────────────────────────────────────────────


def load_urls():
    """監視URLリストを読み込む"""
    with open(URLS_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["sites"]


def load_last_data():
    """前回保存したデータを読み込む"""
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    """最新データを保存する"""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_news(url):
    """サイトのニュース一覧を取得してリストで返す"""
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=15)
    res.encoding = res.apparent_encoding
    soup = BeautifulSoup(res.text, "html.parser")

    news_items = []

    # <li> タグ内に日付っぽいテキストがある要素を探す
    for li in soup.find_all("li"):
        text = li.get_text(separator=" ", strip=True)
        # 日付パターン（例: 2026/05/20 や 2026.05.20）が含まれているものだけ対象
        if any(f"{y}/" in text or f"{y}." in text for y in range(2020, 2030)):
            link_tag = li.find("a")
            link = ""
            if link_tag and link_tag.get("href"):
                href = link_tag["href"]
                # 相対URLを絶対URLに変換
                if href.startswith("http"):
                    link = href
                else:
                    from urllib.parse import urljoin
                    link = urljoin(url, href)
            news_items.append({
                "text": text[:200],  # 長すぎる場合は切り詰め
                "link": link
            })

    return news_items


def make_hash(items):
    """ニュースリストのハッシュ値を作る（変化検知用）"""
    content = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(content.encode()).hexdigest()


def find_new_items(old_items, new_items):
    """新しく追加されたニュースを返す"""
    old_texts = {item["text"] for item in old_items}
    return [item for item in new_items if item["text"] not in old_texts]


def send_discord(site_name, new_items):
    """Discordに通知を送る"""
    lines = [f"📢 **【{site_name}】ニュースリリース更新**", "━━━━━━━━━━━━━━━━━━"]
    for item in new_items[:5]:  # 最大5件まで表示
        lines.append(f"📄 {item['text'][:100]}")
        if item["link"]:
            lines.append(f"🔗 {item['link']}")
        lines.append("")
    message = "\n".join(lines)

    payload = {"content": message}
    res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    if res.status_code not in (200, 204):
        print(f"Discord送信失敗: {res.status_code} {res.text}")
    else:
        print(f"Discord通知送信完了: {site_name}")


def main():
    print(f"=== チェック開始: {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    sites = load_urls()
    last_data = load_last_data()
    new_data = {}

    for site in sites:
        name = site["name"]
        url = site["url"]
        print(f"チェック中: {name} ({url})")

        try:
            items = fetch_news(url)
            new_data[url] = items

            old_items = last_data.get(url, [])

            # 初回実行時は通知しない（ベースラインを作るだけ）
            if not old_items:
                print(f"  → 初回取得。{len(items)}件を保存しました。")
                continue

            new_items = find_new_items(old_items, items)

            if new_items:
                print(f"  → {len(new_items)}件の新着を検出！Discordへ通知します。")
                send_discord(name, new_items)
            else:
                print(f"  → 変化なし")

        except Exception as e:
            print(f"  → エラー: {e}")

    save_data(new_data)
    print("=== チェック完了 ===")


if __name__ == "__main__":
    main()
