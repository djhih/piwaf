# PiWAF Observatory — 容器化部署（已實機跑通）

BunkerWeb 整包當 WAF，外加自己的 **parser → SQLite → Streamlit 儀表板**，把 ModSecurity
的判斷結果（觸發規則、anomaly score、攻擊類型、阻擋決策）變成自訂可視化。

## 架構與資料流

```
generator ─► bunkerweb (nginx + ModSecurity + OWASP CRS) ─► juice-shop
                  │
                  ├─ error.log（每條觸發規則 / 403 決策）┐
                  │                                      ├─► parser ─► SQLite ─► dashboard
                  └─ modsec_audit.log（JSON，所有請求中繼資料）┘
```

**為什麼要兩個 log（這是這個專案最關鍵的點）**
BunkerWeb / libmodsecurity-nginx 有兩個限制：

1. JSON audit log（`SecAuditEngine On`）裡的 `messages` 是**空的**，而且被擋的 403
   **不會進來** → 它只適合拿來抓「所有請求的中繼資料 + 正常流量 baseline」。
2. 每一條觸發規則的完整資訊（rule id、tags、severity、anomaly score、是否 403）只會
   寫進 **nginx error_log**。

所以 [parser.py](parser/parser.py) 同時 tail 兩個檔，用 `unique_id` 把它們**合併**：
乾淨通過的請求只在 audit、觸發但放行的兩邊都有、被擋 403 的只在 error_log。

## 專案結構

```
piwaf/
├── docker-compose.yml      # 5 個服務的編排
├── .env                    # 模式(WAF_MODE)、埠、anomaly 門檻
├── parser/                 # tail 兩個 log → 合併 → 寫 SQLite
│   ├── parser.py
│   └── Dockerfile          # python:3.12-slim（純標準函式庫）
├── dashboard/              # Streamlit 可視化，讀 SQLite(唯讀+WAL)
│   ├── app.py
│   ├── requirements.txt    # streamlit / pandas / streamlit-autorefresh
│   └── Dockerfile
├── generator/              # 測試流量情境（會自動等 WAF 上線）
│   ├── generate.py
│   ├── requirements.txt    # requests
│   └── Dockerfile
├── ftw/                    # go-ftw：現成的 OWASP CRS 攻擊測試集
│   ├── Dockerfile          # 建 go-ftw + 帶入 crs-tests
│   ├── config.yaml         # cloud 模式、打 bunkerweb:8080
│   └── crs-tests/          # vendored CRS v4.12.0 回歸測試（295 檔）
└── logs/                   # host bind：bunkerweb 的 log 寫這裡，parser 從這裡讀
    ├── modsec_audit.log    # JSON audit（真實檔，非 stderr symlink）
    └── error.log           # nginx error_log（含每條 ModSecurity 規則）
```

> SQLite (`piwaf.db`) 存在 Docker named volume `piwaf-data`，不在原始碼樹裡；
> BunkerWeb 自己的設定/資料庫在 named volume `bw-data`。

| 服務 | 角色 | 對外埠 |
|------|------|--------|
| bunkerweb / bw-scheduler | WAF（BunkerWeb 整包） | `8080`（本機 80 被佔） |
| juice-shop | 故意有漏洞的測試站 | `3001`（Direct Mode baseline） |
| parser | 合併兩個 log → SQLite（WAL） | — |
| dashboard | Streamlit 可視化 | `8501` |
| generator | 測試流量情境（手動跑，會自動等 WAF 上線） | — |

## 快速開始

> ⚠ 本機只有 docker-compose **v1**，且被使用者 `~/.local` 的新版 `requests` 弄壞，
> 所有指令要加 `PYTHONNOUSERSITE=1` 前綴。Pi 上若用 compose v2（`docker compose`）
> 就不需要這個前綴。

```bash
cd piwaf
cp .env.example .env                                 # 複製設定範本，再依環境調整 BIND_ADDR 等
PYTHONNOUSERSITE=1 docker-compose up -d --build      # 起 WAF + 測試站 + parser + dashboard
PYTHONNOUSERSITE=1 docker-compose run --rm generator # 送測試流量（會先等 WAF 上線再打）
# 瀏覽器打開  http://<這台的LAN-IP>:8501  看儀表板（見下方「區網綁定」）
```

### 區網綁定（只讓 LAN 連，不暴露到 VPN/外網）

對外埠透過 `.env` 的 `BIND_ADDR` 控制綁哪張網卡：

- `BIND_ADDR=`（空）→ 綁 `0.0.0.0`，**所有介面**都能連（含 VPN / 外網，較不安全）
- `BIND_ADDR=10.49.107.86:` → 只綁該 LAN IP（**結尾要有冒號**），其他介面/loopback 連不到

本機目前鎖在 `10.49.107.86`（`wlp0s20f3`），所以用 `http://10.49.107.86:8501`、
**不是** `localhost`。上 Raspberry Pi 時把它改成 Pi 的 LAN IP。

> 更嚴格的來源限制（只准某網段）可再配防火牆：
> `sudo ufw allow from 10.49.107.0/24 to any port 8080,8501,3001 proto tcp`

實測一輪結果：40 請求 / 16 被擋 / SQLi 10、XSS 3、LFI 3，Top rule 949110（anomaly 阻擋）。

## 送測試流量的兩種方式

**A. 自寫情境產生器**（有 `scenario` 標籤、講「情境→預期偵測」的故事）

```bash
PYTHONNOUSERSITE=1 docker-compose run --rm generator
```

**B. go-ftw — 現成的 OWASP CRS 官方攻擊測試集**（火力大、覆蓋廣）

打整套 CRS v4.12.0 回歸測試（約 7000 筆，cloud 模式只看回應碼，約 20 秒跑完）：

```bash
PYTHONNOUSERSITE=1 docker-compose run --rm ftw
# 只測某規則家族（例 SQLi 942、XSS 941、LFI 930）：
PYTHONNOUSERSITE=1 docker-compose run --rm ftw run -d /tests --config /etc/ftw/config.yaml -i 942
```

實測跑完 DB 多了 ~800 筆、413 被擋，攻擊類型一次涵蓋 SQLi/Protocol/PHPi/RCE/XSS/LFI、
anomaly 分數上看 50 —— 儀表板一下就豐富了。注意 go-ftw 的流量**不帶 `pscn` 標籤**，
所以 scenario 篩選會是 `unlabeled`，但攻擊類型/規則/分數/阻擋等圖表照常。

> CRS 測試集已 vendor 在 [ftw/crs-tests/](ftw/crs-tests/)（對齊 BunkerWeb 的 CRS 版本，
> 因本機 Docker build 網路會 reset github，故不在映像內 clone）。要換版本就更新這個目錄。

## 儀表板看得到什麼（http://<LAN-IP>:8501）

- KPI：總 request、實際阻擋(403)、分數≥門檻(本來會擋)、可疑/攻擊 比例
- 攻擊類型分布（SQLi / XSS / LFI / RCE …，由 CRS tag 推導）
- 阻擋 vs 放行
- Top 觸發規則（rule id）、Top 目標端點
- Anomaly score 分布、事件時間線
- Detection vs Blocking 模式比較表
- 側邊欄可依 **模式** 與 **情境(scenario)** 篩選；每 5 秒自動刷新

## 三種模式（對應計畫第七節）

- **Blocking Mode**：`.env` 設 `WAF_MODE=On` → 攻擊被擋（403）
- **Detection Mode**：`.env` 設 `WAF_MODE=DetectionOnly` → 不擋但記錄；儀表板看
  「分數≥門檻＝本來會擋」，底部有 Detection vs Blocking 比較表
- **Direct Mode**：流量直接打 `http://<LAN-IP>:3001`（繞過 WAF）建立 baseline

切換模式（compose v1 的 recreate 有 bug，用 down+up 走全新建立路徑）：

```bash
sed -i 's/^WAF_MODE=.*/WAF_MODE=DetectionOnly/' .env
PYTHONNOUSERSITE=1 docker-compose down
PYTHONNOUSERSITE=1 docker-compose up -d
PYTHONNOUSERSITE=1 docker-compose run --rm generator
```

parser 會把每筆事件標上當下的 `mode`，方便做模式比較。

## 實作重點（踩過的坑，留給未來的你）

- **log 目錄用 host bind**（[logs/](logs/)）取代 BunkerWeb 內建「全部 symlink 到 stderr」：
  `access.log` 仍 → stdout，但 `error.log` / `modsec_audit.log` 是**真實檔**，parser 才 tail 得到。
- **JSON audit 格式**靠 `CUSTOM_CONF_MODSEC_piwaf_audit` 環境變數注入
  `SecAuditLogFormat JSON`（不能再寫第二條 `SecAuditLog`，否則 libmodsecurity 會直接停用 audit）。
- **`USE_MODSECURITY_GLOBAL_CRS=no`**：改 per-server 載入，自訂 modsec 設定才會生效。
- **scenario 標籤**靠 generator 在 URL 帶 `pscn=<情境>`，讓它出現在 error_log 的
  request 行（被擋的攻擊不會進 audit，所以不能只靠 header）。

## 排錯

```bash
# 確認 audit JSON 有在寫
wc -l logs/modsec_audit.log logs/error.log
# 確認 error.log 有 ModSecurity 行（WAF 真的在檢查）
grep -c "ModSecurity:" logs/error.log
# parser / dashboard log
PYTHONNOUSERSITE=1 docker-compose logs -f parser
```

## 重置資料

```bash
PYTHONNOUSERSITE=1 docker-compose down
docker volume rm piwaf_piwaf-data      # 清掉 SQLite
: > logs/error.log; : > logs/modsec_audit.log
```
