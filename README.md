# 🧠 奇美 AI Everywhere Brief

奇美醫療體系每日醫療 AI 情報簡報自動產生系統。每天自動從多個醫療 AI 相關 RSS 來源抓取新聞，透過 Claude API 進行評鑑排名，並產生一份精美的 HTML 簡報。

## 專案結構

```
.
├── index.html              # 最新一期簡報（靜態 HTML，可直接瀏覽）
├── templates/
│   └── brief.html          # Jinja2 HTML 模板
├── scripts/
│   └── generate_brief.py   # 自動產生腳本
├── requirements.txt         # Python 套件依賴
└── README.md
```

## 本機預覽

### 方法一：直接開啟 HTML（無需 Python）

直接用瀏覽器開啟 `index.html`：

```bash
# macOS
open index.html

# Linux
xdg-open index.html

# Windows（PowerShell）
Start-Process index.html
```

### 方法二：執行自動產生腳本

1. 安裝 Python 套件：

```bash
pip install -r requirements.txt
```

2. 設定 Anthropic API 金鑰：

```bash
export ANTHROPIC_API_KEY="your-api-key-here"
```

3. 執行產生腳本：

```bash
python scripts/generate_brief.py
```

4. 開啟產生的 HTML：

```bash
open output/index.html
```

### 方法三：使用本機 HTTP 伺服器（推薦）

使用 Python 內建 HTTP 伺服器避免部分瀏覽器的本機檔案限制：

```bash
python -m http.server 8080
```

然後在瀏覽器開啟 [http://localhost:8080](http://localhost:8080)

## 版面特色

- **漸層 Header**：品牌識別明確，包含日期與今日統計 Ribbon
- **卡片式排版**：Top 3 三欄格線（桌機版），hover 有浮起動畫
- **視覺評分條**：漸層進度條搭配數字評分
- **標籤系統**：圓角藥丸式分類標籤
- **RWD 響應式**：在手機（＜480px）與平板（＜680px）均正常顯示

## GitHub Actions 自動化

本專案透過 GitHub Actions 每日自動執行 `generate_brief.py`，並將產生的 `index.html` 部署至 GitHub Pages。
