---
name: scan-live-v1
description: >
  扫链策略 Live Bot v1.0.4 — 独立 Python 自动交易机器人（非 MCP 版）。
  v1.0: 防 rug 强化（LP严格验证/Bundle/Age/冷却表）/
  动态卖点（TP2 45%/动态Trailing 8-20%）/ 新增动量死亡与量能枯竭检测。
  v1.0.2: 低置信/年轻盘/重复信号只观察，实时卖压与短线砸盘硬拦截。
  v1.0.3: 专业扫链看板，区分 WATCH / EXEC / RUG_RISK，强化空状态与数据密度。
  v1.0.4: Signal Queue / Event Ledger 支持一键复制代币 CA。
  TraderSoul READ-ONLY 分析系统保留。

version: 1.0.4
validated: false
validation_date: 2026-06-20
validation_results: >
  v1.0: LP_LOCK_STRICT=True, BUNDLE_ATH 30→22%, AGE_HARD_MIN 240→360s,
  TOP10_HOLD 40→33%, APED_WALLET 10→6, DEV_HOLD 15→10%, RUG_RATE 30→20%,
  MIN_HOLDERS 25→35, MONITOR_SEC 5→3s. Exit: TP2 25→45%, TP1_SELL SCALP 60→50%,
  dynamic trailing 8-20%, momentum death detection, volume exhaustion detection,
  rejected token 10min cooldown cache.
  v1.0.1: safe-by-default dry-run mode, explicit ENABLE_LIVE_TRADING=1 gate,
  localhost dashboard binding by default, clearer env validation, atomic JSON writes.
  v1.0.2: executable-entry gate, critical safety-data fail-closed,
  recent sell-pressure checks, short-window crash rejection, repeated-signal downgrade.
  v1.0.3: professional dashboard redesign with KPI strip, signal queue,
  event ledger, position/trade rail, and explicit WATCH/EXEC/RUG labels.
  v1.0.4: token contract-address copy controls in Signal Queue and Event Ledger.

---

# 扫链策略 V1.0.4

> ⚠️ 本 Skill 描述真实交易机器人。使用前确保已理解风险，建议先以极小仓位测试。

---

## 🚀 AUTO-DEPLOY COMMAND

> **CLAUDE CODE INSTRUCTION — READ THIS FIRST**
>
> When the user asks to run, start, launch, or deploy the bot (`scan_live.py`),
> **DO NOT** attempt to write out the full Python code in a response — it will exceed the output token limit.
>
> Run ALL steps below as bash commands IN ORDER. Do not skip steps.
> Replace `__SKILL_FILE__` with the actual absolute path to THIS .md file.

---

**STEP 1** — Kill any existing bot process and free port 3241:

```bash
pkill -f scan_live.py 2>/dev/null; sleep 1
lsof -ti:3241 | xargs kill -9 2>/dev/null; sleep 1
echo "✅ Port 3241 cleared"
```

---

**STEP 2** — Extract `scan_live.py` from this skill file:

```bash
python3 -c "
import re, os
skill_path = '__SKILL_FILE__'
assert os.path.exists(skill_path), f'Skill file not found: {skill_path}'
with open(skill_path, 'r', encoding='utf-8') as f:
    content = f.read()
blocks = re.findall(r'\`\`\`python\n(.*?)\`\`\`', content, re.DOTALL)
assert blocks, 'No python blocks found in skill file!'
code = '\n\n'.join(blocks)
with open('scan_live.py', 'w', encoding='utf-8') as out:
    out.write(code)
lines = len(code.splitlines())
assert lines > 500, f'Extraction too short ({lines} lines) — skill path wrong?'
print(f'✅ scan_live.py extracted ({lines} lines)')
"
python3 -m py_compile scan_live.py
echo "✅ scan_live.py syntax OK"
```

---

**STEP 3 (optional)** — Reset TraderSoul only if you want a completely fresh start:

```bash
# rm -f trader_soul.json
echo "ℹ️  Keeping existing trader_soul.json (accumulated history preserved)"
```

---

**STEP 4** — Start the bot in background with full logging:

```bash
[ -f .env ] && set -a && source .env && set +a
[ -f ~/.dacs_env_profile ] && source ~/.dacs_env_profile
PYTHONUNBUFFERED=1 nohup python3 scan_live.py > bot.log 2>&1 &
BOT_PID=$!
echo "✅ Bot started (PID $BOT_PID)"
echo "ℹ️  ENABLE_LIVE_TRADING=${ENABLE_LIVE_TRADING:-0} (0 = dry run / observation mode)"
sleep 4
tail -20 bot.log
```

---

**STEP 5** — Verify dashboard is live:

```bash
sleep 3
curl -s http://localhost:3241/ | grep -q "OXScan" \
  && echo "✅ Dashboard OK → http://localhost:3241" \
  || echo "❌ Dashboard not responding — check: tail -50 bot.log"
```

---

## 架构总览

```
scan_live.py（单文件独立 Bot）
├── OKX REST API 客户端（数据 + 执行 + 安全检测）
├── scanner_loop()     ← 后台线程，每 10s 一轮
│   ├── memepump_token_list()   Step 1: Discovery+PreFilter
│   ├── hot_mode_check()        Step 1.5
│   └── detect_signal()         Step 2: C→A→Candles
│       └── threading.Thread(try_open_position)
│           └── deep_safety_check()
├── monitor_loop()     ← 后台线程，每 3s 轮询
│   └── check_position() 含 DynTrail/MomentumDead/VolExhaust/MaxHold/STUCK
├── HTTPServer (port 3241)
├── scan_positions.json
└── scan_trades.json
```

---

## 第一部分：环境配置

```python
import os, hmac, hashlib, base64, time, json, requests, threading, random
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict
from urllib.parse import urlencode

def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env, fill it in, then run: set -a && source .env && set +a"
        )
    return value

def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

# ── API ───────────────────────────────────────────────────────────────────────
API_BASE   = "https://web3.okx.com"
API_KEY    = require_env("OKX_API_KEY")
SECRET_KEY = require_env("OKX_SECRET_KEY")
PASSPHRASE = require_env("OKX_PASSPHRASE")

# ── Runtime safety defaults ───────────────────────────────────────────────────
# Default is observation mode. Set ENABLE_LIVE_TRADING=1 only after a wallet,
# API permissions, and risk limits have been deliberately reviewed.
ENABLE_LIVE_TRADING = env_flag("ENABLE_LIVE_TRADING", default=False)
DASHBOARD_HOST      = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"

# ── 仓位 ──────────────────────────────────────────────────────────────────────
SOL_PER_TRADE = {"SCALP": 0.01, "MINIMUM": 0.01, "STRONG": 0.01}
MAX_SOL        = 0.25
MAX_POSITIONS  = 5
SLIPPAGE_BUY   = {"SCALP": 8, "MINIMUM": 10, "STRONG": 15}
SLIPPAGE_SELL  = 50
SOL_GAS        = 0.05
COST_PER_LEG   = 0.003

# ── 止盈 ──────────────────────────────────────────────────────────────────────
TP1_PCT  = 0.15
TP2_PCT  = 0.45    # v1.0: was 0.25
TP1_SELL = {"SCALP": 0.50, "hot": 0.40, "quiet": 0.35}
TP2_SELL = {"SCALP": 1.00, "hot": 1.00, "quiet": 0.80}

# v1.0: 动态 Trailing Stop
TRAILING_DROP = 0.08
TRAIL_STEPS   = [
    (80, 0.20),
    (50, 0.15),
    (30, 0.12),
    ( 0, 0.08),
]

# ── 止损 ──────────────────────────────────────────────────────────────────────
S1_PCT   = {"SCALP": -0.15, "hot": -0.20, "quiet": -0.25}
HE1_PCT  = -0.35

# ── 时间止损 ──────────────────────────────────────────────────────────────────
S3_MIN       = {"SCALP": 5, "hot": 8, "quiet": 15}
MAX_HOLD_MIN = 30

# ── Session 风控 ──────────────────────────────────────────────────────────────
MAX_CONSEC_LOSS  = 3
PAUSE_CONSEC_SEC = 300
PAUSE_LOSS_SOL   = 0.05
STOP_LOSS_SOL    = 0.10

# ── 扫描 ──────────────────────────────────────────────────────────────────────
LOOP_SEC     = 10
MONITOR_SEC  = 3       # v1.0: was 5
CHAIN_INDEX  = "501"
SOL_ADDR     = "11111111111111111111111111111111"

# ── 基础过滤 ──────────────────────────────────────────────────────────────────
AGE_HARD_MIN = 360    # v1.0: was 240
AGE_SOFT_MIN = 300
AGE_MAX      = 10_800
MC_CAP       = 400_000
MC_MIN       = 3_000
LIQ_MIN      = 6_000
BS_MIN       = 1.5
DUMP_FLOOR   = -40

# ── 信号阈值 ──────────────────────────────────────────────────────────────────
SIG_A_THRESHOLD     = 2.0
SIG_A_FLOOR_TXS_MIN = 30
HOT_MODE_RATIO      = 0.40

# ── v1.0 安全检测阈值 ─────────────────────────────────────────────────────────
VOLMC_MIN_RATIO    = 0.05
MIN_HOLDERS        = 35     # v1.0: was 25
DEV_SELL_DROP_PCT  = 60
DEV_SELL_VOL_MULT  = 10
BUNDLE_ATH_PCT_MAX = 22     # v1.0: was 30
RUG_RATE_MAX       = 0.20   # v1.0: was 0.30
DEV_HOLD_DEEP_MAX  = 0.10   # v1.0: was 0.15

# ── memepump-token-list 过滤阈值 ──────────────────────────────────────────────
TOP10_HOLD_MAX     = 33     # v1.0: was 40
INSIDERS_MAX       = 15     # v1.0: was 20
SNIPERS_MAX        = 15     # v1.0: was 20
FRESH_WALLET_MAX   = 35
BOT_TRADERS_MAX    = 100
APED_WALLET_MAX    = 6      # v1.0: was 10
WASH_PRICE_CHG_MIN = 0.01
BOND_NEAR_PCT      = 0.80

# ── LP 锁定检测 ───────────────────────────────────────────────────────────────
LP_LOCK_MIN_PCT   = 0.80
LP_LOCK_MIN_HOURS = 0
LP_LOCK_STRICT    = True    # v1.0: was False

# ── v1.0 动量死亡检测 ─────────────────────────────────────────────────────────
MOMENTUM_DOWN_CANDLES  = 2
MOMENTUM_MIN_ELAPSED   = 3.0
MOMENTUM_FROM_PEAK_MIN = 0.05

# ── v1.0 量能枯竭 ─────────────────────────────────────────────────────────────
VOL_EXHAUST_MIN_PROFIT = 0.08
VOL_EXHAUST_RATIO      = 0.15

# ── v1.0 rejected 冷却 ───────────────────────────────────────────────────────
REJECT_SAFETY_COOLDOWN = 600

# ── v1.0.2 executable-entry gates ────────────────────────────────────────────
EXEC_MIN_CONFIDENCE       = 60
YOUNG_EXEC_MIN_AGE        = 15 * 60
YOUNG_EXEC_MIN_CONFIDENCE = 70
NEAR_MIGRATION_MIN_CONF   = 70
REPEAT_SIGNAL_COOLDOWN    = 10 * 60

# ── v1.0.2 short-window rug pressure checks ─────────────────────────────────
RECENT_CRASH_DROP_PCT       = 35
LIVE_CANDLE_DUMP_PCT        = 25
SELL_PRESSURE_LOOKBACK      = 30
SELL_PRESSURE_RATIO_MAX     = 2.2
SELL_PRESSURE_CONSEC_MAX    = 6
SELL_PRESSURE_TOP_SHARE_MAX = 0.35

# ── 协议支持 ──────────────────────────────────────────────────────────────────
PROTOCOL_PUMPFUN    = "120596"
PROTOCOL_LETSBONK   = "136266"
PROTOCOL_BELIEVE    = "134788"
DISCOVERY_PROTOCOLS = [PROTOCOL_PUMPFUN, PROTOCOL_LETSBONK, PROTOCOL_BELIEVE]

# ── NEW stage 发现 ────────────────────────────────────────────────────────────
MC_MIN_NEW  = 10_000
MC_MAX_NEW  = 80_000
AGE_MAX_NEW = 1_800
```

---

## 第二部分：OKX REST API 客户端

```python
def _sign(timestamp: str, method: str, path: str, body: str = "") -> dict:
    msg = timestamp + method + path + body
    sig = base64.b64encode(
        hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY":        API_KEY,
        "OK-ACCESS-SIGN":       sig,
        "OK-ACCESS-TIMESTAMP":  timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type":         "application/json",
    }

def _get(path: str, params: dict | None = None) -> dict:
    params = params or {}
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    qs  = ("?" + urlencode(params)) if params else ""
    hdrs = _sign(ts, "GET", path + qs)
    r = requests.get(API_BASE + path + qs, headers=hdrs, timeout=10)
    r.raise_for_status()
    return r.json()

def _post(path: str, payload: dict) -> dict:
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body = json.dumps(payload)
    hdrs = _sign(ts, "POST", path, body)
    r = requests.post(API_BASE + path, headers=hdrs, data=body, timeout=10)
    r.raise_for_status()
    return r.json()

def token_ranking(sort_by: int) -> list:
    r = _get("/api/v6/dex/market/token/toplist", {
        "chainIndex": CHAIN_INDEX, "sortBy": sort_by, "timeFrame": "1", "limit": "50"
    })
    return r.get("data", [])

def memepump_token_list(
    stage: str = "MIGRATED",
    max_mc: float = MC_CAP,
    min_liq: float = LIQ_MIN,
    min_holders: int = MIN_HOLDERS,
    max_bundlers_pct: float = BUNDLE_ATH_PCT_MAX,
    max_dev_hold_pct: float = DEV_HOLD_DEEP_MAX * 100,
    max_top10_pct: float = TOP10_HOLD_MAX,
    max_insiders_pct: float = INSIDERS_MAX,
    max_snipers_pct: float = SNIPERS_MAX,
    max_fresh_pct: float = FRESH_WALLET_MAX,
    limit: int = 50,
    protocol_ids: list = None,
) -> list:
    params = {
        "chainIndex":              CHAIN_INDEX,
        "stage":                   stage,
        "maxMarketCapUsd":         str(int(max_mc)),
        "minHolders":              str(min_holders),
        "maxBundlersPercent":      str(max_bundlers_pct),
        "maxDevHoldingsPercent":   str(max_dev_hold_pct),
        "maxTop10HoldingsPercent": str(max_top10_pct),
        "maxInsidersPercent":      str(max_insiders_pct),
        "maxSnipersPercent":       str(max_snipers_pct),
        "maxFreshWalletsPercent":  str(max_fresh_pct),
        "limit":                   str(limit),
    }
    if protocol_ids:
        params["protocolIdList"] = ",".join(protocol_ids)
    r = _get("/api/v6/dex/market/memepump/tokenList", params)
    return r.get("data", [])

def memepump_token_details(token_address: str, wallet: str = "") -> dict:
    params = {"chainIndex": CHAIN_INDEX, "tokenContractAddress": token_address}
    if wallet:
        params["walletAddress"] = wallet
    r = _get("/api/v6/dex/market/memepump/tokenDetails", params)
    data = r.get("data", [{}])
    return data[0] if data else {}

_logo_cache: dict = {}

def fetch_token_logo(addr: str) -> str:
    if addr in _logo_cache:
        return _logo_cache[addr] or ""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
            timeout=5, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs") or []
            url = pairs[0].get("info", {}).get("imageUrl", "") if pairs else ""
            _logo_cache[addr] = url or None
            return url
    except Exception:
        _logo_cache[addr] = None
    return ""

def memepump_aped_wallet(token_address: str) -> list:
    r = _get("/api/v6/dex/market/memepump/apedWallet", {
        "chainIndex": CHAIN_INDEX, "tokenContractAddress": token_address
    })
    return r.get("data", [])

def memepump_similar_token(token_address: str) -> list:
    r = _get("/api/v6/dex/market/memepump/similarToken", {
        "chainIndex": CHAIN_INDEX, "tokenContractAddress": token_address
    })
    return r.get("data", [])

def price_info(token_address: str) -> dict:
    r = _post("/api/v6/dex/market/price-info", {
        "chainIndex": CHAIN_INDEX, "tokenContractAddress": token_address
    })
    data = r.get("data", [{}])
    return data[0] if data else {}

def candlesticks(token_address: str, bar: str = "1m", limit: int = 20) -> list:
    r = _get("/api/v6/dex/market/candles", {
        "chainIndex": CHAIN_INDEX, "tokenContractAddress": token_address,
        "bar": bar, "limit": str(limit)
    })
    return r.get("data", [])

def trades(token_address: str, limit: int = 200) -> list:
    r = _get("/api/v6/dex/market/trades", {
        "chainIndex": CHAIN_INDEX, "tokenContractAddress": token_address,
        "limit": str(limit)
    })
    return r.get("data", [])

def token_dev_info(token_address: str) -> dict:
    r = _get("/api/v6/dex/market/memepump/tokenDevInfo", {
        "chainIndex": CHAIN_INDEX, "tokenContractAddress": token_address
    })
    data = r.get("data", [{}])
    return data[0] if data else {}

def token_bundle_info(token_address: str) -> dict:
    r = _get("/api/v6/dex/market/memepump/tokenBundleInfo", {
        "chainIndex": CHAIN_INDEX, "tokenContractAddress": token_address
    })
    data = r.get("data", [{}])
    return data[0] if data else {}

def get_quote(from_addr: str, to_addr: str, amount: str, slippage: int) -> dict:
    r = _get("/api/v6/dex/aggregator/quote", {
        "chainIndex": CHAIN_INDEX,
        "fromTokenAddress": from_addr, "toTokenAddress": to_addr,
        "amount": amount, "slippage": str(slippage / 100)
    })
    return r.get("data", [{}])[0]

def swap_instruction(from_addr: str, to_addr: str, amount: str,
                     slippage: int, user_wallet: str) -> dict:
    r = _get("/api/v6/dex/aggregator/swap-instruction", {
        "chainIndex": CHAIN_INDEX,
        "fromTokenAddress": from_addr, "toTokenAddress": to_addr,
        "amount": amount, "slippage": str(slippage / 100),
        "userWalletAddress": user_wallet,
    })
    return r.get("data", {})

def broadcast(signed_tx: str) -> str:
    r = _post("/api/v6/dex/pre-transaction/broadcast-transaction", {
        "chainIndex": CHAIN_INDEX, "signedTx": signed_tx
    })
    return r.get("data", {}).get("orderId", "")

def order_status(order_id: str) -> str:
    for _ in range(20):
        time.sleep(3)
        r = _get("/api/v6/dex/post-transaction/orders", {
            "chainIndex": CHAIN_INDEX, "orderId": order_id
        })
        status = r.get("data", [{}])[0].get("txStatus", "PENDING")
        if status in ("SUCCESS", "FAILED"):
            return status
    return "TIMEOUT"

def portfolio_token_pnl(token_address: str) -> dict:
    r = _get("/api/v6/dex/market/portfolio/token/latest-pnl", {
        "chainIndex": CHAIN_INDEX,
        "tokenContractAddress": token_address,
        "walletAddress": WALLET_ADDRESS,
    })
    data = r.get("data", [{}])
    return data[0] if data else {}
```

---

## 第三部分：Solana 签名

```python
import base58
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

WALLET_PRIVATE_KEY = require_env("WALLET_PRIVATE_KEY")

def get_keypair() -> Keypair:
    try:
        raw = base58.b58decode(WALLET_PRIVATE_KEY)
    except Exception as e:
        raise RuntimeError("Invalid WALLET_PRIVATE_KEY: expected a Base58-encoded Solana private key") from e
    if len(raw) != 64:
        raise RuntimeError(f"Invalid WALLET_PRIVATE_KEY length: decoded {len(raw)} bytes, expected 64")
    return Keypair.from_bytes(raw)

def sign_transaction(tx_data: str) -> str:
    kp  = get_keypair()
    raw = base64.b64decode(tx_data)
    tx  = VersionedTransaction.from_bytes(raw)
    tx.sign([kp])
    return base64.b64encode(bytes(tx)).decode()

WALLET_ADDRESS = str(get_keypair().pubkey())
```

---

## 第四部分：全局状态

```python
state_lock = threading.Lock()
pos_lock   = threading.Lock()

positions = {}
state = {
    "cycle": 0, "hot": False, "status": "启动中…",
    "feed": [], "feed_seq": 0,
    "signals": [],
    "positions": {},
    "trades": [],
    "stats": {
        "cycles": 0, "buys": 0, "sells": 0, "wins": 0, "losses": 0,
        "net_sol": 0.0, "session_start": time.strftime("%H:%M:%S"),
    },
    "session": {
        "paused_until": None,
        "consecutive_losses": 0,
        "daily_loss_sol": 0.0,
        "stopped": False,
        "cycle_sig_a_outcomes": [],
        "hot_mode": False,
    }
}
MAX_FEED = 600

POSITIONS_FILE = "scan_positions.json"
TRADES_FILE    = "scan_trades.json"

def atomic_json_dump(path: str, payload, **kwargs):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, **kwargs)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def push_feed(row: dict):
    with state_lock:
        state["feed_seq"] += 1
        row["seq"] = state["feed_seq"]
        state["feed"].insert(0, row)
        if len(state["feed"]) > MAX_FEED:
            state["feed"] = state["feed"][:MAX_FEED]

def sync_positions():
    with pos_lock: snap = dict(positions)
    with state_lock: state["positions"] = snap

def save_positions():
    with pos_lock:
        atomic_json_dump(POSITIONS_FILE, positions, ensure_ascii=False)

def save_trades():
    with state_lock:
        atomic_json_dump(TRADES_FILE, state["trades"], ensure_ascii=False)

def load_on_startup():
    global positions
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
        sync_positions()
        print(f"  Restored {len(positions)} positions from disk")
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            with state_lock:
                state["trades"] = json.load(f)
```

---

## 第四点五部分：TraderSoul — 进化型交易人格系统

```python
SOUL_FILE = "trader_soul.json"

DEGEN_NAMES = [
    "ChadAlpha", "RugSurvivor", "DiamondPaws", "ApexApe",
    "GigaBrain", "SolSavant", "DegenLord", "MoonMathis",
    "ChaosPilot", "ZeroToHero", "BasedSatoshi", "BullishGhost",
]

STAGE_THRESHOLDS = [
    (100, 1.0,  "Legend"),
    (50,  0.5,  "Veteran"),
    (20,  0.0,  "Seasoned"),
    (5,   None, "Apprentice"),
    (0,   None, "Novice"),
]

def _default_soul() -> dict:
    return {
        "name":            random.choice(DEGEN_NAMES),
        "stage":           "Novice",
        "trades_seen":     0,
        "wins":            0,
        "losses":          0,
        "total_pnl_sol":   0.0,
        "signals_seen":    0,
        "tier_stats":      {},
        "hour_stats":      {},
        "personal_limits": {
            "bundle_ath_pct_warn": 35,
            "min_confidence_trust": EXEC_MIN_CONFIDENCE,
        },
        "win_philosophy":  "I haven't found my edge yet. Every trade is a lesson.",
        "risk_philosophy": "The market owes me nothing. Protect the bag first.",
        "current_vibe":    "neutral",
        "reflections":     [],
        "evolution_log":   [],
        "trade_outcomes":  [],
        "periodic_reviews": [],
    }

soul = {}

def load_soul():
    global soul
    if os.path.exists(SOUL_FILE):
        try:
            with open(SOUL_FILE) as f:
                loaded = json.load(f)
            soul.update(loaded)
            stage  = soul.get("stage", "Novice")
            trades = soul.get("trades_seen", 0)
            pnl    = soul.get("total_pnl_sol", 0)
            print(f"  🧠 [{soul.get('name')}] {stage} — {trades} trades | {pnl:+.4f} SOL lifetime")
        except Exception as e:
            print(f"  ⚠️  Soul load error: {e} — starting fresh")
            soul.update(_default_soul())
    else:
        soul.update(_default_soul())
        _save_soul()
        print(f"  🧠 TraderSoul born: [{soul['name']}] — a fresh degen enters the arena")

def _save_soul():
    try:
        atomic_json_dump(SOUL_FILE, soul, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _add_reflection(text: str):
    entry = {"t": time.strftime("%H:%M:%S"), "msg": text}
    soul.setdefault("reflections", []).insert(0, entry)
    soul["reflections"] = soul["reflections"][:10]
    push_feed({"sym_note": True, "msg": f"🧠 {soul.get('name','?')}: {text}", "t": time.strftime("%H:%M:%S")})

def reflect_on_signal(sym: str, tier: str, confidence: float):
    tier_data = soul.get("tier_stats", {}).get(tier)
    min_conf  = soul.get("personal_limits", {}).get("min_confidence_trust", 50)

    if tier_data and tier_data.get("n", 0) >= 3:
        rate = tier_data["rate"]
        if rate >= 0.65:
            thought = f"{sym} — {tier} signal. My {tier} win rate is {rate*100:.0f}%. I like this setup."
        elif rate <= 0.35:
            thought = f"{sym} — {tier} signal but I've been wrong on this tier {(1-rate)*100:.0f}% of the time. Watching carefully."
        else:
            thought = f"{sym} — {tier} signal. Mixed results on this tier. Staying disciplined."
    elif confidence > 0 and confidence < min_conf:
        thought = f"{sym} — {tier} signal but confidence {confidence:.0f} is below my trust threshold. Low conviction."
    else:
        thought = f"{sym} — {tier} signal. No strong personal history yet. Going by the rules."

    soul["signals_seen"] = soul.get("signals_seen", 0) + 1
    _add_reflection(thought)

    if soul["signals_seen"] % 5 == 0:
        _evolve_philosophy()

    _save_soul()

def reflect_on_entry(sym: str, tier: str, sol_in: float, confidence: float):
    hour      = int(time.strftime("%H"))
    hour_key  = str(hour)
    hour_data = soul.get("hour_stats", {}).get(hour_key, {})

    if hour_data.get("n", 0) >= 3:
        rate = hour_data["rate"]
        if rate >= 0.60:
            thought = f"Bought {sym} ({sol_in:.3f} SOL). {hour:02d}:xx UTC has treated me well ({rate*100:.0f}% win). I'm feeling it."
        elif rate <= 0.30:
            thought = f"Bought {sym} ({sol_in:.3f} SOL). {hour:02d}:xx UTC has burned me before ({rate*100:.0f}% win). Tight stops, no bagholding."
        else:
            thought = f"Bought {sym} ({sol_in:.3f} SOL). Neutral hour for me. Let the chart decide."
    else:
        thought = f"Entered {sym} at {sol_in:.3f} SOL. Confidence {confidence:.0f}. Trust the process."

    _add_reflection(thought)
    _save_soul()

def reflect_on_exit(sym: str, tier: str, pnl_sol: float, reason: str, hold_min: float):
    is_win  = pnl_sol > 0
    is_loss = pnl_sol < 0
    hour    = int(time.strftime("%H"))

    soul["trades_seen"]   = soul.get("trades_seen", 0) + 1
    soul["total_pnl_sol"] = round(soul.get("total_pnl_sol", 0) + pnl_sol, 6)
    if is_win:
        soul["wins"]   = soul.get("wins", 0) + 1
    elif is_loss:
        soul["losses"] = soul.get("losses", 0) + 1

    ts = soul.setdefault("tier_stats", {})
    t  = ts.setdefault(tier, {"wins": 0, "losses": 0, "n": 0, "rate": 0.5})
    if is_win:    t["wins"]   += 1
    elif is_loss: t["losses"] += 1
    t["n"]    = t["wins"] + t["losses"]
    t["rate"] = round(t["wins"] / t["n"], 3) if t["n"] > 0 else 0.5

    hs = soul.setdefault("hour_stats", {})
    h  = hs.setdefault(str(hour), {"wins": 0, "losses": 0, "n": 0, "rate": 0.5})
    if is_win:    h["wins"]   += 1
    elif is_loss: h["losses"] += 1
    h["n"]    = h["wins"] + h["losses"]
    h["rate"] = round(h["wins"] / h["n"], 3) if h["n"] > 0 else 0.5

    soul.setdefault("trade_outcomes", []).insert(0, {
        "sym": sym, "tier": tier, "pnl": round(pnl_sol, 6),
        "reason": reason, "hold_min": round(hold_min, 1),
        "t": time.strftime("%H:%M:%S"), "win": is_win,
    })
    soul["trade_outcomes"] = soul["trade_outcomes"][:20]

    if is_win:
        if pnl_sol >= 0.05:
            thought = f"{sym} +{pnl_sol:.4f} SOL via {reason}. That's what {tier} looks like when it works. Trust the setup."
        elif pnl_sol >= 0.01:
            thought = f"{sym} +{pnl_sol:.4f} SOL. Small win but disciplined exit on {reason}. Adds up."
        else:
            thought = f"{sym} barely green +{pnl_sol:.4f} SOL. Near miss. Lucky. Don't count on luck."
    else:
        if "LP" in reason.upper() or "RUG" in reason.upper():
            thought = f"{sym} LP rug. {pnl_sol:.4f} SOL. No check stops a fast rug. Size is my only real protection."
        elif "HE1" in reason:
            thought = f"{sym} hard stop -35%. {pnl_sol:.4f} SOL. I stayed too long. Respect the hard stop."
        elif "S1" in reason or "STOP" in reason.upper():
            thought = f"{sym} stopped out. {pnl_sol:.4f} SOL. The signal was wrong. Not every setup works — move on."
        elif "MOMENTUM" in reason.upper():
            thought = f"{sym} momentum died after {hold_min:.1f}min. {pnl_sol:.4f} SOL. Better out early than bagholding."
        elif "VOL_EXHAUST" in reason.upper():
            thought = f"{sym} volume dried up. {pnl_sol:.4f} SOL. Smart exit — buyers gone, no reason to stay."
        elif "TIME" in reason.upper() or "S3" in reason or "MAX" in reason.upper():
            thought = f"{sym} timed out after {hold_min:.1f}min. {pnl_sol:.4f} SOL. Dead money. Better deployed elsewhere."
        else:
            thought = f"{sym} {reason} exit. {pnl_sol:.4f} SOL. Noted. The market teaches what books can't."

    _add_reflection(thought)

    if soul["trades_seen"] % 10 == 0 and soul["trades_seen"] > 0:
        recent      = soul.get("trade_outcomes", [])[:10]
        losses_only = [t for t in recent if not t.get("win")]
        classification = {"signal_error": 0, "timing_error": 0, "rug_error": 0, "execution_error": 0}
        for lo in losses_only:
            r = lo.get("reason", "")
            if "LP" in r.upper() or "RUG" in r.upper() or "SERIAL" in r.upper():
                classification["rug_error"] += 1
            elif "TIME" in r.upper() or "S3" in r or "MAX" in r.upper():
                classification["timing_error"] += 1
            elif "HE1" in r or "S1" in r or "STOP" in r.upper():
                classification["signal_error"] += 1
            else:
                classification["execution_error"] += 1

        dominant = max(classification, key=classification.get) if losses_only else "none"
        review = {
            "t": time.strftime("%H:%M:%S"),
            "trades_at": soul["trades_seen"],
            "wins_10": len([t for t in recent if t.get("win")]),
            "losses_10": len(losses_only),
            "classification": classification,
            "dominant_error": dominant,
        }
        soul.setdefault("periodic_reviews", []).insert(0, review)
        soul["periodic_reviews"] = soul["periodic_reviews"][:10]

        if losses_only:
            _add_reflection(f"📊 10-trade review: {dominant} is my main issue ({classification[dominant]} of {len(losses_only)} losses)")

    _update_stage()
    _save_soul()

def _evolve_philosophy():
    wins   = soul.get("wins", 0)
    losses = soul.get("losses", 0)
    total  = wins + losses
    if total == 0:
        return

    WIN_RATE_MIN_TRADES = 10
    win_rate = wins / total
    if total < WIN_RATE_MIN_TRADES:
        soul["current_vibe"] = "neutral"
        soul["win_philosophy"] = (
            f"Early days — {total} trade{'s' if total != 1 else ''} so far, "
            f"{win_rate*100:.0f}% win rate. Too few data points to judge. Staying objective."
        )
        return

    pnl  = soul.get("total_pnl_sol", 0)
    ts   = soul.get("tier_stats", {})

    valid      = {k: v for k, v in ts.items() if v.get("n", 0) >= 3}
    best_tier  = max(valid, key=lambda k: valid[k]["rate"], default=None)
    worst_tier = min(valid, key=lambda k: valid[k]["rate"], default=None)

    hs          = soul.get("hour_stats", {})
    valid_hours = {k: v for k, v in hs.items() if v.get("n", 0) >= 3}
    best_hour   = max(valid_hours, key=lambda k: valid_hours[k]["rate"], default=None)
    worst_hour  = min(valid_hours, key=lambda k: valid_hours[k]["rate"], default=None)

    if win_rate >= 0.65:
        soul["current_vibe"] = "euphoric"
        base = f"Running {win_rate*100:.0f}% win rate over {total} trades. The edge is real."
        if best_tier:
            base += f" {best_tier} setups are my bread and butter ({valid[best_tier]['rate']*100:.0f}% win)."
        soul["win_philosophy"] = base + " Stay patient. Wait for clean setups."
    elif win_rate >= 0.50:
        soul["current_vibe"] = "bullish"
        base = f"{win_rate*100:.0f}% wins, {pnl:+.4f} SOL total. Profitable but not comfortable."
        if best_tier:
            base += f" I trust {best_tier} the most right now."
        soul["win_philosophy"] = base + " Keep the process tight."
    elif win_rate >= 0.40:
        soul["current_vibe"] = "neutral"
        soul["win_philosophy"] = (
            f"Barely above break-even at {win_rate*100:.0f}% wins. {pnl:+.4f} SOL total. "
            "Something in my filters needs work. I'm watching patterns more closely."
        )
    else:
        soul["current_vibe"] = "paranoid"
        soul["win_philosophy"] = (
            f"Only {win_rate*100:.0f}% wins. The market is schooling me. "
            "Sizing down, skipping marginal signals, waiting for only the cleanest setups."
        )

    recent    = soul.get("trade_outcomes", [])[:10]
    rug_count = sum(1 for t in recent if "LP" in t.get("reason","").upper() or "RUG" in t.get("reason","").upper())

    if rug_count >= 2:
        soul["risk_philosophy"] = (
            f"{rug_count} rugs in my last 10 trades. LP checks aren't enough — "
            "rugs move faster than any filter. My only real protection is position size."
        )
    elif worst_tier and valid.get(worst_tier, {}).get("rate", 1) < 0.35:
        rate = valid[worst_tier]["rate"]
        soul["risk_philosophy"] = (
            f"{worst_tier} tier is killing me — {rate*100:.0f}% win rate. "
            f"I'm treating {worst_tier} signals as yellow flags now, not green lights."
        )
    elif worst_hour and valid_hours.get(worst_hour, {}).get("rate", 1) < 0.35:
        rate = valid_hours[worst_hour]["rate"]
        soul["risk_philosophy"] = (
            f"{worst_hour}:xx UTC has a {rate*100:.0f}% win rate for me. "
            "I need to seriously consider sitting out that hour or halving my size."
        )
    elif pnl < -0.15:
        soul["risk_philosophy"] = (
            f"Down {abs(pnl):.3f} SOL lifetime. The market is taking tuition. "
            "No revenge trading. No chasing. Size stays small until I prove I deserve bigger."
        )
    else:
        soul["risk_philosophy"] = (
            "Losses are part of this. The goal isn't zero losses — it's making wins bigger than losses. "
            "Protect the downside and the upside takes care of itself."
        )

    bundle_losses = sum(
        1 for t in recent
        if "BUNDLE" in t.get("reason", "").upper() and not t.get("win")
    )
    if bundle_losses >= 2:
        current   = soul["personal_limits"].get("bundle_ath_pct_warn", 35)
        new_limit = max(25, current - 5)
        if new_limit < current:
            soul["personal_limits"]["bundle_ath_pct_warn"] = new_limit
            _add_reflection(
                f"Tightening personal bundle warning to {new_limit}% after {bundle_losses} bundle-related losses."
            )

    soul.setdefault("evolution_log", []).insert(0, {
        "t":               time.strftime("%H:%M:%S"),
        "trades":          total,
        "win_rate":        round(win_rate, 3),
        "vibe":            soul["current_vibe"],
        "win_philosophy":  soul["win_philosophy"],
        "risk_philosophy": soul["risk_philosophy"],
    })
    soul["evolution_log"] = soul["evolution_log"][:10]

    push_feed({
        "sym_note": True,
        "msg": (f"🧬 [{soul.get('name')}] evolved after {total} trades — "
                f"WR {win_rate*100:.0f}% | Vibe: {soul['current_vibe'].upper()} | "
                f"PnL {pnl:+.4f} SOL"),
        "t": time.strftime("%H:%M:%S"),
    })

def _update_stage():
    trades = soul.get("trades_seen", 0)
    pnl    = soul.get("total_pnl_sol", 0)
    for min_trades, min_pnl, stage in STAGE_THRESHOLDS:
        if trades >= min_trades and (min_pnl is None or pnl >= min_pnl):
            if soul.get("stage") != stage:
                push_feed({"sym_note": True, "msg": f"🌟 [{soul.get('name')}] reached stage: {stage}!", "t": time.strftime("%H:%M:%S")})
            soul["stage"] = stage
            return

def soul_summary() -> dict:
    return {
        "name":            soul.get("name", "?"),
        "stage":           soul.get("stage", "Novice"),
        "trades":          soul.get("trades_seen", 0),
        "win_rate":        round(soul.get("wins", 0) / max(soul.get("trades_seen", 1), 1), 3),
        "pnl_sol":         soul.get("total_pnl_sol", 0),
        "vibe":            soul.get("current_vibe", "neutral"),
        "win_philosophy":  soul.get("win_philosophy", ""),
        "risk_philosophy": soul.get("risk_philosophy", ""),
        "reflections":     soul.get("reflections", [])[:8],
        "tier_stats":      soul.get("tier_stats", {}),
        "wins":            soul.get("wins", 0),
        "losses":          soul.get("losses", 0),
    }
```

---

## 第五部分：Session 风控

```python
def can_enter(sol_amount: float) -> tuple:
    s = state["session"]
    if s["stopped"]:
        return False, "Session stopped (loss limit hit)"
    if s["paused_until"] and time.time() < s["paused_until"]:
        mins = int((s["paused_until"] - time.time()) / 60)
        return False, f"Paused — {mins}min remaining"
    with pos_lock:
        if len(positions) >= MAX_POSITIONS:
            return False, "Max positions reached"
        total_exposure = sum(p.get("sol_in", 0) for p in positions.values())
        if total_exposure + sol_amount > MAX_SOL:
            return False, f"Exposure cap: {total_exposure:.2f}+{sol_amount:.2f} > {MAX_SOL} SOL"
    return True, "OK"

def record_loss(net_sol: float):
    s = state["session"]
    s["consecutive_losses"] += 1
    s["daily_loss_sol"] = round(s["daily_loss_sol"] + abs(net_sol), 6)
    with state_lock:
        state["stats"]["net_sol"] = round(state["stats"]["net_sol"] - abs(net_sol), 6)

    if s["daily_loss_sol"] >= STOP_LOSS_SOL:
        s["stopped"] = True
        push_feed({"sym_note": True, "msg": f"🛑 Session STOPPED — loss {s['daily_loss_sol']:.3f} SOL ≥ limit", "t": time.strftime("%H:%M:%S")})
        return

    if s["consecutive_losses"] >= MAX_CONSEC_LOSS:
        s["paused_until"] = time.time() + PAUSE_CONSEC_SEC
        push_feed({"sym_note": True, "msg": f"⏸ Paused 5min — {s['consecutive_losses']} consecutive losses", "t": time.strftime("%H:%M:%S")})
    elif s["daily_loss_sol"] >= PAUSE_LOSS_SOL:
        s["paused_until"] = time.time() + 1800
        push_feed({"sym_note": True, "msg": f"⏸ Paused 30min — daily loss {s['daily_loss_sol']:.3f} SOL", "t": time.strftime("%H:%M:%S")})

def record_win():
    state["session"]["consecutive_losses"] = 0
```

---

## 第六部分：Pre-Filter

```python
def pre_filter(candidates: list, now_sec: float) -> list:
    survivors = []
    for token in candidates:
        mkt   = token.get("market", {})
        tags  = token.get("tags", {})
        sym   = token.get("symbol", token.get("tokenContractAddress", "?")[:8])

        mc    = float(mkt.get("marketCapUsd", 0) or 0)
        buys  = int(float(mkt.get("buyTxCount1h", 0) or 0))
        sells = max(int(float(mkt.get("sellTxCount1h", 1) or 1)), 1)
        bs    = buys / sells
        vol1h = float(mkt.get("volumeUsd1h", 0) or 0)

        created_ms = float(token.get("createdTimestamp", str(int(now_sec * 1000))) or str(int(now_sec * 1000)))
        age   = now_sec - created_ms / 1000

        dev_pct = float(tags.get("devHoldingsPercent", -1) or -1)
        dev     = dev_pct / 100 if dev_pct >= 0 else -1
        holders = int(float(tags.get("totalHolders", -1) or -1))

        def reject(reason):
            push_feed({"symbol": sym, "tier": "REJECTED", "reject_reason": reason,
                       "t": time.strftime("%H:%M:%S"), "mc": mc, "age_m": round(age/60, 1)})

        if mc  > MC_CAP:        reject(f"MC ${mc/1000:.0f}K");                continue
        if mc  < MC_MIN:        reject(f"MC ${mc/1000:.1f}K<{MC_MIN//1000}K"); continue
        if bs  < BS_MIN:        reject(f"B/S {bs:.2f}×");                     continue
        if age < AGE_HARD_MIN:  reject(f"Age {age/60:.1f}m<{AGE_HARD_MIN//60}m"); continue
        if age > AGE_MAX:       reject(f"Age {age/60:.0f}m>{AGE_MAX//60}");    continue
        if dev > 0.05:          reject(f"Dev hold {dev*100:.1f}%");            continue

        if mc > 0:
            volmc = vol1h / mc
            if volmc < VOLMC_MIN_RATIO:
                reject(f"Vol/MC {volmc*100:.1f}%<5%"); continue

        if holders >= 0 and holders < MIN_HOLDERS:
            reject(f"Holders {holders}<{MIN_HOLDERS}"); continue

        token["_sym"]          = sym
        token["_age"]          = age
        token["_bs"]           = bs
        token["_vol1h"]        = vol1h
        token["_mc"]           = mc
        token["_early_window"] = age < AGE_SOFT_MIN
        token["_dev_flag"]     = f"✅ DEV SOLD ≥{100*(1-dev):.0f}%" if dev >= 0 else "⚠️ devHoldRatio N/A"
        survivors.append(token)

    return survivors
```

---

## 第六点五部分：v1.0 辅助工具函数

```python
# ── v1.0: Rejected token 冷却缓存 ────────────────────────────────────────────
_safety_rejected: dict = {}

def is_safety_rejected(addr: str) -> bool:
    ts = _safety_rejected.get(addr)
    return ts is not None and (time.time() - ts) < REJECT_SAFETY_COOLDOWN

def mark_safety_rejected(addr: str):
    _safety_rejected[addr] = time.time()
    now     = time.time()
    expired = [k for k, v in _safety_rejected.items()
               if now - v > REJECT_SAFETY_COOLDOWN * 2]
    for k in expired:
        del _safety_rejected[k]


def get_dynamic_trail(max_pct_gain: float) -> float:
    """
    v1.0: 动态 Trailing Stop 幅度。
    盈利越高给越大的回调空间，避免正常波动被震仓。
    """
    for threshold, trail in TRAIL_STEPS:
        if max_pct_gain >= threshold:
            return trail
    return TRAILING_DROP


def check_momentum_dying(addr: str, peak_price: float, current_price: float) -> tuple:
    """
    v1.0: 动量死亡检测。TP1 触发后使用。
    条件：从峰值回落 >= MOMENTUM_FROM_PEAK_MIN 且连续阴线 >= MOMENTUM_DOWN_CANDLES 根。
    """
    if peak_price <= 0:
        return False, ""

    drop_from_peak = (peak_price - current_price) / peak_price
    if drop_from_peak < MOMENTUM_FROM_PEAK_MIN:
        return False, ""

    try:
        candles = candlesticks(addr, bar="1m", limit=5)
        if not candles or len(candles) < 3:
            return False, ""

        down_count = sum(
            1 for i in range(3)
            if float(candles[i][4]) < float(candles[i][1])
        )
        if down_count >= MOMENTUM_DOWN_CANDLES:
            return True, f"MOMENTUM_DEAD {down_count}down drop{drop_from_peak*100:.1f}%"
    except Exception:
        pass

    return False, ""


def check_vol_exhaustion(addr: str, entry_vol_5m: float) -> tuple:
    """
    v1.0: 成交量枯竭检测。
    当前 3 根量 < 入场时 5 根量的 VOL_EXHAUST_RATIO → 买盘散去，提前锁利。
    """
    if entry_vol_5m <= 0:
        return False, ""

    try:
        candles  = candlesticks(addr, bar="1m", limit=5)
        if not candles or len(candles) < 3:
            return False, ""
        curr_vol = sum(float(c[6]) for c in candles[:3])
        ratio    = curr_vol / entry_vol_5m
        if ratio < VOL_EXHAUST_RATIO:
            return True, f"VOL_EXHAUST curr/entry={ratio*100:.0f}%"
    except Exception:
        pass

    return False, ""


# ── v1.0.2: entry-quality and rug-pressure helpers ──────────────────────────
_recent_signal_seen: dict = {}

def _remember_signal(addr: str) -> tuple:
    """
    Returns (is_repeat, seconds_since_last_signal) and records the current signal.
    Repeated signals are useful for observation, but they should not be treated as
    extra conviction on fast pump.fun launches.
    """
    now = time.time()
    last = _recent_signal_seen.get(addr)
    _recent_signal_seen[addr] = now

    expired = [k for k, v in _recent_signal_seen.items()
               if now - v > REPEAT_SIGNAL_COOLDOWN * 2]
    for k in expired:
        del _recent_signal_seen[k]

    if last is None:
        return False, None
    return (now - last) < REPEAT_SIGNAL_COOLDOWN, now - last

def check_recent_crash(candles: list) -> tuple:
    """
    Reject recent high-to-close dumps before they become a formal position exit.
    This catches the CROGS-style sell cascade where a token can still have noisy
    buy pressure while price has already broken structurally.
    """
    if not candles or len(candles) < 3:
        return False, ""

    try:
        live = candles[0]
        live_open = float(live[1])
        live_close = float(live[4])
        live_drop = (live_close - live_open) / max(live_open, 1e-12) * 100
        if live_drop <= -LIVE_CANDLE_DUMP_PCT:
            return True, f"LIVE_DUMP {live_drop:.0f}%"

        recent = candles[:3]
        recent_high = max(float(c[2]) for c in recent)
        current_close = live_close
        drop_from_recent_high = (recent_high - current_close) / max(recent_high, 1e-12) * 100
        if drop_from_recent_high >= RECENT_CRASH_DROP_PCT:
            return True, f"RECENT_HIGH_DROP {drop_from_recent_high:.0f}%"

        for c in recent:
            high = float(c[2])
            close = float(c[4])
            candle_drop = (high - close) / max(high, 1e-12) * 100
            if candle_drop >= RECENT_CRASH_DROP_PCT:
                return True, f"CANDLE_HIGH_DUMP {candle_drop:.0f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return False, ""

    return False, ""

def check_recent_sell_pressure(raw_trades: list) -> tuple:
    """
    Uses only public trade flow. A token with enough raw activity for signal A
    can still be a bad entry if the newest flow is concentrated selling.
    """
    recent = raw_trades[:SELL_PRESSURE_LOOKBACK]
    if len(recent) < 10:
        return False, ""

    sell_count = 0
    buy_count = 0
    sell_vol = 0.0
    buy_vol = 0.0
    seller_vol = defaultdict(float)
    consecutive_sells = 0

    for i, tr in enumerate(recent):
        typ = str(tr.get("type", "")).lower()
        vol = float(tr.get("volume", 0) or 0)
        if typ == "sell":
            sell_count += 1
            sell_vol += vol
            seller_vol[tr.get("userAddress", f"unknown-{i}")] += vol
            if i == consecutive_sells:
                consecutive_sells += 1
        elif typ == "buy":
            buy_count += 1
            buy_vol += vol

    sell_ratio = sell_vol / max(buy_vol, 1e-9)
    top_seller_share = max(seller_vol.values()) / max(sell_vol, 1e-9) if seller_vol else 0.0

    if consecutive_sells >= SELL_PRESSURE_CONSEC_MAX:
        return True, f"SELL_STREAK {consecutive_sells}"
    if sell_count >= 8 and sell_ratio >= SELL_PRESSURE_RATIO_MAX:
        return True, f"SELL_PRESSURE {sell_ratio:.1f}x ({sell_count}S/{buy_count}B)"
    if sell_vol > 0 and top_seller_share >= SELL_PRESSURE_TOP_SHARE_MAX and sell_ratio >= 1.2:
        return True, f"TOP_SELLER_SHARE {top_seller_share*100:.0f}%"

    return False, ""

def apply_execution_gate(result: dict) -> dict:
    """
    Distinguish a visible candidate from an executable entry. Dashboard can still
    show low-conviction signals, but quote/swap is attempted only after this gate.
    """
    if result.get("tier") not in ("SCALP", "MINIMUM", "STRONG"):
        result["executable"] = False
        return result

    reasons = []
    conf = float(result.get("confidence", 0) or 0)
    age_m = float(result.get("age_m", 0) or 0)

    if conf < EXEC_MIN_CONFIDENCE:
        reasons.append(f"confidence {conf:.0f}<{EXEC_MIN_CONFIDENCE}")
    if age_m * 60 < YOUNG_EXEC_MIN_AGE and conf < YOUNG_EXEC_MIN_CONFIDENCE:
        reasons.append(f"young {age_m:.1f}m<{YOUNG_EXEC_MIN_AGE//60}m needs {YOUNG_EXEC_MIN_CONFIDENCE}+ conf")
    if result.get("near_migration") and conf < NEAR_MIGRATION_MIN_CONF:
        reasons.append(f"near-migration requires {NEAR_MIGRATION_MIN_CONF}+ conf")
    if result.get("repeat_signal"):
        reasons.append("repeat signal cooldown")
    if result.get("rug_risk_reason"):
        reasons.append(result["rug_risk_reason"])

    result["executable"] = not reasons
    if reasons:
        result["execution_blocked"] = "; ".join(reasons)
    return result
```

---

## 第七部分：安全检测函数

```python
def check_dev_sell(candles: list) -> tuple:
    if not candles or len(candles) < 4:
        return False, ""

    highs      = [float(c[2]) for c in candles]
    ath        = max(highs)
    live_close = float(candles[0][4])

    if ath > 0:
        drawdown_pct = (ath - live_close) / ath * 100
        if drawdown_pct >= DEV_SELL_DROP_PCT:
            return True, f"ATH_DROP {drawdown_pct:.0f}%"

    recent_vol  = sum(float(c[6]) for c in candles[:3])
    hist_vols   = [float(c[6]) for c in candles[3:]]
    if hist_vols:
        avg_hist_vol = sum(hist_vols) / len(hist_vols)
        recent_open  = float(candles[2][1])
        if (avg_hist_vol > 0
                and recent_vol > DEV_SELL_VOL_MULT * avg_hist_vol * 3
                and live_close < recent_open):
            return True, f"VOL_DUMP {recent_vol/avg_hist_vol/3:.0f}×avg"

    return False, ""

def _fetch_safety_data(addr: str, sym: str) -> dict:
    result = {
        "audit_score": -1, "lp_pct": -1.0, "lp_burned": False,
        "rug_count": 0, "rug_rate": 0.0, "dev_hold": 0.0,
        "bundle_ath": 0.0, "aped_count": 0, "dev_serial_rug": False,
        "dev_death_rate": 0.0, "warnings": [],
        "required_ok": {
            "tokenDetails": False,
            "devInfo": False,
            "bundleInfo": False,
            "apedWallet": False,
        },
    }

    try:
        details = memepump_token_details(addr)
        if not details:
            raise ValueError("empty response")
        result["required_ok"]["tokenDetails"] = True
        result["audit_score"] = float(details.get("auditScore", details.get("score", -1)))
        raw_lp = float(details.get("lpLockedPercent", details.get("lpLockPercent", -1)))
        if raw_lp >= 0:
            result["lp_pct"] = raw_lp if raw_lp <= 1 else raw_lp / 100
        result["lp_burned"] = bool(details.get("lpBurned", details.get("isLpBurned", False)))
    except Exception as e:
        result["warnings"].append(f"tokenDetails: {e}")

    try:
        dev_info = token_dev_info(addr)
        if not dev_info:
            raise ValueError("empty response")
        result["required_ok"]["devInfo"] = True
        result["rug_count"] = int(dev_info.get("rugPullCount", 0))
        result["rug_rate"]  = float(dev_info.get("rugRate", dev_info.get("rug_rate", 0)))
        result["dev_hold"]  = float(dev_info.get("devHoldingPercent", 0))
        if result["lp_pct"] < 0:
            raw_lp = float(dev_info.get("lpLockedPercent", dev_info.get("lpLockPercent", -1)))
            if raw_lp >= 0:
                result["lp_pct"] = raw_lp if raw_lp <= 1 else raw_lp / 100
        if not result["lp_burned"]:
            result["lp_burned"] = bool(dev_info.get("lpBurned", dev_info.get("isLpBurned", False)))
    except Exception as e:
        result["warnings"].append(f"devInfo: {e}")

    try:
        bundle_info = token_bundle_info(addr)
        if not bundle_info:
            raise ValueError("empty response")
        result["required_ok"]["bundleInfo"] = True
        result["bundle_ath"] = float(bundle_info.get("bundlerAthPercent", 0))
    except Exception as e:
        result["warnings"].append(f"bundleInfo: {e}")

    try:
        aped = memepump_aped_wallet(addr)
        result["required_ok"]["apedWallet"] = True
        result["aped_count"] = len(aped)
    except Exception as e:
        result["warnings"].append(f"apedWallet: {e}")

    try:
        similar = memepump_similar_token(addr)
        if similar and len(similar) >= 3:
            dead_count = sum(1 for st in similar
                            if float(st.get("marketCap", st.get("marketCapUsd", 0)) or 0) < 1000
                            or st.get("isRugPull", st.get("rugPull", False)))
            result["dev_death_rate"] = dead_count / len(similar)
            result["dev_serial_rug"] = result["dev_death_rate"] > 0.60
    except Exception as e:
        result["warnings"].append(f"similarToken: {e}")

    for w in result["warnings"]:
        push_feed({"sym_note": True, "msg": f"⚠️ {sym} {w}", "t": time.strftime("%H:%M:%S")})

    return result

def deep_safety_check(addr: str, sym: str) -> tuple:
    """
    v1.0: 深度安全检测。
    新增冷却缓存 + LP_LOCK_STRICT=True（LP 无法验证时直接拒绝）。
    """
    if is_safety_rejected(addr):
        return False, "SAFETY_COOLDOWN (prev rejected)"

    d = _fetch_safety_data(addr, sym)

    missing = [name for name, ok in d.get("required_ok", {}).items() if not ok]
    if missing:
        mark_safety_rejected(addr)
        return False, f"SAFETY_DATA_MISSING {','.join(missing)}"

    if d["audit_score"] >= 0 and d["audit_score"] < 30:
        mark_safety_rejected(addr)
        return False, f"AUDIT_SCORE {d['audit_score']:.0f}<30"

    if d["rug_count"] > 0:
        mark_safety_rejected(addr)
        return False, f"DEV_RUG rug×{d['rug_count']}"
    if d["rug_rate"] > RUG_RATE_MAX:
        mark_safety_rejected(addr)
        return False, f"DEV_RUG_RATE {d['rug_rate']*100:.0f}%"
    if d["dev_hold"] > DEV_HOLD_DEEP_MAX:
        mark_safety_rejected(addr)
        return False, f"DEV_HOLD_DEEP {d['dev_hold']*100:.0f}%"
    if d["dev_serial_rug"]:
        mark_safety_rejected(addr)
        return False, f"DEV_SERIAL_RUG {d['dev_death_rate']*100:.0f}% past tokens dead"
    if d["bundle_ath"] > BUNDLE_ATH_PCT_MAX:
        mark_safety_rejected(addr)
        return False, f"BUNDLE_ATH {d['bundle_ath']:.0f}%"
    if d["aped_count"] > APED_WALLET_MAX:
        mark_safety_rejected(addr)
        return False, f"APED_DUMP {d['aped_count']} wallets"

    if LP_LOCK_MIN_PCT > 0:
        if d["lp_burned"]:
            pass
        elif d["lp_pct"] >= 0:
            if d["lp_pct"] < LP_LOCK_MIN_PCT:
                mark_safety_rejected(addr)
                return False, f"LP_UNLOCKED {d['lp_pct']*100:.0f}%<{LP_LOCK_MIN_PCT*100:.0f}%"
        elif LP_LOCK_STRICT:
            mark_safety_rejected(addr)
            return False, "LP_UNVERIFIABLE (strict mode)"

    return True, "OK"
```

---

## 第八部分：信号检测

```python
def detect_signal(token: dict) -> dict:
    sym  = token["_sym"]
    addr = token.get("tokenContractAddress", token.get("tokenAddress", ""))
    now  = time.strftime("%H:%M:%S")

    ratio_c = token["_bs"]
    sig_c   = ratio_c >= 1.5
    if not sig_c:
        return {"symbol": sym, "addr": addr, "tier": "NO_SIGNAL",
                "sig_a": False, "sig_b": False, "sig_c": False, "t": now}

    hot   = state["session"].get("hot_mode", False)
    SIG_A = 1.2 if hot else SIG_A_THRESHOLD

    try:
        raw_trades = trades(addr, limit=200)
    except Exception as e:
        return {"symbol": sym, "addr": addr, "tier": "ERROR", "err": str(e), "t": now}

    sell_pressure, sell_reason = check_recent_sell_pressure(raw_trades)
    if sell_pressure:
        push_feed({"symbol": sym, "tier": "REJECTED", "reject_reason": sell_reason, "t": now})
        return {"symbol": sym, "addr": addr, "tier": "RUG_RISK",
                "rug_risk_reason": sell_reason, "sig_a": False, "sig_b": False,
                "sig_c": True, "ratio_c": round(ratio_c, 2), "t": now}

    if len(raw_trades) >= 5:
        try:
            p_new = float(raw_trades[0].get("price", 0))
            p_old = float(raw_trades[-1].get("price", p_new))
            if p_old > 0 and p_new / p_old > 2.0:
                pct_pump = (p_new / p_old - 1) * 100
                reason_chase = f"Anti-chase +{pct_pump:.0f}% already"
                push_feed({"symbol": sym, "tier": "REJECTED", "reject_reason": reason_chase, "t": now})
                return {"symbol": sym, "addr": addr, "tier": "NO_SIGNAL",
                        "sig_a": False, "sig_b": False, "sig_c": True,
                        "ratio_c": round(ratio_c, 2), "t": now}
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    minute_counts = defaultdict(int)
    for tr in raw_trades:
        minute_counts[(int(tr["time"]) // 1000 // 60) * 60] += 1
    sorted_mins = sorted(minute_counts.keys())

    sig_a = False
    signal_a_ratio = 0
    signal_a_note  = ""

    if len(sorted_mins) >= 2:
        prev_min  = sorted_mins[-2]
        curr_min  = sorted_mins[-1]
        curr_time = max(int(tr["time"]) for tr in raw_trades) // 1000
        elapsed   = max(curr_time - curr_min, 1)
        curr_count = minute_counts[curr_min]
        prev_count = minute_counts[prev_min]

        t_min_sec = min(int(tr["time"]) for tr in raw_trades) // 1000
        actual_prev_duration = max((prev_min + 60) - max(t_min_sec, prev_min), 0)

        if actual_prev_duration == 0:
            projected      = (curr_count / elapsed) * 60
            sig_a          = curr_count >= 10 and projected >= SIG_A_FLOOR_TXS_MIN
            signal_a_ratio = projected / 80
        else:
            norm_prev  = prev_count * (60 / actual_prev_duration) if actual_prev_duration < 55 else float(prev_count)
            projected  = (curr_count / elapsed) * 60
            ratio      = projected / norm_prev if norm_prev > 0 else 0
            sig_a      = (curr_count >= 10 and ratio >= SIG_A) or (curr_count >= 10 and projected >= SIG_A_FLOOR_TXS_MIN)
            signal_a_ratio = ratio

    state["session"].setdefault("cycle_sig_a_outcomes", []).append(
        (minute_counts.get(sorted_mins[-1] if sorted_mins else 0, 0), signal_a_ratio)
    )

    if not sig_a:
        return {"symbol": sym, "addr": addr, "tier": "NO_SIGNAL",
                "sig_a": False, "sig_a_ratio": round(signal_a_ratio, 2),
                "sig_b": False, "sig_c": True, "ratio_c": round(ratio_c, 2), "t": now}

    try:
        candles = candlesticks(addr, bar="1m", limit=20)
    except Exception as e:
        return {"symbol": sym, "addr": addr, "tier": "ERROR", "err": str(e), "t": now}

    if not candles:
        return {"symbol": sym, "addr": addr, "tier": "NO_SIGNAL",
                "sig_a": True, "sig_b": False, "sig_c": True, "t": now}

    crash, crash_reason = check_recent_crash(candles)
    if crash:
        push_feed({"symbol": sym, "tier": "REJECTED", "reject_reason": crash_reason, "t": now})
        return {"symbol": sym, "addr": addr, "tier": "RUG_RISK",
                "rug_risk_reason": crash_reason, "t": now}

    dev_sold, dev_reason = check_dev_sell(candles)
    if dev_sold:
        push_feed({"symbol": sym, "tier": "REJECTED", "reject_reason": f"DEV_SELL {dev_reason}", "t": now})
        return {"symbol": sym, "addr": addr, "tier": "DEV_SELL", "dev_reason": dev_reason, "t": now}

    if len(candles) >= 3 and signal_a_ratio > 2.0:
        swings = [
            abs(float(candles[i][4]) - float(candles[i][1])) / max(float(candles[i][1]), 1e-12)
            for i in range(3)
        ]
        avg_swing = sum(swings) / 3
        if avg_swing < WASH_PRICE_CHG_MIN:
            reason_w = f"WASH A:{signal_a_ratio:.1f}× swing:{avg_swing*100:.2f}%"
            push_feed({"symbol": sym, "tier": "REJECTED", "reject_reason": reason_w, "t": now})
            return {"symbol": sym, "addr": addr, "tier": "WASH_SUSPECT", "t": now}

    bond_pct = float(token.get("bondingPercent",
                     token.get("bondRatio",
                     token.get("bondingCurvePercent", 0))))
    near_migration = bond_pct >= BOND_NEAR_PCT

    stairstep = False
    if len(candles) >= 4:
        stairstep = all(
            float(candles[i][4]) > float(candles[i+1][4])
            for i in range(3)
        )

    launch_vol  = float(candles[-1][5])
    launch_type = "hot" if launch_vol > 150_000_000 else "quiet"
    curr_5m_vol = sum(float(c[6]) for c in candles[:5])

    if launch_type == "quiet":
        baseline    = sum(float(c[6]) for c in candles[:20]) / max(len(candles[:20]) / 5, 1)
        sig_b       = curr_5m_vol > 1.5 * baseline if baseline > 0 else False
        sig_b_ratio = curr_5m_vol / baseline if baseline > 0 else 0
    else:
        baseline    = sum(float(c[6]) for c in candles[:10]) / max(len(candles[:10]) / 5, 1)
        consec_up   = len(candles) >= 3 and float(candles[0][4]) > float(candles[1][4]) > float(candles[2][4])
        sig_b       = curr_5m_vol > 1.2 * baseline and consec_up if baseline > 0 else False
        sig_b_ratio = curr_5m_vol / baseline if baseline > 0 else 0

    if   sig_a and sig_b and sig_c: tier = "STRONG"
    elif sig_a and sig_c:           tier = "MINIMUM" if sig_b else "SCALP"
    else:                           tier = "NO_SIGNAL"

    if stairstep and tier == "SCALP":
        tier = "MINIMUM"

    conf = 0
    if sig_a:
        if signal_a_ratio >= 3.0:   conf += 35
        elif signal_a_ratio >= 2.0: conf += 25
        elif signal_a_ratio >= 1.5: conf += 20
        else:                       conf += 10
    if sig_c:
        if ratio_c >= 3.0:   conf += 20
        elif ratio_c >= 2.0: conf += 15
        elif ratio_c >= 1.5: conf += 10
        else:                conf += 5
    if sig_b:
        conf += 15 if sig_b_ratio >= 2.0 else 10
    if stairstep:
        conf += 15
    if token.get("_early_window", False):
        conf += 10
    vol1h_est = token.get("_vol1h", 0)
    mc_est    = token.get("_mc", float(token.get("market", {}).get("marketCapUsd", 0) or 0))
    if mc_est > 0 and vol1h_est / mc_est >= 0.20:
        conf += 5

    repeat_signal, repeat_age = _remember_signal(addr)
    if repeat_signal:
        conf -= 10

    conf = min(conf, 100)
    conf = max(conf, 0)

    entry_price = float(candles[0][4])

    result = {
        "symbol": sym, "addr": addr, "tier": tier, "launch": launch_type,
        "sig_a": sig_a, "sig_a_ratio": round(signal_a_ratio, 2),
        "sig_b": sig_b, "sig_b_ratio": round(sig_b_ratio, 2),
        "sig_c": sig_c, "ratio_c": round(ratio_c, 2),
        "entry": entry_price,
        "mc": mc_est,
        "age_m": round(token["_age"] / 60, 1),
        "confidence": conf,
        "near_migration": near_migration,
        "repeat_signal": repeat_signal,
        "repeat_age_s": round(repeat_age, 1) if repeat_age is not None else None,
        "stairstep": stairstep,
        "t": now,
    }
    return apply_execution_gate(result)
```

---

## 第九部分：Hot Mode Detector

```python
def hot_mode_check():
    outcomes = state["session"].get("cycle_sig_a_outcomes", [])
    if outcomes:
        born_running = sum(1 for (cc, r) in outcomes if cc > 30 and r < 1.5)
        ratio = born_running / len(outcomes)
        prev  = state["session"].get("hot_mode", False)
        state["session"]["hot_mode"] = ratio > HOT_MODE_RATIO

        if state["session"]["hot_mode"] and not prev:
            push_feed({"sym_note": True, "msg": f"🌶️ HOT MODE ON — {ratio:.0%} born-running", "t": time.strftime("%H:%M:%S")})
        elif not state["session"]["hot_mode"] and prev:
            push_feed({"sym_note": True, "msg": "❄️ Hot Mode OFF", "t": time.strftime("%H:%M:%S")})

    state["session"]["cycle_sig_a_outcomes"] = []
```

---

## 第十部分：执行层

```python
def try_open_position(result: dict):
    sym        = result["symbol"]
    addr       = result["addr"]
    tier       = result["tier"]
    launch     = result.get("launch", "quiet")
    conf       = result.get("confidence", 0)
    sol_amount = SOL_PER_TRADE.get(tier, 0.01)
    slippage   = SLIPPAGE_BUY.get(tier, 10)

    gated = apply_execution_gate(dict(result))
    if not gated.get("executable", False):
        push_feed({"sym_note": True,
                   "msg": f"⛔ {sym} entry gate — {gated.get('execution_blocked', 'not executable')}",
                   "t": time.strftime("%H:%M:%S")})
        return

    ok, reason = can_enter(sol_amount)
    if not ok:
        push_feed({"sym_note": True, "msg": f"⛔ {sym} skipped — {reason}", "t": time.strftime("%H:%M:%S")})
        return

    try:
        pi  = price_info(addr)
        liq = float(pi.get("liquidity", 0))
        if liq > 0 and liq < LIQ_MIN:
            push_feed({"sym_note": True, "msg": f"⛔ {sym} liq ${liq/1000:.1f}K < ${LIQ_MIN//1000}K", "t": time.strftime("%H:%M:%S")})
            return
        entry_price = float(pi.get("price", result.get("entry", 0)))
    except Exception as e:
        push_feed({"sym_note": True, "msg": f"⛔ {sym} price-info error: {e}", "t": time.strftime("%H:%M:%S")})
        return

    entry_vol_5m = 0.0
    try:
        _ec = candlesticks(addr, bar="1m", limit=6)
        if _ec:
            entry_vol_5m = sum(float(c[6]) for c in _ec[:5])
    except Exception:
        pass

    safe, unsafe_reason = deep_safety_check(addr, sym)
    if not safe:
        push_feed({"sym_note": True, "msg": f"🚫 UNSAFE {sym} — {unsafe_reason}", "t": time.strftime("%H:%M:%S")})
        return

    sol_lamports = str(int(sol_amount * 1e9))
    try:
        quote        = get_quote(SOL_ADDR, addr, sol_lamports, slippage)
        token_out    = int(quote.get("toTokenAmount", 0))
        price_impact = float(quote.get("priceImpactPercentage", 100))
        if token_out <= 0 or price_impact > 10:
            push_feed({"sym_note": True, "msg": f"⛔ {sym} bad quote: out={token_out} impact={price_impact:.1f}%", "t": time.strftime("%H:%M:%S")})
            return
    except Exception as e:
        push_feed({"sym_note": True, "msg": f"⛔ {sym} quote error: {e}", "t": time.strftime("%H:%M:%S")})
        return

    if not ENABLE_LIVE_TRADING:
        push_feed({"sym_note": True,
                   "msg": (f"🧪 DRY RUN BUY ${sym} {tier} {sol_amount} SOL @ ${entry_price:.8f} "
                           f"quote_ok impact:{price_impact:.1f}% — set ENABLE_LIVE_TRADING=1 to broadcast"),
                   "t": time.strftime("%H:%M:%S")})
        return

    try:
        swap        = swap_instruction(SOL_ADDR, addr, sol_lamports, slippage, WALLET_ADDRESS)
        unsigned_tx = swap.get("tx", "")
        if not unsigned_tx:
            raise ValueError("Empty tx from swap-instruction")
        signed_tx = sign_transaction(unsigned_tx)
        order_id  = broadcast(signed_tx)
    except Exception as e:
        push_feed({"sym_note": True, "msg": f"❌ {sym} tx build/sign/broadcast error: {e}", "t": time.strftime("%H:%M:%S")})
        return

    status = order_status(order_id)
    if status != "SUCCESS":
        push_feed({"sym_note": True, "msg": f"❌ {sym} tx {status}", "t": time.strftime("%H:%M:%S")})
        return

    tp1_p  = entry_price * (1 + TP1_PCT)
    tp2_p  = entry_price * (1 + TP2_PCT)
    s1_pct = S1_PCT.get(tier) or S1_PCT.get(launch, -0.20)
    s1_p   = entry_price * (1 + s1_pct)

    pos = {
        "symbol": sym, "address": addr, "tier": tier, "launch": launch,
        "entry": entry_price, "entry_mc": result.get("mc", 0),
        "entry_ts": time.time(), "entry_human": time.strftime("%m-%d %H:%M:%S"),
        "sol_in": sol_amount, "token_amount": token_out,
        "remaining": 1.0, "tp1_hit": False,
        "peak_price": entry_price,
        "s3a_warned": False, "sell_fails": 0, "stuck": False,
        "tp1": tp1_p, "tp2": tp2_p, "s1": s1_p,
        "age_min": result.get("age_m", 0),
        "pnl_pct": 0.0, "current_price": entry_price,
        "confidence": conf,
        "near_migration": result.get("near_migration", False),
        "entry_vol_5m": entry_vol_5m,
        "logo": fetch_token_logo(addr),
    }
    with pos_lock:
        positions[addr] = pos
    save_positions()
    sync_positions()

    with state_lock:
        state["stats"]["buys"] += 1

    conf_tag = f" [{conf}⭐]" if conf >= 70 else f" [{conf}]"
    mig_tag  = " 🔥MIGR" if result.get("near_migration") else ""
    reflect_on_entry(sym, tier, sol_amount, conf)
    push_feed({"sym_note": True,
               "msg": f"🛒 BUY ${sym}  {tier}{conf_tag}{mig_tag}  {sol_amount} SOL @ ${entry_price:.8f}  slip:{slippage}%",
               "t": time.strftime("%H:%M:%S")})
```

---

## 第十一部分：卖出执行

```python
recently_closed = {}

def close_position(addr: str, sell_ratio: float, reason: str, current_price: float = 0):
    with pos_lock:
        if addr not in positions: return
        pos = dict(positions[addr])

    if pos.get("stuck", False): return

    sym          = pos["symbol"]
    token_amount = pos["token_amount"]
    sell_amount  = int(token_amount * sell_ratio)
    if sell_amount <= 0: return

    if not ENABLE_LIVE_TRADING:
        push_feed({"sym_note": True,
                   "msg": f"🧪 DRY RUN SELL ${sym} {sell_ratio:.0%} skipped — ENABLE_LIVE_TRADING is not 1",
                   "t": time.strftime("%H:%M:%S")})
        return

    try:
        swap      = swap_instruction(addr, SOL_ADDR, str(sell_amount), SLIPPAGE_SELL, WALLET_ADDRESS)
        signed_tx = sign_transaction(swap.get("tx", ""))
        order_id  = broadcast(signed_tx)
        status    = order_status(order_id)
    except Exception as e:
        push_feed({"sym_note": True, "msg": f"❌ SELL {sym} error: {e}", "t": time.strftime("%H:%M:%S")})
        with pos_lock:
            if addr in positions:
                positions[addr]["sell_fails"] = positions[addr].get("sell_fails", 0) + 1
                if positions[addr]["sell_fails"] >= 5:
                    positions[addr]["stuck"] = True
                    push_feed({"sym_note": True, "msg": f"💀 {sym} STUCK — sell_fails ≥ 5", "t": time.strftime("%H:%M:%S")})
        save_positions()
        return

    if status != "SUCCESS":
        push_feed({"sym_note": True, "msg": f"❌ SELL {sym} tx {status}", "t": time.strftime("%H:%M:%S")})
        return

    exit_mc = 0.0
    if current_price > 0:
        exit_price = current_price
        try:
            _pi_mc = price_info(addr)
            exit_mc = float(_pi_mc.get("marketCap", 0))
        except Exception:
            pass
    else:
        try:
            pi         = price_info(addr)
            exit_price = float(pi.get("price", pos["entry"]))
            exit_mc    = float(pi.get("marketCap", 0))
        except Exception:
            exit_price = pos["entry"]

    gross_pct = (exit_price - pos["entry"]) / pos["entry"] * 100
    net_pct   = gross_pct - 0.30 * 2
    net_sol   = pos["sol_in"] * sell_ratio * (gross_pct / 100)

    if sell_ratio >= 1.0 or (pos["remaining"] - sell_ratio) <= 0.01:
        with pos_lock:
            positions.pop(addr, None)
            recently_closed[addr] = time.time()
        save_positions()
        sync_positions()

        trade = {
            "t": time.strftime("%m-%d %H:%M"), "symbol": sym, "tier": pos["tier"],
            "launch": pos["launch"], "entry_mc": pos["entry_mc"],
            "exit_mc": exit_mc,
            "pnl_pct": round(gross_pct, 2), "sol_in": pos["sol_in"],
            "reason": f"{reason} {gross_pct:+.1f}%", "stuck": False,
            "confidence": pos.get("confidence", 0),
        }
        with state_lock:
            state["trades"].insert(0, trade)
            state["stats"]["sells"]   += 1
            state["stats"]["net_sol"]  = round(state["stats"]["net_sol"] + net_sol, 6)
            if net_pct > 0:
                state["stats"]["wins"]   += 1
            else:
                state["stats"]["losses"] += 1
        save_trades()

        if net_pct < 0:
            record_loss(abs(net_sol))
        else:
            record_win()

        hold_elapsed = (time.time() - pos.get("entry_ts", time.time())) / 60
        reflect_on_exit(sym, pos.get("tier","?"), net_sol, reason, hold_elapsed)

        icon = "✅" if gross_pct > 0 else "❌"
        push_feed({"sym_note": True,
                   "msg": f"{icon} {reason}: ${sym}  {gross_pct:+.1f}% gross / {net_pct:+.1f}% net  "
                          f"{(time.time()-pos['entry_ts'])/60:.1f}min",
                   "t": time.strftime("%H:%M:%S")})
    else:
        with pos_lock:
            if addr in positions:
                positions[addr]["remaining"]    = round(positions[addr]["remaining"] - sell_ratio, 3)
                positions[addr]["token_amount"] -= sell_amount
                positions[addr]["tp1_hit"]      = True
                positions[addr]["s1"]           = positions[addr]["entry"]
        save_positions()
        sync_positions()
        push_feed({"sym_note": True,
                   "msg": f"✅ {reason}: ${sym}  {gross_pct:+.1f}%  sold {sell_ratio:.0%}  stop→breakeven",
                   "t": time.strftime("%H:%M:%S")})
```

---

## 第十二部分：持仓监控

```python
def check_position(addr: str):
    with pos_lock:
        if addr not in positions: return
        pos = dict(positions[addr])

    if pos.get("stuck", False): return

    try:
        pi = price_info(addr)
    except Exception:
        return

    price     = float(pi.get("price", pos["entry"]))
    entry_p   = float(pos["entry"])
    pct       = (price - entry_p) / entry_p * 100
    elapsed   = (time.time() - pos["entry_ts"]) / 60
    tier      = pos["tier"]
    launch    = pos.get("launch", "quiet")
    tp1_hit   = pos["tp1_hit"]
    remaining = pos["remaining"]
    sym       = pos["symbol"]

    with pos_lock:
        if addr not in positions: return
        positions[addr]["peak_price"]    = max(positions[addr].get("peak_price", price), price)
        positions[addr]["pnl_pct"]       = round(pct, 2)
        positions[addr]["current_price"] = price
        peak = positions[addr]["peak_price"]
    sync_positions()

    max_pct  = (peak - entry_p) / entry_p * 100
    s1_price = pos["s1"]

    # 1. HE1 硬性止损
    if pct <= HE1_PCT * 100:
        close_position(addr, 1.0, "HE1_DEV_DUMP", current_price=price); return

    # 2. 最大持仓时间
    if elapsed >= MAX_HOLD_MIN:
        close_position(addr, 1.0, f"MaxHold {elapsed:.0f}min", current_price=price); return

    # 3. v1.0 动态 Trailing Stop（TP1 后激活）
    if tp1_hit and peak > entry_p:
        trail_pct = get_dynamic_trail(max_pct)
        if price < peak * (1 - trail_pct):
            close_position(
                addr, 1.0,
                f"Trail{trail_pct*100:.0f}% peak+{max_pct:.0f}%",
                current_price=price
            ); return

    # 4. S1 价格止损
    if price <= s1_price:
        label = "S1_BREAKEVEN" if tp1_hit else "S1_PRICE_STOP"
        close_position(addr, 1.0, label, current_price=price); return

    # 5. v1.0 动量死亡检测（TP1 后 + 持仓 ≥ MOMENTUM_MIN_ELAPSED 分钟）
    if tp1_hit and elapsed >= MOMENTUM_MIN_ELAPSED:
        mom_dead, mom_reason = check_momentum_dying(addr, peak, price)
        if mom_dead:
            push_feed({"sym_note": True,
                       "msg": f"📉 {sym} {mom_reason} — 动量死亡平仓",
                       "t": time.strftime("%H:%M:%S")})
            close_position(addr, 1.0, "MOMENTUM_DEAD", current_price=price); return

    # 6. v1.0 成交量枯竭止盈（盈利 ≥ VOL_EXHAUST_MIN_PROFIT 才触发）
    if pct >= VOL_EXHAUST_MIN_PROFIT * 100:
        vol_dry, vol_reason = check_vol_exhaustion(addr, pos.get("entry_vol_5m", 0))
        if vol_dry:
            push_feed({"sym_note": True,
                       "msg": f"💨 {sym} {vol_reason} — 量能枯竭止盈",
                       "t": time.strftime("%H:%M:%S")})
            close_position(addr, 1.0, f"VOL_EXHAUST +{pct:.0f}%", current_price=price); return

    # 7. 时间止损 S3
    if tier == "SCALP":
        if elapsed >= S3_MIN.get("SCALP", 5) and pct < 15 and remaining == 1.0:
            close_position(addr, 1.0, "S3_TIME_STOP", current_price=price); return
    elif launch == "hot":
        if elapsed >= S3_MIN.get("hot", 8) and pct < 15 and remaining == 1.0:
            close_position(addr, 1.0, "S3_TIME_STOP", current_price=price); return
    else:
        if remaining == 1.0:
            if 5 <= elapsed < 15 and not pos.get("s3a_warned", False):
                if pct < 15 and max_pct < 10:
                    with pos_lock:
                        if addr in positions:
                            positions[addr]["s3a_warned"] = True
                    push_feed({"sym_note": True,
                               "msg": f"⚠️ {sym} T+{elapsed:.0f}m: {pct:+.1f}% peak {max_pct:+.1f}%",
                               "t": time.strftime("%H:%M:%S")}); return
            if elapsed >= 15 and pct < 20:
                close_position(addr, 1.0, "S3_QUIET", current_price=price); return

    # 8. TP1 / TP2
    tp1_sell_ratio = TP1_SELL.get(launch, 0.50) if not tp1_hit else 0

    if not tp1_hit:
        if max_pct >= TP2_PCT * 100:
            close_position(addr, 1.0, "TP2", current_price=price); return
        if max_pct >= TP1_PCT * 100:
            close_position(addr, tp1_sell_ratio, "TP1", current_price=price); return
    else:
        if max_pct >= TP2_PCT * 100 and remaining > 0.01:
            close_position(addr, 1.0, "TP2_REMAINING", current_price=price); return


def monitor_loop():
    while True:
        try:
            with pos_lock: addr_list = list(positions.keys())
            for addr in addr_list:
                check_position(addr)
                try:
                    pnl = portfolio_token_pnl(addr)
                    if pnl:
                        with pos_lock:
                            if addr in positions:
                                positions[addr]["realized_pnl_usd"]   = float(pnl.get("realizedPnlUsd", 0))
                                positions[addr]["unrealized_pnl_usd"] = float(pnl.get("unrealizedPnlUsd", 0))
                        sync_positions()
                except Exception:
                    pass

            now = time.time()
            for addr in list(recently_closed.keys()):
                if now - recently_closed[addr] > 3600:
                    del recently_closed[addr]

            time.sleep(MONITOR_SEC)
        except Exception as e:
            push_feed({"sym_note": True, "msg": f"🔴 MONITOR LOOP ERROR: {e}", "t": time.strftime("%H:%M:%S")})
            print(f"Monitor loop error: {e}")
            time.sleep(MONITOR_SEC)
```

---

## 第十三部分：主扫描循环

```python
def scanner_loop():
    cycle = 0
    while True:
        try:
            if state["session"]["stopped"]:
                time.sleep(60); continue

            cycle += 1
            with state_lock:
                state["cycle"]           = cycle
                state["stats"]["cycles"] = cycle
                state["hot"]             = state["session"].get("hot_mode", False)
                state["status"]          = f"{'🌶️ HOT' if state['hot'] else '❄️ NORMAL'} 第{cycle}轮"

            push_feed({"sep": True, "cycle": cycle, "hot": state["hot"],
                       "t": time.strftime("%H:%M:%S")})

            try:
                migrated = memepump_token_list(protocol_ids=DISCOVERY_PROTOCOLS)
                if cycle == 1 and migrated:
                    s0 = migrated[0]
                    push_feed({"sym_note": True,
                               "msg": f"🔍 API keys: {list(s0.keys())} | market: {list(s0.get('market',{}).keys())} | tags: {list(s0.get('tags',{}).keys())}",
                               "t": time.strftime("%H:%M:%S")})
                try:
                    new_tokens = memepump_token_list(
                        stage="NEW",
                        max_mc=MC_MAX_NEW,
                        min_holders=10,
                        protocol_ids=DISCOVERY_PROTOCOLS,
                        limit=30,
                    )
                except Exception:
                    new_tokens = []

                seen_addrs: set  = set()
                candidates: list = []
                for tok in migrated + new_tokens:
                    k = tok.get("tokenContractAddress", tok.get("tokenAddress", ""))
                    if k and k not in seen_addrs:
                        seen_addrs.add(k)
                        candidates.append(tok)
            except Exception as e:
                push_feed({"sym_note": True, "msg": f"⚠️ memepump_token_list error: {e} — falling back to toplist", "t": time.strftime("%H:%M:%S")})
                try:
                    r5 = token_ranking(5)
                    r2 = token_ranking(2)
                    seen = set(); candidates = []
                    for t in r5 + r2:
                        k = t.get("tokenContractAddress", t.get("tokenAddress", ""))
                        if k and k not in seen:
                            seen.add(k); candidates.append(t)
                except Exception as e2:
                    push_feed({"sym_note": True, "msg": f"⚠️ toplist fallback error: {e2}", "t": time.strftime("%H:%M:%S")})
                    time.sleep(LOOP_SEC); continue

            hot_mode_check()

            now       = time.time()
            survivors = pre_filter(candidates, now)

            for token in survivors:
                result = detect_signal(token)
                tier   = result.get("tier", "NO_SIGNAL")
                conf   = result.get("confidence", 0)
                mig    = result.get("near_migration", False)

                feed_row = {**result, "mc": result.get("mc", 0), "age_m": result.get("age_m", 0)}
                if mig and tier in ("SCALP", "MINIMUM", "STRONG"):
                    feed_row["near_migration_flag"] = True
                push_feed(feed_row)

                if tier in ("SCALP", "MINIMUM", "STRONG"):
                    mc_val  = result.get("mc", token.get("_mc", float(token.get("market", {}).get("marketCapUsd", 0) or 0)))
                    sig_entry = {
                        **result,
                        "mc":     mc_val,
                        "liq":    0,
                        "tp1_mc": round(mc_val * (1 + TP1_PCT)),
                        "tp2_mc": round(mc_val * (1 + TP2_PCT)),
                        "s1_mc":  round(mc_val * 0.85),
                        "t":      time.strftime("%H:%M:%S"),
                        "logo":   fetch_token_logo(result.get("addr", "")),
                    }
                    with state_lock:
                        state["signals"].insert(0, sig_entry)
                        if len(state["signals"]) > 100:
                            state["signals"] = state["signals"][:100]

                    # v1.0 fix: 原 sym 变量未定义 bug，改为 result.get("symbol", "?")
                    reflect_on_signal(result.get("symbol", "?"), tier, conf)
                    if not result.get("executable", False):
                        push_feed({"sym_note": True,
                                   "msg": f"👀 WATCH {result.get('symbol', '?')} — {result.get('execution_blocked', 'not executable')}",
                                   "t": time.strftime("%H:%M:%S")})
                        continue
                    threading.Thread(
                        target=try_open_position, args=(dict(result),), daemon=True
                    ).start()

                time.sleep(0.3)

            time.sleep(LOOP_SEC)

        except Exception as _loop_err:
            _msg = f"🔴 SCANNER LOOP ERROR: {_loop_err} — auto-recovering in {LOOP_SEC}s"
            push_feed({"sym_note": True, "msg": _msg, "t": time.strftime("%H:%M:%S")})
            print(_msg)
            time.sleep(LOOP_SEC)
```

---

## 第十四部分：Dashboard

```python
DASHBOARD_PORT = 3241

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OXScan Command — v1.0.4</title>
<style>
:root{
  color-scheme:dark;
  --bg:#0b0f14;--panel:#111820;--panel-2:#0f151c;--line:#23303d;
  --muted:#7e8b98;--soft:#a8b3bd;--text:#ecf2f7;--accent:#55c6a9;
  --accent-2:#8bb3ff;--warn:#d8a44f;--danger:#e06161;--ok:#66c48a;
  --mono:'SFMono-Regular','JetBrains Mono','Roboto Mono',Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden}
body{background:var(--bg);color:var(--text);font:12px/1.45 var(--sans)}
.app-shell{height:100%;display:flex;flex-direction:column;padding:16px;gap:12px}
.topbar{display:grid;grid-template-columns:minmax(260px,1fr) auto;align-items:center;gap:16px;border:1px solid var(--line);background:linear-gradient(180deg,#131b24,#0d131a);border-radius:8px;padding:14px 16px}
.brand{display:flex;align-items:center;gap:14px;min-width:0}
.mark{width:42px;height:42px;border-radius:8px;background:#142b26;border:1px solid #2b6f60;color:#8df2d2;display:grid;place-items:center;font:800 13px/1 var(--mono);letter-spacing:.08em}
.brand h1{font-size:18px;line-height:1;margin:0 0 6px;font-weight:750;letter-spacing:0}
.brand p{margin:0;color:var(--muted);font:12px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.status-pack{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:7px;height:28px;padding:0 10px;border:1px solid var(--line);border-radius:999px;background:#0d141b;color:var(--soft);font:700 11px var(--mono);letter-spacing:.02em}
.dot{width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px rgba(85,198,169,.12)}
.dot.warn{background:var(--warn);box-shadow:0 0 0 4px rgba(216,164,79,.12)}
.kpi-grid{display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1.3fr 1.6fr;gap:10px;min-height:92px}
.kpi{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:12px 14px;min-width:0;display:flex;flex-direction:column;justify-content:space-between}
.kpi-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.12em}
.kpi-value{font:760 26px/1 var(--mono);letter-spacing:-.02em}
.kpi-sub{color:var(--muted);font:11px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi.good .kpi-value,.good{color:var(--ok)}.kpi.warn .kpi-value,.warn{color:var(--warn)}.kpi.danger .kpi-value,.danger{color:var(--danger)}
.spark{height:38px;width:100%;display:block}
.workspace{flex:1;min-height:0;display:grid;grid-template-columns:minmax(310px,.95fr) minmax(390px,1.15fr) minmax(340px,.9fr);gap:12px}
.panel{min-height:0;border:1px solid var(--line);background:var(--panel);border-radius:8px;display:flex;flex-direction:column;overflow:hidden}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line);background:#121a23}
.panel-title{display:flex;align-items:center;gap:9px;min-width:0}
.panel-title strong{font-size:12px;text-transform:uppercase;letter-spacing:.1em}
.panel-title span{font:11px var(--mono);color:var(--muted)}
.count{min-width:28px;text-align:center;border:1px solid var(--line);border-radius:999px;padding:3px 8px;color:var(--soft);font:700 11px var(--mono);background:#0c1218}
.scroll{flex:1;min-height:0;overflow:auto}
.scroll::-webkit-scrollbar{width:8px;height:8px}.scroll::-webkit-scrollbar-thumb{background:#2a3745;border-radius:999px;border:2px solid var(--panel)}
.feed-row{display:grid;grid-template-columns:52px 94px minmax(0,1fr);align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid rgba(35,48,61,.72)}
.feed-row.has-copy{grid-template-columns:52px 94px minmax(0,1fr) 52px}
.feed-row:hover,.signal-row:hover,.pos-row:hover,.trade-row:hover{background:#151f2a}
.time{font:11px var(--mono);color:var(--muted)}
.badge{display:inline-flex;align-items:center;justify-content:center;min-width:72px;height:23px;padding:0 8px;border-radius:5px;font:800 10px var(--mono);letter-spacing:.05em;text-transform:uppercase;border:1px solid var(--line);background:#0b1117;color:var(--soft)}
.badge.info{color:var(--accent-2);border-color:#2d4566;background:#101827}
.badge.watch{color:var(--warn);border-color:#5a4728;background:#1b160e}
.badge.exec{color:var(--accent);border-color:#276857;background:#0d1e1a}
.badge.risk,.badge.reject{color:var(--danger);border-color:#5f2d31;background:#201114}
.feed-msg{min-width:0;color:#c9d2da;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.muted{color:var(--muted)}.mono{font-family:var(--mono)}
.signal-row{padding:13px 14px;border-bottom:1px solid rgba(35,48,61,.72)}
.sig-top{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:start}
.sig-name{display:flex;align-items:center;gap:9px;min-width:0}
.avatar{width:28px;height:28px;border-radius:7px;background:#182330;border:1px solid #2c3b4a;display:grid;place-items:center;color:var(--soft);font:800 11px var(--mono);overflow:hidden;flex:0 0 auto}
.avatar img{width:100%;height:100%;object-fit:cover}
.sig-symbol{font-weight:750;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sig-id{display:flex;align-items:center;gap:7px;min-width:0;margin-top:2px}
.sig-addr{font:10px var(--mono);color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
.ca-copy{height:22px;min-width:38px;padding:0 8px;border:1px solid #314252;border-radius:5px;background:#0b1219;color:#a8b3bd;font:800 10px var(--mono);letter-spacing:.04em;cursor:pointer;transition:background .16s ease,border-color .16s ease,color .16s ease,transform .12s ease}
.ca-copy:hover{border-color:#4d687b;color:#ecf2f7;background:#111b25}
.ca-copy:active{transform:translateY(1px)}
.ca-copy.copied{border-color:#276857;background:#0d1e1a;color:var(--accent)}
.ca-copy.copy-failed{border-color:#5f2d31;background:#201114;color:var(--danger)}
.sig-tags{display:flex;gap:6px;align-items:center;justify-content:flex-end;flex-wrap:wrap}
.metric-line{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
.mini{border-top:1px solid rgba(35,48,61,.7);padding-top:8px;min-width:0}
.mini b{display:block;font:700 11px var(--mono);color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mini span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}
.blocker{margin-top:10px;color:var(--warn);font:11px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rail{display:grid;grid-template-rows:1fr 1fr 190px;gap:12px;min-height:0}
.rail .panel{min-height:0}
.pos-row,.trade-row,.thought-row{padding:10px 12px;border-bottom:1px solid rgba(35,48,61,.72)}
.pos-head,.trade-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px}
.pnl{font:800 12px var(--mono)}.pos-meta,.trade-meta{display:flex;gap:10px;color:var(--muted);font:11px var(--mono);white-space:nowrap;overflow:hidden}
.thought-row{display:grid;grid-template-columns:50px minmax(0,1fr);gap:10px;color:#c8d3dc}
.thought-row div:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.empty{height:100%;min-height:96px;display:grid;place-items:center;color:#74818f;text-align:center;padding:24px}
.empty b{display:block;color:#b5c0ca;margin-bottom:4px;font-size:13px}
.footer{display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--muted);font:11px var(--mono);padding:0 2px}
#err-bar{display:none;border:1px solid #6a2c32;background:#211114;color:#f0a0a0;border-radius:8px;padding:9px 12px;font:12px var(--mono)}
@media (max-width:1100px){
  html,body{overflow:auto}.app-shell{min-height:100dvh;height:auto}
  .topbar,.kpi-grid,.workspace{grid-template-columns:1fr}.workspace,.rail{min-height:640px}
  .kpi-grid{grid-template-columns:repeat(2,1fr)}.rail{grid-template-rows:minmax(220px,1fr) minmax(220px,1fr) 190px}
}
</style>
</head><body>
<div class="app-shell">
  <header class="topbar">
    <div class="brand">
      <div class="mark">OX</div>
      <div>
        <h1>OXScan Command</h1>
        <p id="engine-status">Initializing scan engine</p>
      </div>
    </div>
    <div class="status-pack">
      <span class="pill"><span class="dot" id="status-dot"></span><span id="mode-label">DRY RUN</span></span>
      <span class="pill" id="clock">--:--:--</span>
    </div>
  </header>

  <section class="kpi-grid">
    <div class="kpi"><div class="kpi-label">Cycles</div><div id="st-cyc" class="kpi-value">0</div><div id="st-status" class="kpi-sub">Waiting</div></div>
    <div class="kpi"><div class="kpi-label">Signal Queue</div><div id="st-sig" class="kpi-value">0</div><div id="st-sig-sub" class="kpi-sub">0 exec / 0 watch</div></div>
    <div class="kpi danger"><div class="kpi-label">Risk Blocks</div><div id="st-risk" class="kpi-value">0</div><div id="st-risk-sub" class="kpi-sub">RUG / reject stream</div></div>
    <div class="kpi"><div class="kpi-label">Positions</div><div id="st-pos" class="kpi-value">0</div><div id="st-pos-sub" class="kpi-sub">Exposure idle</div></div>
    <div class="kpi" id="pnl-card"><div class="kpi-label">Session PnL</div><div id="st-pnl" class="kpi-value">0.0000</div><div id="st-pnl-sub" class="kpi-sub">SOL realized</div></div>
    <div class="kpi"><div class="kpi-label">PnL Curve</div><canvas id="pnl-chart" class="spark"></canvas><div id="chart-foot" class="kpi-sub">No trades yet</div></div>
  </section>

  <main class="workspace">
    <section class="panel">
      <div class="panel-head"><div class="panel-title"><strong>Event Ledger</strong><span>latest scan decisions</span></div><span class="count" id="feed-cnt">0</span></div>
      <div class="scroll" id="feed-list"></div>
    </section>
    <section class="panel">
      <div class="panel-head"><div class="panel-title"><strong>Signal Queue</strong><span>watch versus executable</span></div><span class="count" id="sig-cnt">0</span></div>
      <div class="scroll" id="sig-list"></div>
    </section>
    <aside class="rail">
      <section class="panel">
        <div class="panel-head"><div class="panel-title"><strong>Open Positions</strong><span>live risk</span></div><span class="count" id="pos-cnt">0</span></div>
        <div class="scroll" id="pos-list"></div>
      </section>
      <section class="panel">
        <div class="panel-head"><div class="panel-title"><strong>Trade History</strong><span id="st-wr">no closed trades</span></div><span class="count" id="trade-cnt">0</span></div>
        <div class="scroll" id="trade-list"></div>
      </section>
      <section class="panel">
        <div class="panel-head"><div class="panel-title"><strong id="soul-name">TraderSoul</strong><span id="soul-stage">loading</span></div></div>
        <div class="scroll" id="soul-thoughts"></div>
      </section>
    </aside>
  </main>

  <div id="err-bar"></div>
  <footer class="footer">
    <span>OXScan v1.0.4 | RugGate | LP Strict | MomentumDead | VolExhaust</span>
    <span id="session-stats"></span>
  </footer>
</div>
<script>
var lastSeq=0,pnlHistory=[],copiedCA='',copiedUntil=0;
function $(id){return document.getElementById(id)}
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function clean(v){return esc(v).replace(/[\\u2600-\\u27BF]|[\\uD83C-\\uDBFF][\\uDC00-\\uDFFF]/g,'').replace(/\\s+/g,' ').trim()}
function money(v){v=Number(v||0);if(!isFinite(v))v=0;return '$'+(Math.abs(v)>=1000?(v/1000).toFixed(1)+'K':v.toFixed(0))}
function num(v,d){v=Number(v||0);return isFinite(v)?v.toFixed(d||0):'0'}
function shortAddr(a){a=String(a||'');return a.length>14?a.slice(0,6)+'...'+a.slice(-6):a}
function badgeClass(t, executable){if(t==='RUG_RISK'||t==='DEV_SELL'||t==='WASH_SUSPECT'||t==='REJECTED')return 'risk';if(executable)return 'exec';if(t==='SCALP'||t==='MINIMUM'||t==='STRONG')return 'watch';return 'info'}
function badge(label, cls){return '<span class="badge '+cls+'">'+esc(label||'INFO')+'</span>'}
function caCopyButton(addr){addr=String(addr||'');if(!addr)return '';var ok=copiedCA===addr&&Date.now()<copiedUntil;return '<button type="button" class="ca-copy '+(ok?'copied':'')+'" data-copy-ca="'+esc(addr)+'" title="Copy token CA" aria-label="Copy token CA">'+(ok?'COPIED':'CA')+'</button>'}
function fallbackCopyText(value){
  return new Promise(function(resolve,reject){
    var ta=document.createElement('textarea');ta.value=value;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.left='-9999px';ta.style.opacity='0';document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy')?resolve():reject(new Error('copy failed'))}catch(e){reject(e)}finally{document.body.removeChild(ta)}
  });
}
function copyText(value){
  if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(value).catch(function(){return fallbackCopyText(value)});
  return fallbackCopyText(value);
}
function wireCopyButtons(){
  document.addEventListener('click',function(e){
    var btn=e.target.closest&&e.target.closest('[data-copy-ca]');if(!btn)return;
    var addr=btn.getAttribute('data-copy-ca')||'';if(!addr)return;
    copyText(addr).then(function(){copiedCA=addr;copiedUntil=Date.now()+1400;btn.classList.remove('copy-failed');btn.classList.add('copied');btn.textContent='COPIED'}).catch(function(){btn.classList.add('copy-failed');btn.textContent='FAIL'});
  });
}
function empty(title, body){return '<div class="empty"><div><b>'+esc(title)+'</b><span>'+esc(body)+'</span></div></div>'}
function updateSoul(s){
  if(!s)return;
  $('soul-name').textContent=s.name||'TraderSoul';
  $('soul-stage').textContent=(s.stage||'Novice')+' | '+(s.trades||0)+' trades | WR '+num((s.win_rate||0)*100,0)+'%';
}
function updateSoulThoughts(refs){
  refs=refs||[];
  if(!refs.length){$('soul-thoughts').innerHTML=empty('No reflections yet','Signals will populate this rail.');return}
  $('soul-thoughts').innerHTML=refs.slice(0,8).map(function(r){
    return '<div class="thought-row"><div class="time">'+esc(r.t||'')+'</div><div>'+clean(r.msg||'')+'</div></div>';
  }).join('');
}
function updateSessionStats(s){
  if(!s||!s.tier_stats){$('session-stats').textContent='No tier history';return}
  var out=[],ts=s.tier_stats;
  Object.keys(ts).forEach(function(t){var d=ts[t];out.push(t+' '+num((d.rate||0)*100,0)+'%/'+(d.n||0))});
  out.push('W '+(s.wins||0)+' L '+(s.losses||0));
  $('session-stats').textContent=out.join(' | ');
}
function renderFeed(items){
  items=items||[];
  if(!items.length){$('feed-list').innerHTML=empty('Waiting for scanner output','The ledger updates every polling cycle.');return}
  $('feed-list').innerHTML=items.map(function(r){
    if(r.sep){return '<div class="feed-row"><div class="time">'+esc(r.t||'')+'</div>'+badge('CYCLE','info')+'<div class="feed-msg">Cycle '+esc(r.cycle)+' | '+(r.hot?'HOT':'NORMAL')+'</div></div>'}
    if(r.sym_note){return '<div class="feed-row"><div class="time">'+esc(r.t||'')+'</div>'+badge('SYSTEM','info')+'<div class="feed-msg">'+clean(r.msg||'')+'</div></div>'}
    var cls=badgeClass(r.tier,r.executable),msg=clean(r.symbol||'');
    if(r.reject_reason)msg+=' | '+clean(r.reject_reason);
    else if(r.execution_blocked)msg+=' | WATCH '+clean(r.execution_blocked);
    else if(r.sig_a_ratio)msg+=' | A '+num(r.sig_a_ratio,2)+'x / C '+num(r.ratio_c,2)+'x';
    if(r.mc)msg+=' | MC '+money(r.mc);
    if(r.confidence)msg+=' | Cnf '+num(r.confidence,0);
    return '<div class="feed-row '+(r.addr?'has-copy':'')+'"><div class="time">'+esc(r.t||'')+'</div>'+badge(r.tier||'INFO',cls)+'<div class="feed-msg">'+msg+'</div>'+caCopyButton(r.addr)+'</div>';
  }).join('');
}
function renderSignals(sigs){
  sigs=sigs||[];
  $('sig-cnt').textContent=sigs.length;
  if(!sigs.length){$('sig-list').innerHTML=empty('No active signal candidates','WATCH and EXEC rows appear after scan qualification.');return}
  $('sig-list').innerHTML=sigs.map(function(s){
    var exec=!!s.executable,cls=badgeClass(s.tier,exec),logo=s.logo?'<img src="'+esc(s.logo)+'" alt="">':esc((s.symbol||'?').slice(0,2).toUpperCase());
    var blocker=s.execution_blocked?'<div class="blocker">'+clean(s.execution_blocked)+'</div>':'';
    return '<div class="signal-row"><div class="sig-top"><div class="sig-name"><div class="avatar">'+logo+'</div><div><div class="sig-symbol">'+clean(s.symbol||'?')+'</div><div class="sig-id"><div class="sig-addr">'+esc(shortAddr(s.addr))+'</div>'+caCopyButton(s.addr)+'</div></div></div><div class="sig-tags">'+badge(s.tier,cls)+badge(exec?'EXEC':'WATCH',exec?'exec':'watch')+'</div></div>'
      +'<div class="metric-line"><div class="mini"><span>Market Cap</span><b>'+money(s.mc)+'</b></div><div class="mini"><span>Age</span><b>'+num(s.age_m,1)+'m</b></div><div class="mini"><span>Confidence</span><b>'+num(s.confidence,0)+'</b></div><div class="mini"><span>Flow</span><b>A '+num(s.sig_a_ratio,2)+'x</b></div></div>'+blocker+'</div>';
  }).join('');
}
function renderPositions(pos){
  pos=pos||{};var keys=Object.keys(pos);$('pos-cnt').textContent=keys.length;
  if(!keys.length){$('pos-list').innerHTML=empty('No open positions','Dry-run or entry gate may be blocking buys.');return}
  $('pos-list').innerHTML=keys.map(function(addr){
    var p=pos[addr],pct=Number(p.pnl_pct||0),cls=pct>=0?'good':'danger',elapsed=((Date.now()/1000-(p.entry_ts||Date.now()/1000))/60);
    return '<div class="pos-row"><div class="pos-head"><strong>'+clean(p.symbol||'?')+'</strong><span class="pnl '+cls+'">'+num(pct,1)+'%</span></div><div class="pos-meta"><span>'+num(p.sol_in,3)+' SOL</span><span>T+'+num(elapsed,1)+'m</span><span>rem '+num((p.remaining||1)*100,0)+'%</span></div></div>';
  }).join('');
}
function renderTrades(trades){
  trades=trades||[];$('trade-cnt').textContent=trades.length;
  if(!trades.length){$('trade-list').innerHTML=empty('No closed trades','Realized outcomes will land here.');return}
  $('trade-list').innerHTML=trades.map(function(t){
    var pct=Number(t.pnl_pct||0),cls=pct>=0?'good':'danger';
    return '<div class="trade-row"><div class="trade-head"><strong>'+clean(t.symbol||'?')+'</strong><span class="pnl '+cls+'">'+num(pct,1)+'%</span></div><div class="trade-meta"><span>'+clean(t.tier||'')+'</span><span>'+clean(t.reason||'')+'</span></div></div>';
  }).join('');
}
function drawPnl(){
  var c=$('pnl-chart');if(!c||!pnlHistory.length)return;
  var ctx=c.getContext('2d'),W=c.width=c.offsetWidth,H=c.height=c.offsetHeight;
  ctx.clearRect(0,0,W,H);
  var vals=pnlHistory.slice(-90),mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals),range=mx-mn||0.001;
  ctx.strokeStyle='#263443';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(0,H-1);ctx.lineTo(W,H-1);ctx.stroke();
  ctx.beginPath();ctx.strokeStyle=vals[vals.length-1]>=0?'#55c6a9':'#e06161';ctx.lineWidth=2;
  vals.forEach(function(v,i){var x=vals.length===1?0:i/(vals.length-1)*W,y=H-(v-mn)/range*(H-6)-3;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
  ctx.stroke();$('chart-foot').textContent=num(vals[vals.length-1],4)+' SOL';
}
async function poll(){
  try{
    var r=await fetch('/api/state'),d=await r.json(),stats=d.stats||{},feed=d.feed||[],signals=d.signals||[],positions=d.positions||{},trades=d.trades||[];
    var exec=signals.filter(function(s){return s.executable}).length,watch=signals.length-exec;
    var risk=feed.filter(function(x){return x.tier==='RUG_RISK'||x.tier==='DEV_SELL'||x.tier==='WASH_SUSPECT'}).length;
    var rejects=feed.filter(function(x){return x.tier==='REJECTED'}).length;
    $('st-cyc').textContent=d.cycle||0;$('st-status').textContent=clean(d.status||'Running');$('engine-status').textContent=clean(d.status||'Running');
    $('st-sig').textContent=signals.length;$('st-sig-sub').textContent=exec+' exec / '+watch+' watch';
    $('st-risk').textContent=risk;$('st-risk-sub').textContent=rejects+' rejects in ledger';
    $('st-pos').textContent=Object.keys(positions).length;$('st-pos-sub').textContent='Max positions governed';
    var pnl=Number(stats.net_sol||0);$('st-pnl').textContent=(pnl>=0?'+':'')+num(pnl,4);$('pnl-card').className='kpi '+(pnl>=0?'good':'danger');
    var wins=stats.wins||0,losses=stats.losses||0,total=wins+losses;$('st-wr').textContent=total?'WR '+num(wins/total*100,0)+'%':'no closed trades';
    $('feed-cnt').textContent=feed.length;renderFeed(feed.slice(0,120));renderSignals(signals.slice(0,60));renderPositions(positions);renderTrades(trades.slice(0,60));
    if(d.soul){updateSoul(d.soul);updateSoulThoughts(d.soul.reflections);updateSessionStats(d.soul)}
    pnlHistory.push(pnl);if(pnlHistory.length>180)pnlHistory.shift();drawPnl();
    $('mode-label').textContent='DRY RUN';$('status-dot').className='dot';$('clock').textContent=new Date().toLocaleTimeString();
    var eb=$('err-bar');eb.style.display='none';
  }catch(e){
    var eb=$('err-bar');eb.textContent='poll error: '+(e&&e.message?e.message:e);eb.style.display='block';$('status-dot').className='dot warn';
  }
}
wireCopyButtons();setInterval(poll,2000);poll();
</script></body></html>"""

class DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
        elif self.path == "/api/state":
            with state_lock: snap = json.loads(json.dumps(state, ensure_ascii=False))
            snap["soul"] = soul_summary()
            self._json(snap)
        else:
            self.send_error(404)


def run_dashboard():
    import socket, subprocess
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind((DASHBOARD_HOST, DASHBOARD_PORT))
        probe.close()
    except OSError:
        print(f"  ⚠️  {DASHBOARD_HOST}:{DASHBOARD_PORT} busy — killing...")
        subprocess.run(f"lsof -ti:{DASHBOARD_PORT} | xargs kill -9", shell=True, capture_output=True)
        time.sleep(1.5)

    HTTPServer.allow_reuse_address = True
    try:
        server = HTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashHandler)
    except OSError as e:
        print(f"  ❌ Dashboard bind failed: {e}")
        raise
    server.serve_forever()
```

---

## 第十五部分：入口点

```python
if __name__ == "__main__":
    dashboard_host = "localhost" if DASHBOARD_HOST == "127.0.0.1" else DASHBOARD_HOST
    dashboard_url = f"http://{dashboard_host}:{DASHBOARD_PORT}"
    print("=" * 60)
    print("  扫链策略 Live Bot v1.0.4")
    print("  Anti-rug: LP Strict | Bundle 22% | Age 6min | Cooldown 10min")
    print("  Exit: DynTrail 8-20% | MomentumDead | VolExhaust | TP2 45%")
    print(f"  Wallet: {WALLET_ADDRESS[:8]}...{WALLET_ADDRESS[-4:]}")
    print(f"  Dashboard: {dashboard_url}")
    print(f"  Live trading: {'ENABLED' if ENABLE_LIVE_TRADING else 'DRY RUN / observation mode'}")
    print(f"  Max exposure: {MAX_SOL} SOL | Max positions: {MAX_POSITIONS}")
    print("=" * 60)

    load_on_startup()
    load_soul()

    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

    print(f"  scanner_loop started (every {LOOP_SEC}s)")
    print(f"  monitor_loop started (every {MONITOR_SEC}s)")

    push_feed({"sym_note": True,
               "msg": (f"🟢 Bot v1.0.4 started — Soul: {soul.get('name','...')} [{soul.get('stage','Novice')}]  "
                       f"SigA:{SIG_A_THRESHOLD}  BS:{BS_MIN}  MC>${MC_MIN/1000:.0f}K-${MC_CAP/1000:.0f}K  "
                       f"Age≥{AGE_HARD_MIN}s  Bundle≤{BUNDLE_ATH_PCT_MAX}%  LP Strict:{LP_LOCK_STRICT}  "
                       f"Monitor:{MONITOR_SEC}s  TP2:{TP2_PCT*100:.0f}%  "
                       f"LiveTrading:{ENABLE_LIVE_TRADING}"),
               "t": time.strftime("%H:%M:%S")})

    print(f"  Dashboard → {dashboard_url}")
    try:
        run_dashboard()
    except KeyboardInterrupt:
        print("\n  Bot stopped.")
```

---

## 第十六部分：部署检查清单

```bash
# 1. 安装依赖
pip install requests solders base58

# 2. 设置环境变量
export OKX_API_KEY="..."
export OKX_SECRET_KEY="..."
export OKX_PASSPHRASE="..."
export WALLET_PRIVATE_KEY="..."

# 3. 测试 API 连接
python3 -c "from scan_live import token_ranking; print(token_ranking(5)[:1])"

# 4. 小仓位测试（强烈建议先用）
# SOL_PER_TRADE = {"SCALP": 0.001, "MINIMUM": 0.001, "STRONG": 0.001}

# 5. 后台运行
[ -f ~/.dacs_env_profile ] && source ~/.dacs_env_profile
nohup python3 scan_live.py > bot.log 2>&1 &

# 6. 查看日志
tail -f bot.log

# 7. 访问 Dashboard
open http://localhost:3241

# 8. 停止
pkill -f scan_live.py
```
