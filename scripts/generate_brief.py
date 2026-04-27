"""
奇美 AI Everywhere Brief — 自動產生器（RSS + Claude 評分版）
-----------------------------------------------------------
流程：
  1. 從 RSS_FEEDS 蒐集今日 + 昨日的候選新聞（feedparser，零 token）
  2. 讀過去 7 天 JSON 建立去重排除清單
  3. 呼叫 Claude API（不啟用 web_search）→ 從候選列表挑選 5–10 則、評分、改寫繁中、輸出 JSON
  4. 解析回傳 JSON、套用 brief.html 模板，輸出 output/index.html

設計理念：
  - 來源策展由 RSS_FEEDS 控制（取代「搜尋查詢」），與 Skill 中的「優先來源」一致
  - 主流醫療 AI 來源（STAT、Healthcare IT News、JAMA、NEJM、Lancet、AMA、Stanford HAI 等）皆用各自 RSS
  - 主題式搜尋（FDA approval、AI radiology、台灣醫療 AI 等）改用 Google News RSS（依關鍵字訂閱）
  - Claude 只做：篩日期 + 評分 + 翻譯改寫 + JSON 化（單次呼叫遠低於 30K input tokens）

評分仍採 Skill 4 維度（臨床相關性 / 產業影響 / 政策法規 / 技術突破）；輸出 5–10 則；前 3 為 Top 3（HTML 標示）。
JSON schema 與舊版完全相容（rank/score/title/summary/tags/author/source_url）。
"""

import os
import json
import datetime
import pathlib
import re
import textwrap
import time

import anthropic
import feedparser
from jinja2 import Environment, FileSystemLoader
from zoneinfo import ZoneInfo


# ─── 設定 ────────────────────────────────────────────────────────────────

OUTPUT_DIR = pathlib.Path("output")
TEMPLATE_DIR = pathlib.Path("templates")

# 明確指定台灣時區做「今日」日切，避免 GitHub runner 預設 UTC 造成跨日誤判
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TODAY = datetime.datetime.now(TAIPEI_TZ).date()
YESTERDAY = TODAY - datetime.timedelta(days=1)
TODAY_STR = TODAY.strftime("%Y年%m月%d日")
WEEKDAY_MAP = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
WEEKDAY_STR = WEEKDAY_MAP[TODAY.weekday()]

# 需要「刪除/不再出現」的日期（ISO 格式）
EXCLUDED_DATE_ISOS: set[str] = {
    "2026-04-15",
    "2026-04-18",
    "2026-04-19",
}

# Claude 模型設定（不再使用 web_search server tool）
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_OUTPUT_TOKENS = 8000

# ─── RSS 來源（取代搜尋查詢；Skill 中的「優先來源」原則此處實作） ──────────
# 每組 (顯示名稱, RSS URL)；新增/移除來源直接動這個 list 即可
RSS_FEEDS: list[tuple[str, str]] = [
    # ── 醫療 AI 主流英文來源 ──
    ("STAT News",            "https://www.statnews.com/feed/"),
    ("Healthcare IT News",   "https://www.healthcareitnews.com/feed"),
    ("AMA",                  "https://www.ama-assn.org/rss.xml"),
    ("Stanford HAI",         "https://hai.stanford.edu/news/rss.xml"),
    ("Becker's Health IT",   "https://www.beckershospitalreview.com/healthcare-information-technology.feed"),
    ("MIT Tech Review",      "https://www.technologyreview.com/feed/"),
    ("FierceBiotech",        "https://www.fiercebiotech.com/rss/xml"),
    ("MedCity News",         "https://medcitynews.com/feed/"),

    # ── 期刊 / 政策 ──
    ("Nature",               "https://www.nature.com/nature.rss"),
    ("The Lancet",           "https://www.thelancet.com/rssfeed/lancet_current.xml"),
    ("FDA Press Releases",   "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"),

    # ── Google News 主題式 RSS（取代原來的關鍵字 search query） ──
    ("GNews-Medical AI",     "https://news.google.com/rss/search?q=%22medical+AI%22+OR+%22healthcare+AI%22&hl=en-US&gl=US&ceid=US:en"),
    ("GNews-FDA AI approval","https://news.google.com/rss/search?q=%22FDA%22+%22artificial+intelligence%22+approval&hl=en-US&gl=US&ceid=US:en"),
    ("GNews-AI radiology",   "https://news.google.com/rss/search?q=%22AI%22+%22radiology%22+OR+%22pathology%22&hl=en-US&gl=US&ceid=US:en"),
    ("GNews-AI clinical",    "https://news.google.com/rss/search?q=%22AI%22+%22clinical+decision%22+OR+%22hospital%22&hl=en-US&gl=US&ceid=US:en"),

    # ── 台灣醫療 AI（Google News 中文搜尋）──
    ("GNews-台灣醫療AI",      "https://news.google.com/rss/search?q=%E9%86%AB%E7%99%82+AI+OR+%E4%BA%BA%E5%B7%A5%E6%99%BA%E6%85%A7&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("GNews-台灣健保AI",      "https://news.google.com/rss/search?q=%E5%81%A5%E4%BF%9D+AI+OR+%E9%86%AB%E9%99%A2+%E6%99%BA%E6%85%A7&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
]

# 每個 RSS feed 取最新幾條檢查
RSS_PER_FEED_LIMIT = 25


# ─── 工具函式 ────────────────────────────────────────────────────────────


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


def _normalize_title(title: str) -> str:
    """標題正規化：去空白、全小寫、去常見標點。"""
    return re.sub(r"[\s\W_]+", "", title or "").lower()


def _parse_entry_date(entry) -> datetime.date | None:
    """從 RSS entry 抽出發布日期（轉成 Asia/Taipei 的 date）。"""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                dt = datetime.datetime(*t[:6], tzinfo=ZoneInfo("UTC"))
                return dt.astimezone(TAIPEI_TZ).date()
            except Exception:
                continue
    # 退一步：解析字串
    for key in ("published", "updated", "pubDate"):
        s = entry.get(key)
        if s:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                return dt.astimezone(TAIPEI_TZ).date()
            except Exception:
                continue
    return None


def _strip_html(text: str) -> str:
    """簡易去除 HTML tag、解 entity，並截斷過長。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_rss_candidates() -> list[dict]:
    """從 RSS_FEEDS 蒐集今日 + 昨日的候選新聞。
    回傳格式：每筆 dict 含 source/title/summary/url/published_iso。
    所有日期過濾以 Asia/Taipei 為準。"""
    print(f"-> 從 {len(RSS_FEEDS)} 個 RSS 來源蒐集候選新聞（accept dates: {YESTERDAY.isoformat()} / {TODAY.isoformat()}）...")
    accept_dates = {TODAY, YESTERDAY}
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for source_name, feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            entries = (feed.entries or [])[:RSS_PER_FEED_LIMIT]
            kept = 0
            for entry in entries:
                pub_date = _parse_entry_date(entry)
                if pub_date is None:
                    continue
                if pub_date not in accept_dates:
                    continue

                url = (entry.get("link") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                title = _strip_html(entry.get("title") or "")
                summary_raw = entry.get("summary") or entry.get("description") or ""
                summary = _strip_html(summary_raw)[:600]
                if not title:
                    continue

                candidates.append({
                    "source": source_name,
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "published_iso": pub_date.isoformat(),
                })
                kept += 1
            print(f"  [{source_name}] 取得 {kept} / {len(entries)} 則符合日期")
        except Exception as e:
            print(f"  [WARN] {source_name} 抓取失敗：{e}")

    print(f"  RSS 總候選數：{len(candidates)}（{TODAY.isoformat()} + {YESTERDAY.isoformat()}）")
    return candidates


def filter_candidates_against_history(
    candidates: list[dict],
    excluded_urls: set[str],
    excluded_titles: set[str],
) -> list[dict]:
    """在送進 Claude 之前，先把過去 7 天已納入的稿件從候選中剔除。
    這樣可以同時節省 input tokens 並避免模型誤選重複稿。"""
    out: list[dict] = []
    dropped = 0
    for c in candidates:
        if c["url"] in excluded_urls:
            dropped += 1
            continue
        if _normalize_title(c["title"]) in excluded_titles:
            dropped += 1
            continue
        out.append(c)
    if dropped:
        print(f"  預過濾：剔除 {dropped} 則過去 7 天已納入；剩 {len(out)} 則送 Claude")
    return out


def load_excluded_set(days: int = 7) -> tuple[set[str], set[str], list[dict]]:
    """讀取過去 N 天根目錄的 JSON。
    回傳：
      excluded_urls   — 用於 prompt 排除清單
      excluded_titles — 用於 prompt 排除清單
      excluded_pairs  — 給 prompt 的 (date, title) 對照表，方便模型辨識
    """
    ROOT_DIR = pathlib.Path(".")
    excluded_urls: set[str] = set()
    excluded_titles: set[str] = set()
    excluded_pairs: list[dict] = []
    for i in range(1, days + 1):
        d = TODAY - datetime.timedelta(days=i)

        if d.isoformat() in EXCLUDED_DATE_ISOS:
            continue

        json_path = ROOT_DIR / f"{d.isoformat()}.json"
        if not json_path.exists():
            continue
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
            for item in raw.get("items", []):
                url = (item.get("source_url") or "").strip()
                title = (item.get("title") or "").strip()
                if url:
                    excluded_urls.add(url)
                if title:
                    excluded_titles.add(_normalize_title(title))
                if title or url:
                    excluded_pairs.append(
                        {"date": d.isoformat(), "title": title, "url": url}
                    )
        except Exception as e:
            print(f"  [WARN] 讀取歷史 JSON 失敗 {json_path}: {e}")
    return excluded_urls, excluded_titles, excluded_pairs


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


# ─── Skill prompt（轉成系統 prompt） ──────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = textwrap.dedent("""
    你是奇美 AI Everywhere 情報代理人，每天為奇美醫療體系產出醫療 AI 每日情報簡報。
    目標讀者：醫院領導層、各臨床科主任、資訊部與數位轉型團隊。

    語言規則：所有輸出一律使用繁體中文。固有名詞（如 FDA、DeepMind、GPT-4）、工具名稱、URL 保留英文。

    ## 今日日期（已由系統設定，請務必沿用）
    - 今日：{today_iso}（{today_zh}）
    - 昨日：{yesterday_iso}

    ## 任務說明

    使用者訊息會提供一份「RSS 候選新聞列表」，已由 Python 預先從以下來源蒐集，且僅含發布日期為今日（{today_iso}）或昨日（{yesterday_iso}）的稿件：
    STAT、Healthcare IT News、AMA、Stanford HAI、Becker's Health IT、MIT Technology Review、FierceBiotech、MedCity News、Nature、The Lancet、FDA Press Releases、Google News（醫療 AI / FDA AI / AI radiology / 台灣醫療 AI / 健保 AI 等主題訂閱）。

    每筆候選包含：來源名稱、標題（多為英文）、摘要、URL、實際發布日期。

    你的工作（不需呼叫任何工具，純評分翻譯改寫）：
    1. 篩選：從候選中挑出「與醫療 AI 領域確實相關、且具醫院領導層／臨床參考價值」的 5–10 則。
    2. 去重：排除已在過去 7 天簡報中出現過的稿件（清單見下）。
    3. 評分：每則以 0–10 分評（4 維度，見下）。
    4. 改寫：標題譯/改寫成清晰的繁體中文（保留固有名詞）；摘要重寫為 150–200 字繁體中文，客觀說明事件、背景與意義。
    5. 輸出：依評分由高至低排序，輸出 JSON。

    ## 過去 7 天去重清單（命中即排除）

    {excluded_block}

    ## 納入規則

    - 優先選 {today_iso} 的稿件；若篩完今日合格稿件不足 5 則，補入 {yesterday_iso} 的稿件至 5 則為止。
    - 與醫療 AI 完全無關者一律排除（純消費 AI、純股市報導、純政治新聞、行銷稿、純意見文章）。
    - 一般醫療新聞（非 AI）排除；一般 AI 新聞（非醫療）也排除；要兩者交集才納入。
    - 寧可寬鬆收錄、勿過度嚴格排除：候選若 ≥ 5 則，輸出至少 5 則；候選 < 5 則時全數輸出。
    - 嚴禁編造：所有 source_url 必須來自候選列表，不得自行加入候選中沒有的新聞。

    ## 評分標準（0–10 分）

    | 標準 | 最高分 |
    |------|--------|
    | 臨床相關性（直接影響臨床工作） | +3 |
    | 產業影響力（對醫療產業的廣泛影響） | +3 |
    | 政策／法規影響 | +2 |
    | 技術突破程度 | +2 |

    依評分由高至低排序。前 3 名為「Top 3 精選」（HTML 完整日報會標示，LINE 訊息不另設 Top 3 區塊）。

    ## 輸出格式（嚴格使用以下 JSON 結構，且只輸出 JSON）

    最後一則訊息只輸出下列 JSON，不要附加任何 markdown、說明或前後文：

    ```
    {{
      "items": [
        {{
          "rank": 1,
          "score": 9.5,
          "title": "繁體中文標題（可改寫使其更清晰）",
          "summary": "150–200字繁體中文摘要，客觀說明新聞事件、背景與意義",
          "tags": ["標籤1", "標籤2", "標籤3"],
          "author": "第一作者或媒體（媒體名稱｜YYYY-MM-DD）",
          "source_url": "https://候選列表中的原始網址"
        }}
      ]
    }}
    ```

    ## 強制規則

    - author 欄位：格式為「第一作者姓名（媒體名稱｜YYYY-MM-DD）」。RSS 通常無作者資訊，第一作者不明時填「未標示（媒體名稱｜YYYY-MM-DD）」。日期取候選列表中該則的 published_iso 值，不得改寫為今日。
    - source_url 欄位：必須與候選列表中該則的 url 完全一致。
    - tags：每則 2–4 個，繁體中文，不含 # 符號。
    - summary：150–200 字（允許略短），繁體中文，僅客觀描述新聞事實與意義；不得加入對奇美的建議、行動方針、策略建議、啟示、下一步等。
    - 嚴禁在 JSON 中出現換行符號（必要時用全形標點分句）。
    - 最終回應必須是純 JSON，禁止任何 ```json``` 或解釋文字包裹。
    - 候選列表若為空或全數無關，回傳 {{"items": []}}，不得編造。
""").strip()


def build_excluded_block(excluded_pairs: list[dict]) -> str:
    """把過去 7 天的標題清單格式化成 prompt 可讀的條列。"""
    if not excluded_pairs:
        return "（過去 7 天無歷史紀錄，本次無需去重）"
    lines = []
    for p in excluded_pairs[:25]:  # 上限 25 則（保留最近期最相關的，避免吃光 token 預算）
        line = f"- [{p['date']}] {p['title']}"
        lines.append(line)
    if len(excluded_pairs) > 25:
        lines.append(f"…（另有 {len(excluded_pairs) - 25} 則歷史紀錄，請一併視為已納入）")
    return "\n    ".join(lines)


def build_system_prompt(excluded_pairs: list[dict]) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        today_iso=TODAY.isoformat(),
        today_zh=TODAY_STR,
        yesterday_iso=YESTERDAY.isoformat(),
        excluded_block=build_excluded_block(excluded_pairs),
    )


def build_candidates_text(candidates: list[dict]) -> str:
    """把 RSS 候選新聞包裝成乾淨的純文字清單，給 Claude 當 user message。"""
    if not candidates:
        return "（候選列表為空）"
    blocks = []
    for i, c in enumerate(candidates, 1):
        blocks.append(
            f"[{i}] source={c['source']} | published_iso={c['published_iso']}\n"
            f"    title  : {c['title']}\n"
            f"    summary: {c['summary'][:500]}\n"
            f"    url    : {c['url']}"
        )
    return "\n\n".join(blocks)


# ─── Claude API（純評分；不啟用 web_search） ──────────────────────────────


def parse_json_response(raw: str) -> list[dict] | None:
    """嘗試從 Claude 回應中解析 JSON，回傳 items 或 None。"""
    if not raw or not raw.strip():
        return None

    # 直接解析
    try:
        return json.loads(raw)["items"]
    except Exception:
        pass

    # 嘗試從 markdown code block 抽出
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if not part:
                continue
            try:
                return json.loads(part)["items"]
            except Exception:
                continue

    # 嘗試抽出第一個 { ... } 區塊
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group(0))["items"]
        except Exception:
            pass

    return None


def extract_final_text(message) -> str:
    """從 Claude API 回應的 content blocks 中抽出最終 text block。"""
    texts = []
    for block in message.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            txt = getattr(block, "text", "") or ""
            if txt.strip():
                texts.append(txt)
    return texts[-1].strip() if texts else ""


def call_claude(system_prompt: str, candidates: list[dict], max_retries: int = 3) -> list[dict]:
    """呼叫 Claude API（不啟用 web_search），把 RSS 候選列表丟給模型評分排序。"""
    if not candidates:
        print("  [INFO] RSS 候選為空，跳過 Claude 呼叫")
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    candidates_text = build_candidates_text(candidates)
    user_msg = (
        f"以下是從 RSS 蒐集的醫療 AI 候選新聞列表，共 {len(candidates)} 則"
        f"（{TODAY.isoformat()} + {YESTERDAY.isoformat()}）。\n"
        "請依系統指示挑選 5–10 則最重要者，評分並改寫為繁體中文，最後只回傳符合 schema 的 JSON。\n\n"
        f"{candidates_text}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  [API] 第 {attempt} 次嘗試（model={CLAUDE_MODEL}, 候選 {len(candidates)} 則, no web_search）...")
            message = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )

            print(f"  [API] stop_reason={message.stop_reason}")
            raw = extract_final_text(message)
            if not raw:
                print(f"  [WARN] 回應為空（attempt {attempt}）")
                time.sleep(8)
                continue

            print(f"  [API] 最終 text 長度：{len(raw)} 字元")

            items = parse_json_response(raw)
            if items is None:
                print(f"  [WARN] JSON 解析失敗（attempt {attempt}），原始回應前 300 字：{raw[:300]}")
                time.sleep(8)
                continue

            if len(items) == 0:
                print(f"  [INFO] 模型從 {len(candidates)} 則候選中挑出 0 則 → 視為今日無合格新聞")
                return []

            return items

        except Exception as e:
            err_str = str(e)
            print(f"  [ERROR] API 呼叫失敗（attempt {attempt}）：{e}")
            if "429" in err_str or "rate_limit" in err_str.lower():
                wait_s = 65
                print(f"  [BACKOFF] 偵測到 rate limit，等待 {wait_s} 秒讓 token bucket 回滿...")
                time.sleep(wait_s)
                continue

        if attempt < max_retries:
            time.sleep(15)

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
    print(f"[{TODAY_STR}] === 奇美 AI Everywhere Brief 自動產生（RSS + Claude 評分版） ===")
    print(f"  [TIME] Asia/Taipei now: {now_taipei.isoformat(timespec='seconds')}")
    print(f"  [TIME] TODAY iso: {TODAY.isoformat()}")

    # 1. 讀取過去 7 天的已納入清單（用於去重）
    print("-> 建立排除清單（過去 7 天已納入新聞）...")
    excluded_urls, excluded_titles, excluded_pairs = load_excluded_set(days=7)
    print(f"  排除清單：URL {len(excluded_urls)} 筆、標題 {len(excluded_titles)} 筆、條目 {len(excluded_pairs)} 則")

    # 2. 從 RSS 蒐集候選新聞（取代舊版的 web_search）
    rss_candidates = fetch_rss_candidates()

    # 3. 預過濾：把已在過去 7 天簡報出現過的稿件先剔除
    rss_candidates = filter_candidates_against_history(
        rss_candidates, excluded_urls, excluded_titles
    )

    # 4. 組系統 prompt 並呼叫 Claude（純評分翻譯，不啟用 web_search）
    print("-> 組系統 prompt 並呼叫 Claude API（純評分模式，不啟用 web_search）...")
    system_prompt = build_system_prompt(excluded_pairs)
    items = call_claude(system_prompt, rss_candidates, max_retries=3)
    print(f"  產生 {len(items)} 則精選新聞")

    # 5. 後處理：以 (source_url, normalized title) 再做一次防呆去重
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    cleaned_items: list[dict] = []
    for it in items:
        u = (it.get("source_url") or "").strip()
        t_norm = _normalize_title(it.get("title", ""))
        if u and (u in excluded_urls or u in seen_urls):
            print(f"  [DEDUP] 移除已重複 URL：{u}")
            continue
        if t_norm and (t_norm in excluded_titles or t_norm in seen_titles):
            print(f"  [DEDUP] 移除已重複標題：{it.get('title')}")
            continue
        seen_urls.add(u)
        if t_norm:
            seen_titles.add(t_norm)
        cleaned_items.append(it)
    if len(cleaned_items) != len(items):
        for i, it in enumerate(cleaned_items, 1):
            it["rank"] = i
    items = cleaned_items
    print(f"  防呆去重後剩 {len(items)} 則")

    # 6. 儲存今日 JSON（供歷史查詢與明天去重用）
    OUTPUT_DIR.mkdir(exist_ok=True)
    json_path = OUTPUT_DIR / f"{TODAY.isoformat()}.json"
    json_path.write_text(
        json.dumps({"date": TODAY_STR, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  JSON 備份：{json_path}")

    # 7. 載入所有可用天的資料（含今日）並嵌入單一 HTML
    print("-> 載入歷史資料並套用模板...")
    available_dates = get_available_dates()
    all_days_data = load_all_days_data(items)

    # 防止 </script> 注入
    all_days_json = json.dumps(all_days_data, ensure_ascii=False).replace("</script>", r"<\/script>")

    html = render_html(items, available_dates, all_days_json)

    output_path = OUTPUT_DIR / "index.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"  輸出：{output_path}")

    print(f"[{TODAY_STR}] === 完成 ===")


if __name__ == "__main__":
    main()
