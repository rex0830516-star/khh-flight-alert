# KHH 週末直航機票監控

條件：**週五 KHH 出發 → 同週週日回 KHH**、直航、目的地為日本／韓國／中國任一機場、來回 **< 7,500 TWD**，監控到 2027/12/31（最後一班週五出發）。符合就推播到 Telegram。

## 設定步驟（約 10 分鐘）

### 1. 申請 Travelpayouts API token（免費）
1. 到 https://www.travelpayouts.com 註冊
2. 加入 **Aviasales** 計畫
3. 在 Profile → API token 複製 token

### 2. 建立 Telegram 機器人
1. Telegram 搜尋 `@BotFather` → 傳 `/newbot` → 照指示取名 → 拿到 **Bot Token**
2. 對你的新機器人傳一句任意訊息（例如 `hi`）
3. 瀏覽器打開 `https://api.telegram.org/bot<你的BotToken>/getUpdates`，找到 `"chat":{"id":123456789` → 這串數字是 **Chat ID**

### 3. 放上 GitHub
1. 新建一個 **Public** repo（Public 的 Actions 分鐘數無上限；Private 每月只有 2,000 分鐘，請把排程改成每 6 小時）
2. 把這個資料夾所有檔案上傳（包含 `.github/workflows/flight-alert.yml`）
3. repo → Settings → Secrets and variables → Actions → New repository secret，新增三個：
   - `TP_TOKEN`
   - `TG_BOT_TOKEN`
   - `TG_CHAT_ID`
4. repo → Actions → 啟用 workflows → 點「KHH 週末機票監控」→ **Run workflow** 手動跑一次確認

之後每 2 小時自動掃描一次。

### 4. 開啟結果網頁（GitHub Pages）
1. repo → Settings → Pages
2. Source 選 **Deploy from a branch**，Branch 選 `main`、資料夾選 `/docs` → Save
3. 約 1 分鐘後網址會出現在同一頁：`https://<你的帳號>.github.io/<repo名稱>/`
4. 手機打開 → 加入主畫面，就像一個 App

網頁會列出**目前所有**符合條件的票（不只新的），可依國家篩選、依價格或日期排序，點卡片直接進訂票頁。每次掃描完自動更新。

## 調整參數
在 `.github/workflows/flight-alert.yml` 的 `env` 改：
- `PRICE_LIMIT`：價格門檻（預設 7500）
- `END_DATE`：監控結束日
- 排程：`cron: "7 */2 * * *"`（每 2 小時）

## 通知規則
- 同一組（目的地＋日期＋航空公司）只通知一次
- 之後若價格再降低，會再通知一次
- 紀錄存在 `sent.json`

## 注意
- Travelpayouts 的價格是**用戶搜尋過的快取資料**，可能延遲數小時，冷門日期可能沒有資料；收到通知後請點連結確認實際票價。
- 價格通常含稅，但不一定含托運行李（廉航尤其要注意）。
- 本機測試：`DRY_RUN=1 TP_TOKEN=你的token python flight_alert.py`（只印出不推播）
- 測試 Telegram：`TG_BOT_TOKEN=... TG_CHAT_ID=... TP_TOKEN=x python flight_alert.py --test-telegram`
