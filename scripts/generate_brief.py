"""
奇美 AI Everywhere Brief — 自動產生器
--------------------------------------
流程：
  1. 從 RSS 抓取醫療 AI 相關新聞
  2. 送給 Claude API，要求排名評分並產生結構化 JSON
  3. 套用 brief.html 模板，輸出 output/index.html
"""

import os
import json
import datetime
import pathlib
import textwrap
import time

import anthropic
import feedparser
from jinja2 import Environment, FileSystemLoader
from zoneinfo import ZoneInfo


# ─── 設定 ────────────────────────────────────────────────────────────────

OUTPUT_DIR = pathlib.Path("output")
TEMPLATE_DIR = pathlib.Path("templates")

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TODAY = datetime.datetime.now(TAIPEI_TZ).date()
TODAY_STR = TODAY.strftime("%Y年%m月%d日")
WEEKDAY_MAP = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
WEEKDAY_STR = WEEKDAY_MAP[TODAY.weekday()]

# 需要「刪除/不再出現」的日期（ISO 格式）
EXCLUDED_DATE_ISOS: set[str] = {
    "2026-04-15",
    "2026-04-18",
    "2026-04-19",
}


def get_available_dates() -> list[dict]:
    """取得過去 7 天的日期資訊，根據根目錄是否有 JSON 判斷可用性。"""
    ROOT_DIR = pathlib.Path(".")
    dates = []
    for i in range(7):
        d = TODAY - datetime.timedelta(days=i)

        # 黑名單：直接不顯示該日期按鈕/ICON
        if d.isoformat() in EXCLUDED_DATE_ISOS:
            continue

        json_path = ROOT_DIR / f"{d.isoformat()}.json"
        is_today = (d == TODAY)
        dates.append(
            {
                "date": d,
                "month_day": d.strftime("%m/%d"),
                "date_iso": d.isoformat(),
                "weekday": WEEKDAY_MAP[d.weekday()],
                "available": json_path.exists() or is_today,
                "is_today": is_today,
            }
        )
    return dates


def parse_entry_date(entry) -> datetime.date | None:
    """嘗試從 RSS entry 解析發布日期，失敗回 None。"""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
            except (ValueError, TypeError):
                continue
    return None


def _normalize_title(title: str) -> str:
    """標題正規化：去空白、全小寫、去常見標點。"""
    import re

    return re.sub(r"[\s\W_]+", "", title or "").lower()


def load_excluded_set(days: int = 7) -> tuple[set[str], set[str]]:
    """讀取過去 N 天根目錄的 JSON，回傳 (excluded_urls, excluded_titles)。
    用於避免連續數日出現同一則新聞。"""
    ROOT_DIR = pathlib.Path(".")
    excluded_urls: set[str] = set()
    excluded_titles: set[str] = set()
    for i in range(1, days + 1):
        d = TODAY - datetime.timedelta(days=i)

        # 黑名單：不讀取該天資料
        if d.isoformat() in EXCLUDED_DATE_ISOS:
            continue

        json_path = ROOT_DIR / f"{d.isoformat()}.json"
        if not json_path.exists():
            continue
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
            for item in raw.get("items", []):
                url = (item.get("source_url") or "").strip()
                if url:
                    excluded_urls.add(url)
                title = _normalize_title(item.get("title", ""))
                if title:
                    excluded_titles.add(title)
        except Exception as e:
            print(f"  [WARN] 讀取歷史 JSON 失敗 {json_path}: {e}")
    return excluded_urls, excluded_titles


def load_all_days_data(items_today: list[dict]) -> dict:
    """讀取過去 7 天的 JSON，合併成一份字典嵌入 HTML。"""
    ROOT_DIR = pathlib.Path(".")
    all_data = {
        TODAY.isoformat(): {
            "date_str": TODAY_STR,
            "weekday": WEEKDAY_STR,
            "items": items_today,
        }
    }
    for i in range(1, 7):
        d = TODAY - datetime.timedelta(days=i)

        # 黑名單：不載入到 ALL_DAYS
        if d.isoformat() in EXCLUDED_DATE_ISOS:
            continue

        json_path = ROOT_DIR / f"{d.isoformat()}.json"
        if json_path.exists():
            try:
                raw = json.loads(json_path.read_text(encoding="utf-8"))
                all_data[d.isoformat()] = {
                    "date_str": raw.get("date", d.strftime("%Y年%m月%d日")),
                    "weekday": WEEKDAY_MAP[d.weekday()],
                    "items": raw.get("items", []),
                }
            except Exception as e:
                print(f"  [WARN] 讀取歷史 JSON 失敗 {json_path}: {e}")
    return all_data


# 醫療 AI 相關 RSS 來源
RSS_FEEDS = [
    # 國際
    "https://www.healthcareitnews.com/rss.xml",
    "https://www.fiercehealthcare.com/rss/xml",
    "https://medcitynews.com/feed/",
    "https://www.statnews.com/feed/",
    # 台灣
    "https://www.mohw.gov.tw/rss-16.html",
    "https://udn.com/rssfeed/news/2/6644?ch=news",  # 聯合報數位/科技
]

SYSTEM_PROMPT = textwrap.dedent(
    """
    你是醫療 AI 新聞的專業情報代理人。
    你的任務是分析「使用者提供的候選新聞清單」，進行專業評鑑後產出每日簡報。

    ## 最重要硬規則（請逐條遵守）
    - 你只能使用「候選新聞清單」裡提供的新聞；不得從訓練資料、記憶或網路自行補充任何新聞。
    - 候選新聞若為空，回傳 {"items": []}。
    - 候選新聞若不為空，items 不可為空：至少輸出 1 則。
    - items 數量必須等於 min(7, 候選新聞數量)。例如候選 2 則就輸出 2 則；候選 9 則就輸出 7 則。
    - 你可以改寫標題使其更清晰，但不得改變「新聞事件」的事實。
    - `author` 欄位末尾日期必須「完全等於」候選清單中的「實際發布日期」，不得改寫為今日、不得自行推測日期。
    - 嚴禁在 summary 或任何欄位中出現對奇美醫院的建議、行動方針、策略建議或啟示；summary 只陳述新聞本身事實與意義。
    - 請務必輸出「有效 JSON」，且只輸出 JSON，不要有任何解釋文字。

    ## 評分標準（0–10 分）
    - 臨床相關性：與醫療決策、照護品質的直接關聯程度
    - 創新程度：技術或模式的突破性
    - 奇美適用性：對奇美醫院或台灣醫療體系的參考價值
    - 可信度：來源機構、研究設計的可靠性

    ## 輸出格式（嚴格使用以下 JSON 結構）
    {
      "items": [
        {
          "rank": 1,
          "score": 9.5,
          "title": "新聞標題（繁體中文，可改寫使其更清晰）",
          "summary": "150–200字摘要，客觀說明新聞事件、背景與意義",
          "tags": ["標籤1", "標籤2", "標籤3"],
          "author": "作者姓名與職稱（來源）（日期）",
          "source_url": "https://原始網址（若有）"
        }
      ]
    }

    ## tags / 文字規則
    - 最多 7 則（由上方 items 數量規則決定）
    - tags 每則 2–4 個，繁體中文，不含 # 符號
    - summary 使用繁體中文、專業醫療用語，僅客觀描述新聞事實與意義
    - 若某則新聞無來源 URL，source_url 填 ""
"""
)


# ─── 新聞抓取 ─────────────────────────────────────────────────────────────


def fetch_news(
    max_items: int = 20,
    excluded_urls: set[str] | None = None,
    excluded_titles: set[str] | None = None,
    allow_yesterday_fallback: bool = True,
) -> list[dict]:
    """從 RSS 抓取新聞，套用「只收今日發布 + 去除歷史重複」規則。
    若今日結果不足 5 則，可回退納入昨日新聞，並在每篇標註 `is_from_yesterday`。"""
    excluded_urls = excluded_urls or set()
    excluded_titles = excluded_titles or set()
    YESTERDAY = TODAY - datetime.timedelta(days=1)

    today_articles: list[dict] = []
    yesterday_articles: list[dict] = []
    dropped_old = 0
    dropped_dup = 0
    dropped_undated = 0

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                link = (entry.get("link") or "").strip()
                title = (entry.get("title") or "").strip()
                norm_title = _normalize_title(title)

                if (link and link in excluded_urls) or (norm_title and norm_title in excluded_titles):
                    dropped_dup += 1
                    continue

                pub_date = parse_entry_date(entry)
                if pub_date is None:
                    dropped_undated += 1
                    continue

                item = {
                    "title": title,
                    "summary": entry.get("summary", entry.get("description", ""))[:400].strip(),
                    "link": link,
                    "source": feed.feed.get("title", url),
                    "date": entry.get("published", pub_date.isoformat()),
                    "pub_date": pub_date.isoformat(),
                    "is_from_yesterday": False,
                }

                if pub_date == TODAY:
                    today_articles.append(item)
                elif pub_date == YESTERDAY and allow_yesterday_fallback:
                    item["is_from_yesterday"] = True
                    yesterday_articles.append(item)
                else:
                    dropped_old += 1
        except Exception as e:
            print(f"[WARN] RSS 失敗 {url}: {e}")

    print(f"  [篩選] 今日 {len(today_articles)} 則 / 昨日備用 {len(yesterday_articles)} 則")
    print(f"  [篩選] 過濾掉：日期過舊 {dropped_old}、重複 {dropped_dup}、無日期 {dropped_undated}")

    if len(today_articles) >= 5 or not allow_yesterday_fallback:
        articles = today_articles
    else:
        needed = max_items - len(today_articles)
        articles = today_articles + yesterday_articles[:needed]
        if yesterday_articles[:needed]:
            print(f"  [篩選] 今日不足 5 則，補入 {len(yesterday_articles[:needed])} 則昨日新聞")

    return articles[:max_items]


def format_news_for_prompt(articles: list[dict]) -> str:
    """格式化新聞清單給 Claude，附上實際發布日期讓模型能驗證。"""
    if not articles:
        return "（今日 RSS 無法取得新聞，請回傳 {\"items\": []}，不要編造內容）"
    lines = [
        f"今日為 {TODAY.isoformat()}（{TODAY_STR}）。",
        f"候選新聞總數：{len(articles)}。",
        "以下是候選新聞清單（已通過日期過濾、去重檢查後）：\n",
    ]
    for i, a in enumerate(articles, 1):
        tag = "【今日】" if not a.get("is_from_yesterday") else "【昨日備用】"
        lines.append(f"[{i}] {tag} 來源：{a['source']}")
        lines.append(f"    標題：{a['title']}")
        lines.append(f"    實際發布日期：{a.get('pub_date', a.get('date', ''))}")
        if a["summary"]:
            lines.append(f"    摘要：{a['summary'][:300]}")
        if a["link"]:
            lines.append(f"    URL：{a['link']}")
        lines.append("")
    lines.append("⚠️ 輸出每則 item 時，`author` 欄位結尾的日期務必使用上方「實際發布日期」，不得臆測或改寫。")
    return "\n".join(lines)


# ─── Claude API 呼叫 ───────────────────────────────────────────────��──────


def parse_json_response(raw: str) -> list[dict] | None:
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw)["items"]
    except Exception:
        pass

    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if not part:
                continue
            try:
                return json.loads(part)["items"]
            except Exception:
                continue
    return None


def call_claude(news_text: str, had_candidates: bool, max_retries: int = 3) -> list[dict]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  [API] 第 {attempt} 次嘗試...")
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": news_text}],
            )

            if not message.content:
                print(f"  [WARN] Claude 回傳空 content（attempt {attempt}）")
                time.sleep(5)
                continue

            raw = message.content[0].text.strip()
            print(f"  [API] 回應長度：{len(raw)} 字元")

            items = parse_json_response(raw)
            if items is None:
                print(f"  [WARN] JSON 解析失敗（attempt {attempt}），原始回應前 200 字：{raw[:200]}")
                time.sleep(5)
                continue

            if had_candidates and len(items) == 0:
                print("  [WARN] Claude 回傳空 items，但候選新聞不為空；判定為異常回覆，將重試。")
                print(f"  [WARN] 原始回應前 200 字：{raw[:200]}")
                time.sleep(8)
                continue

            return items

        except Exception as e:
            print(f"  [ERROR] API 呼叫失敗（attempt {attempt}）：{e}")

        if attempt < max_retries:
            time.sleep(10)

    raise RuntimeError(f"Claude API 在 {max_retries} 次嘗試後仍無法取得有效回應")


# ─── HTML 產生 ─────────────────────────────────────────────────────────────


def render_html(items: list[dict], available_dates: list[dict] | None = None, all_days_json: str = "{}") -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
    )
    template = env.get_template("brief.html")
    return template.render(
        items=items,
        today=TODAY_STR,
        weekday=WEEKDAY_STR,
        available_dates=available_dates or [],
        today_iso=TODAY.isoformat(),
        all_days_json=all_days_json,
    )


# ─── 主程式 ───────────────────────────────────────────────────────────────


def main():
    now_taipei = datetime.datetime.now(TAIPEI_TZ)
    print(f"[{TODAY_STR}] === 奇美 AI Everywhere Brief 自動產生 ===")
    print(f"  [TIME] Asia/Taipei now: {now_taipei.isoformat(timespec='seconds')}")
    print(f"  [TIME] TODAY iso: {TODAY.isoformat()}")

    print("→ 建立排除清單（過去 7 天已納入新聞）...")
    excluded_urls, excluded_titles = load_excluded_set(days=7)
    print(f"  排除清單：URL {len(excluded_urls)} 筆、標題 {len(excluded_titles)} 筆")

    print("→ 抓取 RSS 新聞（僅今日發布，去除歷史重複）...")
    articles = fetch_news(
        excluded_urls=excluded_urls,
        excluded_titles=excluded_titles,
        allow_yesterday_fallback=True,
    )
    news_text = format_news_for_prompt(articles)
    print(f"  取得 {len(articles)} 則候選新聞（已通過日期與去重過濾）")

    print("→ 呼叫 Claude API 進行評鑑...")
    items = call_claude(news_text, had_candidates=(len(articles) > 0), max_retries=3)
    print(f"  產生 {len(items)} 則精選新聞")

    OUTPUT_DIR.mkdir(exist_ok=True)
    json_path = OUTPUT_DIR / f"{TODAY.isoformat()}.json"
    json_path.write_text(
        json.dumps({"date": TODAY_STR, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  JSON 備份：{json_path}")

    print("→ 載入歷史資料並套用模板...")
    available_dates = get_available_dates()
    all_days_data = load_all_days_data(items)
    all_days_json = json.dumps(all_days_data, ensure_ascii=False).replace("</script>", r"<\/script>")

    html = render_html(items, available_dates, all_days_json)

    output_path = OUTPUT_DIR / "index.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"  輸出：{output_path}")

    print(f"[{TODAY_STR}] === 完成 ===")


if __name__ == "__main__":
    main()
