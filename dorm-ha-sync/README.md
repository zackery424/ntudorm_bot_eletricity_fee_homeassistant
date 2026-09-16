# dorm-ha-sync

把「太子學舍電力計費雲端平台」（`dormtopup.prince.com.tw`）的用電度數 / 電費 /
帳戶餘額，定時同步進 Home Assistant。

## 為什麼不是做成 Chrome 擴充功能？

一開始的想法是做 Chrome 擴充功能，但擴充功能只有在瀏覽器開著、且分頁有在
執行時才會動作，沒辦法在背景「即時」把資料送進 Home Assistant。這裡改成
一個獨立的 Python 背景程式，用排程（cron / systemd timer / Docker）每
30～60 分鐘執行一次，主動把資料推送進 HA 的 REST API，不受瀏覽器開關影響。

補充：實測發現來源網站本身的資料更新頻率是——用電度數「每小時」更新一次、
電費扣款與帳戶餘額「每天」（結算後）更新一次。所以排程抓取只要 30～60
分鐘一次就已經是「跟資料源一樣即時」了，抓更頻繁不會有更新的資料，只會
增加對學校系統的負擔，請不要調得太密集。

## 這個工具做的事

1. 用你的帳號密碼登入 `dormtopup.prince.com.tw`（水源舍區預設是
   `/User/D02`，如果是別的舍區，登入頁網址會不一樣，改 `.env` 裡的
   `DORM_LOGIN_PATH` 即可）。
2. 登入頁的驗證碼是一道簡單算式（例如 `10 x 4 = ?`、`13 + 22 = ?`），
   程式會自動用圖片辨識（OCR）算出答案並送出。因為 OCR 不會 100% 準，
   程式的策略是：**辨識結果只要格式看起來不像正確答案，就直接換一張新的
   驗證碼重試**，答錯了系統也只會說「驗證碼錯誤」再重試一次，不會鎖帳號
   等級的風險（一般網站頂多鎖比較久或要求等待，如果你發現有異常鎖定，
   請調小 `.env` 裡的 `MAX_LOGIN_ATTEMPTS` 並加大重試間隔）。
3. 登入後抓：
   - 房間用電資訊 → 今天每小時的用電度數（加總 = 今天累計用電度數）
   - 帳戶交易紀錄 → 最近一次結算的電費扣款金額、目前帳戶餘額
4. 把上面這些數字，透過 Home Assistant 的 REST API（長期存取權杖）寫進
   幾個 sensor。

## 會建立哪些 Home Assistant 感測器

| entity_id（預設前綴）              | 內容                          | 單位  |
|-----------------------------------|-------------------------------|-------|
| `sensor.dorm_power_today_kwh`     | 今日累計用電度數                | kWh   |
| `sensor.dorm_power_latest_hour_kwh` | 最近一小時的用電度數           | kWh   |
| `sensor.dorm_power_balance`       | 電費帳戶目前餘額                | TWD   |
| `sensor.dorm_power_last_charge`   | 最近一次結算的電費扣款金額       | TWD   |
| `sensor.dorm_power_last_sync`     | 這個程式最後一次成功同步的時間   | -     |

前綴可以在 `.env` 的 `ENTITY_PREFIX` 改掉。這些 sensor 是用 REST API
`POST /api/states/<entity_id>` 建立的「無設定檔」感測器，第一次同步成功後
就會出現在 HA 的「開發者工具 → 狀態」或直接在儀表板上用 entity id 加卡片，
歷史記錄會照常記錄在 Recorder 裡，可以直接拿來畫圖用。

## 事前準備

### 1. 在 Home Assistant 建立長期存取權杖

HA 左下角你的使用者頭像 → 最下面「長期存取權杖 (Long-Lived Access
Tokens)」→ 建立權杖，複製起來（只會顯示一次），填進 `.env` 的 `HA_TOKEN`。

### 2. 系統需要 tesseract-ocr

驗證碼辨識用到 `pytesseract`，這個套件本身只是個包裝，實際辨識需要系統裝有
`tesseract-ocr` 這個執行檔：

- Debian/Ubuntu：`sudo apt-get install tesseract-ocr`
- Alpine：`apk add tesseract-ocr`
- macOS：`brew install tesseract`

**如果你打算用 Home Assistant 的 AppDaemon / Pyscript 加載執行**：
這兩者通常跑在受限的容器環境裡，不一定能額外安裝系統套件（`apt`/`apk`）。
這是這類「圖形驗證碼 + 需要系統 OCR 執行檔」的整合的通病，不是這份程式碼
特有的限制。**因此更推薦用下面「部署方式」的 Docker 或 cron 方式**，
在一個你能完全控制套件安裝的環境跑這個程式，再用 REST API 跟 HA 對話——
兩邊完全解耦，HA 端不需要裝任何額外東西。

## 安裝

```bash
cd dorm-ha-sync
pip install -r requirements.txt   # 或用下面的 Docker 方式，就不需要這一步
cp .env.example .env
# 編輯 .env，填入帳號密碼、HA_URL、HA_TOKEN
```

先手動跑一次確認可以動：

```bash
python3 ha_sync.py
```

看到類似這樣的訊息代表成功：

```
... 登入成功（第 N 次嘗試）。
... 已更新 sensor.dorm_power_today_kwh = 1.36
... 已更新 sensor.dorm_power_latest_hour_kwh = 0.05
... 已更新 sensor.dorm_power_balance = 615.91
... 已更新 sensor.dorm_power_last_charge = 7.37
... 已更新 sensor.dorm_power_last_sync = 2026-09-16T05:32:10+00:00
... 同步完成：今日 1.36 kWh，最近一小時 0.05 kWh，餘額 615.91 元，最近扣款 7.37 元（2026-09-16）
```

如果卡在「嘗試 N 次後仍未能登入成功」，先確認帳號密碼是否正確（程式不會
因為驗證碼而動到帳密本身），如果帳密確定沒錯，代表這批驗證碼辨識率不夠，
可以把 `.env` 的 `MAX_LOGIN_ATTEMPTS` 調大（例如 20~30），或參考下面
「驗證碼辨識原理」自行調整 `dorm_captcha.py` 的參數。

## 部署方式（三選一）

### A. Docker（最推薦，最不用擔心環境相容性）

```bash
docker compose -f docker-compose.example.yml up -d --build
```

容器內建了 `tesseract-ocr`，並且會在背景每 30 分鐘自動跑一次
`ha_sync.py`（可以直接改 `Dockerfile` 最後一行的 `sleep 1800` 調整
間隔，單位是秒）。這個容器可以跟你的 Home Assistant 容器放在同一台機器
上（甚至同一個 docker-compose.yml 裡），也可以放在家裡任何一台常開的
機器/NAS/樹莓派上，只要能連到 HA 的網址就行。

### B. Linux 主機上用 systemd timer（適合 HA Container/Core、或另一台
常開的 Linux 機器，例如 NAS、樹莓派）

```bash
sudo mkdir -p /opt/dorm-ha-sync
sudo cp dorm_captcha.py dorm_client.py ha_sync.py .env /opt/dorm-ha-sync/
sudo cp dorm-ha-sync.service dorm-ha-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dorm-ha-sync.timer
```

`dorm-ha-sync.timer` 預設每 30 分鐘跑一次，可自行調整
`OnUnitActiveSec`。

### C. 一般 crontab

```bash
crontab -e
# 每 30 分鐘跑一次
*/30 * * * * cd /path/to/dorm-ha-sync && /usr/bin/python3 ha_sync.py >> sync.log 2>&1
```

### 關於 Home Assistant OS（HAOS）+ AppDaemon/Pyscript

如前面提到的，這兩個 add-on 通常沒辦法額外裝 `tesseract-ocr` 系統套件，
所以不建議把這支程式直接塞進 AppDaemon/Pyscript 裡跑。比較穩的作法是：
在同一個區網裡找一台「你能自由安裝套件」的機器（樹莓派、NAS、或跑 HAOS
的那台機器如果你有另外裝 Portainer/Docker 也可以），用上面的方式 A 或 B
跑這支程式，透過 HA 的 REST API 把資料寫進去——HA 那一端完全不需要改動，
資料自然就會出現在你的 HA 裡。

## Lovelace 卡片範例

```yaml
type: entities
title: 宿舍電費
entities:
  - entity: sensor.dorm_power_today_kwh
  - entity: sensor.dorm_power_latest_hour_kwh
  - entity: sensor.dorm_power_balance
  - entity: sensor.dorm_power_last_charge
  - entity: sensor.dorm_power_last_sync
```

也可以用 `sensor.dorm_power_today_kwh`（`state_class: total_increasing`）
搭配 HA 的「工具箱：公用事業計量表 (Utility Meter)」或直接用歷史圖表卡，
畫出每日/每週/每月用電趨勢。

## 檔案說明

- `dorm_captcha.py`：驗證碼 OCR（顏色遮罩去噪 + 多組參數 + tesseract +
  嚴格格式驗證，格式不對就放棄這張圖，不會硬送錯誤答案）
- `dorm_client.py`：登入 + 抓每日/每小時用電度數 + 帳戶交易紀錄的爬蟲邏輯
- `ha_sync.py`：主程式，串起 `dorm_client` 跟 Home Assistant REST API
- `requirements.txt` / `.env.example`：套件與環境變數
- `Dockerfile` / `docker-compose.example.yml`：Docker 部署
- `dorm-ha-sync.service` / `dorm-ha-sync.timer`：systemd 部署

## 驗證碼辨識原理（想微調的話看這裡）

驗證碼圖片裡文字（數字、`+`、`x`、`=`、`?`）都是同一種偏藍色調、比背景
明顯偏暗，背景則有大量細小雜訊點。做法：

1. 用顏色（藍色分量明顯高於紅/綠，且整體不會太亮）挑出「可能是文字」的
   像素，做成黑白遮罩。
2. 用連通元件（相鄰像素群）大小過濾掉太小的雜訊點（單獨的噪點通常只有
   1~3 個像素，文字筆畫至少有 4~8 個像素相連）。
3. 放大、丟給 `tesseract` 用「單行文字、限定字元集」模式辨識。
4. 因為單一組參數不一定每次都準，會用好幾組不同的過濾強度各跑一次，
   取第一個「整串結果完全符合『數字 運算符 數字 = ?』格式」的答案。
5. 如果所有參數組合都得不到符合格式的結果，就直接放棄這張圖、換新的
   驗證碼重試——不會用猜的格式亂送。

這個做法不追求「單張圖片一定辨識正確」，而是「送出去的答案格式看起來
合理」+「靠多次重試把整體成功率拉高」，實測（4 張真實樣本）大概每 2~4
張圖能猜對 1 張、其他會被系統當作「驗證碼錯誤」而重試，對一個排程每
30~60 分鐘才跑一次、每次只需要幾秒鐘重試的背景程式來說是可以接受的。
如果你想追求更高的單次命中率，可以：

- 收集更多樣本，調整 `dorm_captcha.py` 裡 `_VARIANTS` 的參數組合；
- 或改用逐字元切割 + 針對這個字型單獨訓練的分類器（工程量較大，這份
  程式碼先用「重試」換取簡單可靠）。

## 注意事項

- `.env` 裡有你的帳號密碼跟 HA 權杖，請不要傳到公開的地方。
- 這支程式只會做「讀取」（登入、查詢用電/帳務紀錄），不會幫你做任何
  儲值、遠端電源開關等操作。
- 如果學校網站改版（表單欄位名稱、頁面網址、表格結構變了），這支程式的
  解析邏輯可能會跟著失效，需要照網站新的結構調整 `dorm_client.py`。
