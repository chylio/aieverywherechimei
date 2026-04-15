name: Chimei Daily Brief Generator

on:
  schedule:
    - cron: '40 0 * * *'
  workflow_dispatch:
    inputs:
      audience:
        description: '目標讀者 (executive / staff / public)'
        required: false
        default: 'executive'

jobs:
  generate-brief:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Generate Daily Brief
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          AUDIENCE: ${{ github.event.inputs.audience || 'executive' }}
        run: python scripts/generate_brief.py

      - name: Commit files to main
        run: |
          TODAY=$(date '+%Y-%m-%d')

          # index.html：所有7天資料已內嵌，使用者只需開這一個檔案
          cp output/index.html index.html

          # JSON 存到根目錄：讓明後天的腳本可讀取歷史資料產生按鈕
          cp output/${TODAY}.json ${TODAY}.json

          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add index.html ${TODAY}.json
          git diff --staged --quiet || git commit -m "🏥 Auto-update daily brief ${TODAY}"
          git push
