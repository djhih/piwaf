# Raspberry Pi 3 部署指南

把 PiWAF Observatory 部署到 **Raspberry Pi 3 (B / B+)**。Pi 3 跟一般機器有三個關鍵差異，
照這份走才不會踩雷。

## ⚠ 先讀：Pi 3 的三個限制

1. **只有 1GB RAM** —— 最大瓶頸。主要吃 RAM 的是 BunkerWeb（+scheduler，~300–450MB）；
   後端已用輕量 whoami（~2MB）、儀表板用輕量 FastAPI（~32MB），整體已大幅瘦身。
   仍建議**開 swap**（步驟 2）當緩衝。
2. **必須用 64-bit (arm64) 作業系統** —— BunkerWeb 的容器映像只有 arm64，
   32-bit Raspberry Pi OS 跑不起來。
3. **用 Docker Compose v2（`docker compose`）** —— Pi 上用官方腳本裝，內建 v2 plugin，
   **不需要**開發機那個 `PYTHONNOUSERSITE=1` 前綴。

---

## 1. 燒錄 64-bit Raspberry Pi OS

用 Raspberry Pi Imager：

- OS 選 **Raspberry Pi OS (64-bit) — Lite**（無桌面版，省 RAM）
- 齒輪設定：開 SSH、設 hostname、Wi-Fi/帳號密碼
- 開機後 SSH 進去，確認是 64-bit：

```bash
uname -m        # 要顯示 aarch64（不是 armv7l）
```

> 若顯示 `armv7l`，表示燒成 32-bit，請重燒 64-bit 版。

## 2. 系統更新 + 開 swap（重要）

```bash
sudo apt update && sudo apt full-upgrade -y
```

**開 2GB swap（通用做法，任何發行版都可用）：**

```bash
swapon --show                 # 先看有沒有現成 swap；有 /swap.img 之類就只需擴大
sudo swapoff -a 2>/dev/null
sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
free -h                       # 確認 Swap 那行約 2.0Gi
grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

> **只有 Raspberry Pi OS (Raspbian)** 才有 `dphys-swapfile` 工具；那種系統可改用：
> `sudo dphys-swapfile swapoff` →
> 編輯 `/etc/dphys-swapfile` 設 `CONF_SWAPSIZE=2048`、`CONF_MAXSWAP=2048` →
> `sudo dphys-swapfile setup && sudo dphys-swapfile swapon`。
> Ubuntu Server / 其他系統沒有此工具，請用上面的 `/swapfile` 通用做法。

> 根目錄若是 **btrfs**，swapfile 需特別處理（`chattr +C`、不可壓縮）；ext4 直接照上面即可。

（可選）啟用 cgroup 記憶體控制，之後才能對容器設記憶體上限 —— 編輯 `/boot/firmware/cmdline.txt`
（舊版在 `/boot/cmdline.txt`），在**同一行結尾**加上：`cgroup_enable=memory cgroup_memory=1`，
然後 `sudo reboot`。

## 3. 安裝 Docker + Compose v2

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker            # 或登出再登入，讓群組生效

docker version
docker compose version   # 要看到 v2.x（注意是空格的 docker compose）
```

## 4. 取得專案 + 設定

```bash
git clone <你的-repo-url> piwaf-deploy
cd piwaf-deploy/piwaf        # 視 repo 結構，進到有 docker-compose.yml 的目錄
cp .env.example .env

hostname -I                  # 查 Pi 的 LAN IP，例如 192.168.1.50
```

編輯 `.env`：

```ini
BIND_ADDR=192.168.1.50:      # ← 改成上面查到的 Pi LAN IP，結尾保留冒號（鎖區網）
BW_HTTP_PORT=80              # Pi 上 80 沒被佔，可直接用 80
APP_DIRECT_PORT=3001
DASHBOARD_PORT=8501

# （建議）過載告警 webhook —— Pi 3 容易被流量打爆，設了就會主動通知（見第 9 步）
ALERT_WEBHOOK=
```

## 5. 啟動（首次 build 會慢，耐心等）

```bash
./setup.sh                 # 先備好 logs/ 權限（容器內 nginx uid 101 要能寫，否則 error.log permission denied）
docker compose up -d --build
```

- `dashboard` 只裝 fastapi/uvicorn（很輕，幾分鐘）；`parser` 純標準函式庫。
- `bunkerweb` / `webapp`(whoami) 是直接拉 arm64 映像。
- `generator` / `ftw` 是 `tools` profile，**不會在這步 build**，等你第一次 `run` 才建。

確認都起來：

```bash
docker compose ps          # bunkerweb 要 healthy
free -h                    # 看 RAM/Swap 用量
```

## 6. 送測試流量 + 看儀表板

```bash
docker compose run --rm generator     # 會自動等 WAF 上線再打
```

瀏覽器（同網段的電腦）開：**`http://192.168.1.50:8501`**（換成你的 Pi IP）。
頂端會有一條**負載監測狀態條**（綠/黃/紅），即時顯示這台 Pi 能不能負荷目前流量。

> go-ftw 全套約 7000 筆，Pi 3 上建議先縮範圍跑，省時間又省 RAM：
> `docker compose run --rm ftw run -d /tests --config /etc/ftw/config.yaml -i 942`
> 跑全套時可一邊看狀態條，觀察 Pi 3 被打到什麼程度會轉黃/紅。

## 7. 防火牆（只准區網來源）

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH                                   # ⚠ 先放行 SSH，別把自己鎖在外面
sudo ufw allow from 192.168.1.0/24 to any port 80       # 換成你的網段
sudo ufw allow from 192.168.1.0/24 to any port 8501
sudo ufw allow from 192.168.1.0/24 to any port 3001
sudo ufw enable
sudo ufw status
```

## 8. 開機自動啟動

`docker-compose.yml` 各服務已設 `restart: unless-stopped`，重開機會自己回來。
確認 Docker 本身開機啟動：

```bash
sudo systemctl enable docker
```

## 9. 負載監測 / 過載告警（Pi 3 特別有用）

Pi 3 資源少，被掃描器或攻擊流量打爆時容易卡死。儀表板內建監測：背景每 10 秒讀**主機**的
CPU 負載、可用記憶體、swap、目前 req/s，判定能不能負荷，頂端狀態條顯示綠/黃/紅。

- 不必額外裝東西、不另開容器（做在 dashboard 內，幾乎零開銷）。
- 容器內讀 `/proc` 拿到的是**主機**值，所以監測的就是 Pi 本身。

**想被主動通知**（人不在電腦前時）：在 `.env` 設 `ALERT_WEBHOOK`，轉 critical 會 POST 告警、
恢復再通知一次（有冷卻不洗版）。Discord 最簡單——伺服器設定 → 整合 → 建立 Webhook，把網址貼上：

```ini
ALERT_WEBHOOK=https://discord.com/api/webhooks/xxxx/yyyy
```

改完 `.env` 後讓 dashboard 重讀設定：

```bash
docker compose up -d dashboard       # compose v2 直接這樣即可
```

門檻可微調（Pi 3 預設值通常夠用）：`HEALTH_LOAD_CRIT`（load1÷核心數，預設 2.0）、
`HEALTH_MEM_CRIT`（可用記憶體 %，預設 7）、`HEALTH_SWAP_CRIT`（swap 使用 %，預設 80）。
外部 uptime 工具可輪詢 `GET http://<pi-ip>:8501/api/health`。

---

## 疑難排解（Pi 3 常見）

| 症狀 | 原因 / 解法 |
|------|------------|
| 容器一直重啟、`dmesg` 看到 **OOM / Killed** | RAM 不夠。確認 swap 有開（步驟 2）；負載監測狀態條通常會先轉紅可預警（第 9 步）；流量太猛就縮小 go-ftw 範圍 |
| `uname -m` 是 `armv7l`、映像 pull 失敗 `no matching manifest` | 燒成 32-bit 了，要重燒 **64-bit** OS |
| `docker compose` 找不到指令 | 用對版本：Pi 上是 `docker compose`（v2，空格），不是 `docker-compose` |
| build 較久 | 正常，Pi 3 本來就慢；dashboard 已改輕量(fastapi/uvicorn)，最慢的是首次 `run ftw` 的 Go 編譯 |
| 瀏覽器連不到儀表板 | 確認 `.env` 的 `BIND_ADDR` 是 Pi 的 LAN IP；防火牆有放行該網段；用 `http://<pi-ip>:8501` 不是 localhost |
| 儀表板**回 404**（有回應、不是連不到） | 前端 `dashboard/static/index.html` 沒進 image。確認版控有此檔：`git ls-files dashboard/static/` 要列出 `index.html`（早期被 `.gitignore` 的 `*.html` 擋掉漏 commit）。Pi 上 `git pull` 後**務必重建**：`docker compose up -d --build dashboard`——前端是 build 時 `COPY` 進 image，光 pull 不重建不生效。驗證：`docker compose exec dashboard ls /app/static/index.html` |
| `error.log` **permission denied** / parser 讀不到 log | 忘了跑 `./setup.sh`（容器內 nginx uid 101 無法寫你帳號的 logs/）。修復：`docker compose down` → `sudo chown -R "$(id -u):$(id -g)" logs` → `./setup.sh` → `docker compose up -d` |

### 後端用 whoami（預設，已最佳化）

後端預設就是極輕量的 `traefik/whoami`（~2MB），對任何路徑回 200。WAF 的偵測是在
**請求進來時**就判定（攻擊 payload 在 WAF 端就回 403），所以後端是什麼不影響「攻擊偵測」展示。
若想要更真實、有實際漏洞的測試站，可把 `webapp` 換回 Juice Shop / DVWA（記得 listen 3000、
保持 `REVERSE_PROXY_HOST=http://webapp:3000`，並確認映像有 arm64）。

### 把 build 移到電腦上做（進階，可選）

Pi 3 build 慢。可在 x86 電腦用 `docker buildx --platform linux/arm64` 建好映像、推到
registry 或 `docker save`/`load` 搬到 Pi，Pi 上就只 `docker compose up -d`（不加 `--build`）。
