"""
奇美 AI Everywhere Brief — 自動產生器（Skill prompt 驅動版）
-----------------------------------------------------------
流程：
  1. 讀過去 7 天 JSON 建立去重排除清單
  2. 呼叫 Claude API（啟用 web_search server tool）
     → 模型依 chimei-healthcareai-daily-brief Skill 規範自行搜尋、驗證日期、評分排序
  3. 解析回傳 JSON、套用 brief.html 模板，輸出 output/index.html

與舊版的差異：
  - 不再使用 RSS（feedparser 已自 requirements.txt 移除）
  - 模型用 web_search + 內建 fetch 自己抓今日新聞並驗證發布日期
  - 評分採 Skill 的 4 維度（臨床相關性 / 產業影響 / 政策法規 / 技術突破）

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

# 英文日期（給搜尋查詢用）
EN_MONTH = ["January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"]
TODAY_EN = f"{EN_MONTH[TODAY.month - 1]} {TODAY.day} {TODAY.year}"
YESTERDAY_EN = f"{EN_MONTH[YESTERDAY.month - 1]} {YESTERDAY.day} {YESTERDAY.year}"

# 需要「刪除/不再出現」的日期（ISO 格式）
EXCLUDED_DATE_ISOS: set[str] = {
    "2026-04-15",
    "2026-04-18",
    "2026-04-19",
}

# Claude 模型與 Web Search Tool 設定
CLAUDE_MODEL = "claude-sonnet-4-6"
WEB_SEARCH_MAX_USES = 6   # 必須壓在 6 以下，否則單次呼叫累積 input tokens 會超過 30K/min org 限額
MAX_OUTPUT_TOKENS = 8000


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
    - 今日英文：{today_en}
    - 昨日英文：{yesterday_en}

    ## 第一步 — 搜尋新聞（使用 web_search 工具）

    使用 web_search 工具，依下列順序執行查詢；每組查詢完成後再進行下一組。
    所有查詢一律使用 `after:{yesterday_iso}` 過濾（即取「昨日含當日」之後發布的文章），確保候選池同時涵蓋今日（{today_iso}）與昨日（{yesterday_iso}）兩天的稿件。
    最終是否納入仍由第二步「納入條件」決定（優先收今日；不足 5 則才補昨日）。

    1. `healthcare AI clinical statnews.com OR nature.com OR nejm.org OR thelancet.com OR healthcareitnews.com OR jamanetwork.com after:{yesterday_iso}`
    2. `medical AI FDA approval hospital workflow after:{yesterday_iso}`
    3. `AI radiology pathology diagnosis breakthrough after:{yesterday_iso}`
    4. `醫療 AI 人工智慧 臨床 after:{yesterday_iso}`（台灣新聞）
    5. `site:healthcareitnews.com AI after:{yesterday_iso}`

    > **重要：搜尋次數上限為 5，請務必在 5 次內完成且不要進行驗證 fetch。** 模型須直接根據搜尋結果片段判斷發布日期、納入條件並產出 JSON；不得對個別文章再做 web_fetch。

    優先來源（不限於此）：上述查詢涵蓋重點來源，但不排除其他高品質來源。
    若搜尋過程中發現來自 WHO、NIH、MIT Technology Review、Wired Health、FierceBiotech、
    MedCity News、Becker's Health IT、Modern Healthcare 等可信媒體的重要新聞，同樣應予納入
    ——前提是發布日期等於 {today_iso}（合格今日新聞不足 5 則時，可依下方第二步補入 {yesterday_iso} 新聞）。

    ## 第二步 — 納入條件（優先收今日；不足 5 則時允許回退補昨日）

    - **發布日期優先等於 {today_iso}**（透過搜尋結果片段或檢視原文確認）。
      - 實際發布日期等於 {today_iso} → 直接納入候選。
      - 實際發布日期等於 {yesterday_iso} → **僅當今日合格新聞不足 5 則時**才補入，並在補滿 5 則後停止；今日合格新聞 ≥ 5 則時則一律排除昨日新聞。
      - 實際發布日期早於 {yesterday_iso} → **一律排除**，不允許補入更早新聞。
      - 無法確認發布日期 → **一律排除**。
    - 與醫療產業具有重要意義。
    - 排除：行銷稿、無實質內容的純意見文章、重複報導、{yesterday_iso} 之前的新聞。
    - 候選不足 5 則但已盡力搜尋（且昨日亦無新聞可補）時，照實輸出（可少於 5 則甚至為空），不得以無關內容或更早新聞湊數。
    - **author 欄位日期必須誠實反映該文實際發布日期**（今日就寫今日、昨日就寫昨日），不得改寫為今日。

    ## 第三步 — 去重（強制執行）

    以下「過去 7 天已納入」清單裡的 URL 與標題，**禁止再次納入今日簡報**，
    即使該則新聞重要性評分很高，也須排除。

    {excluded_block}

    ## 第四步 — 評分（0–10 分）

    | 標準 | 最高分 |
    |------|--------|
    | 臨床相關性（直接影響臨床工作） | +3 |
    | 產業影響力（對醫療產業的廣泛影響） | +3 |
    | 政策／法規影響 | +2 |
    | 技術突破程度 | +2 |

    依評分由高至低排序，輸出 5–10 則（候選不足 5 則時，先依第二步補入昨日新聞至 5 則為止；若補完仍不足則全數輸出）。
    評分前 3 名為「Top 3 精選」（僅在 HTML 完整日報中標示，LINE 訊息不另設 Top 3 區塊，依評分序依序列出即可）。

    ## 第五步 — 輸出格式（嚴格使用以下 JSON 結構，且只輸出 JSON）

    完成所有搜尋與評鑑後，最後一次回應**只輸出**下列 JSON，不要附加任何 markdown、說明或前後文：

    ```
    {{
      "items": [
        {{
          "rank": 1,
          "score": 9.5,
          "title": "繁體中文標題（可改寫使其更清晰）",
          "summary": "150–200字摘要，客觀說明新聞事件、背景與意義",
          "tags": ["標籤1", "標籤2", "標籤3"],
          "author": "第一作者姓名（媒體名稱｜YYYY-MM-DD）",
          "source_url": "https://原始網址"
        }}
      ]
    }}
    ```

    ## 強制規則

    - **author 欄位**：格式為「第一作者姓名（媒體名稱｜YYYY-MM-DD）」。第一作者無法辨識時填「未標示（媒體名稱｜YYYY-MM-DD）」。日期必須等於該文實際發布日期，不得改寫為今日。
    - **source_url 欄位**：必須是搜尋結果中實際出現過的原始連結。
    - **tags**：每則 2–4 個，繁體中文，不含 # 符號。
    - **summary**：150–200 字（允許略短），繁體中文，僅客觀描述新聞事實與意義。
    - 嚴禁在任何欄位輸出對奇美的建議、行動方針、策略建議、啟示、下一步等內容。
    - 嚴禁編造未實際搜尋到的新聞；若今日（{today_iso}）合格新聞不足 5 則，依第二步補入 {yesterday_iso} 新聞；若今日與昨日合計仍無任何合格新聞，回傳 {{"items": []}}。
    - 嚴禁補入早於 {yesterday_iso} 的新聞，亦不得使用訓練資料中的舊新聞充數。
    - 嚴禁在 JSON 中出現換行符號（必要時用全形標點分句）。
    - 最終回應必須是純 JSON，禁止任何 ```json``` 或解釋文字包裹。
""").strip()


def build_excluded_block(excluded_pairs: list[dict]) -> str:
    """把過去 7 天的標題清單格式化成 prompt 可讀的條列。"""
    if not excluded_pairs:
        return "（過去 7 天無歷史紀錄，本次無需去重）"
    lines = []
    for p in excluded_pairs[:25]:  # 上限 25 則（保留最近期最相關的，避免吃光 token 預算）
        line = f"- [{p['date']}] {p['title']}"
        # 不再夾 URL（後處理階段會再用 URL set 防呆去重，prompt 內只保留標題即可省 tokens）
        lines.append(line)
    if len(excluded_pairs) > 25:
        lines.append(f"…（另有 {len(excluded_pairs) - 25} 則歷史紀錄，請一併視為已納入）")
    return "\n    ".join(lines)


def build_system_prompt(excluded_pairs: list[dict]) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        today_iso=TODAY.isoformat(),
        today_zh=TODAY_STR,
        yesterday_iso=YESTERDAY.isoformat(),
        today_en=TODAY_EN,
        yesterday_en=YESTERDAY_EN,
        excluded_block=build_excluded_block(excluded_pairs),
    )


# ─── Claude API（含 web_search server tool） ──────────────────────────────


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
    """從 Claude API 回應的 content blocks 中抽出最終 text。
    Web search 會混入 server_tool_use / web_search_tool_result blocks，
    這些不是給程式吃的；我們只要最後的 text block。"""
    texts = []
    for block in message.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            txt = getattr(block, "text", "") or ""
            if txt.strip():
                texts.append(txt)
    return texts[-1].strip() if texts else ""


def call_claude(system_prompt: str, max_retries: int = 3) -> list[dict]:
    """呼叫 Claude API，啟用 web_search server tool 讓模型自行搜尋。
    僅收今日新聞 → 真的搜不到時回空清單也是合法結果。
    重試條件：API 失敗、JSON 解析失敗、最終 text 為空。
    『有效但 items 為空』在最後一次嘗試會被接受（記錄為今日無合格新聞）。"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_msg = (
        f"請依系統指示，搜尋並產出 {TODAY.isoformat()} 的醫療 AI 每日簡報。"
        f"只允許納入發布日期等於 {TODAY.isoformat()} 的新聞，"
        "完成所有搜尋後，最後一則訊息只回傳符合 schema 的 JSON。"
    )

    last_valid_empty = False  # 是否曾收到「JSON 有效但 items 空」的回應

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  [API] 第 {attempt} 次嘗試（model={CLAUDE_MODEL}, web_search up to {WEB_SEARCH_MAX_USES} uses）...")
            message = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system_prompt,
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": WEB_SEARCH_MAX_USES,
                    }
                ],
                messages=[{"role": "user", "content": user_msg}],
            )

            tool_use_count = sum(
                1 for b in message.content
                if getattr(b, "type", "") == "server_tool_use"
            )
            print(f"  [API] stop_reason={message.stop_reason}  web_search 呼叫 {tool_use_count} 次")

            raw = extract_final_text(message)
            if not raw:
                print(f"  [WARN] 最終 text block 為空（attempt {attempt}）")
                time.sleep(8)
                continue

            print(f"  [API] 最終 text 長度：{len(raw)} 字元")

            items = parse_json_response(raw)
            if items is None:
                print(f"  [WARN] JSON 解析失敗（attempt {attempt}），原始回應前 300 字：{raw[:300]}")
                time.sleep(8)
                continue

            if len(items) == 0:
                last_valid_empty = True
                # 不是最後一次 → 多試一次，可能只是搜尋運氣
                if attempt < max_retries:
                    print(f"  [WARN] 模型回傳空 items（attempt {attempt}）；再嘗試一次，確認今日確無新聞。")
                    time.sleep(8)
                    continue
                # 最後一次仍空 → 視為今日確實無合格新聞，正常返回
                print(f"  [INFO] 連續 {max_retries} 次嘗試後仍為空 → 判定今日（{TODAY.isoformat()}）無合格醫療 AI 新聞")
                return []

            return items

        except Exception as e:
            err_str = str(e)
            print(f"  [ERROR] API 呼叫失敗（attempt {attempt}）：{e}")
            # 429 rate limit → 必須等到 token bucket 回滿（per-minute 限額）
            if "429" in err_str or "rate_limit" in err_str.lower():
                wait_s = 65
                print(f"  [BACKOFF] 偵測到 rate limit，等待 {wait_s} 秒讓 token bucket 回滿...")
                time.sleep(wait_s)
                continue

        if attempt < max_retries:
            time.sleep(15)

    # 所有嘗試都因 API 失敗或解析失敗而失敗
    if last_valid_empty:
        print("  [INFO] 雖有 API 失敗但其中收過合法空回應 → 視為今日無合格新聞")
        return []
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
    print(f"[{TODAY_STR}] === 奇美 AI Everywhere Brief 自動產生（Skill prompt 驅動版） ===")
    print(f"  [TIME] Asia/Taipei now: {now_taipei.isoformat(timespec='seconds')}")
    print(f"  [TIME] TODAY iso: {TODAY.isoformat()}")

    # 1. 讀取過去 7 天的已納入清單（用於去重）
    print("→ 建立排除清單（過去 7 天已納入新聞）...")
    excluded_urls, excluded_titles, excluded_pairs = load_excluded_set(days=7)
    print(f"  排除清單：URL {len(excluded_urls)} 筆、標題 {len(excluded_titles)} 筆、條目 {len(excluded_pairs)} 則")

    # 2. 組系統 prompt 並呼叫 Claude（讓模型自己搜尋）
    print("→ 組系統 prompt 並呼叫 Claude API（web_search 工具）...")
    system_prompt = build_system_prompt(excluded_pairs)
    items = call_claude(system_prompt, max_retries=3)
    print(f"  產生 {len(items)} 則精選新聞")

    # 3. 後處理：以 (source_url, normalized title) 再做一次防呆去重
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
        # 重新編號 rank
        for i, it in enumerate(cleaned_items, 1):
            it["rank"] = i
    items = cleaned_items
    print(f"  防呆去重後剩 {len(items)} 則")

    # 4. 儲存今日 JSON（供歷史查詢與明天去重用）
    OUTPUT_DIR.mkdir(exist_ok=True)
    json_path = OUTPUT_DIR / f"{TODAY.isoformat()}.json"
    json_path.write_text(
        json.dumps({"date": TODAY_STR, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  JSON 備份：{json_path}")

    # 5. 載入所有可用天的資料（含今日）並嵌入單一 HTML
    print("→ 載入歷史資料並套用模板...")
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
