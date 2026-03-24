---
name: scan-live-v1
description: >
  扫链策略 Live Bot v1.0 — 独立 Python 自动交易机器人（非 MCP 版）。
  v1.0: 防 rug 强化（LP严格验证/Bundle/Age/冷却表）/
  动态卖点（TP2 45%/动态Trailing 8-20%）/ 新增动量死亡与量能枯竭检测。
  TraderSoul READ-ONLY 分析系统保留。

version: 1.0.0
validated: false
validation_date: 2026-03-24
validation_results: >
  v1.0: LP_LOCK_STRICT=True, BUNDLE_ATH 30→22%, AGE_HARD_MIN 240→360s,
  TOP10_HOLD 40→33%, APED_WALLET 10→6, DEV_HOLD 15→10%, RUG_RATE 30→20%,
  MIN_HOLDERS 25→35, MONITOR_SEC 5→3s. Exit: TP2 25→45%, TP1_SELL SCALP 60→50%,
  dynamic trailing 8-20%, momentum death detection, volume exhaustion detection,
  rejected token 10min cooldown cache.

---

# 扫链策略 V1.0

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
[ -f ~/.dacs_env_profile ] && source ~/.dacs_env_profile
PYTHONUNBUFFERED=1 nohup python3 scan_live.py > bot.log 2>&1 &
BOT_PID=$!
echo "✅ Bot started (PID $BOT_PID)"
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

# ── API ───────────────────────────────────────────────────────────────────────
API_BASE   = "https://web3.okx.com"
API_KEY    = os.environ["OKX_API_KEY"]
SECRET_KEY = os.environ["OKX_SECRET_KEY"]
PASSPHRASE = os.environ["OKX_PASSPHRASE"]

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

def _get(path: str, params: dict = {}) -> dict:
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    qs  = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
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

WALLET_PRIVATE_KEY = os.environ["WALLET_PRIVATE_KEY"]

def get_keypair() -> Keypair:
    raw = base58.b58decode(WALLET_PRIVATE_KEY)
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
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, ensure_ascii=False)

def save_trades():
    with state_lock:
        with open(TRADES_FILE, "w") as f:
            json.dump(state["trades"], f, ensure_ascii=False)

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
            "min_confidence_trust": 50,
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
        with open(SOUL_FILE, "w") as f:
            json.dump(soul, f, ensure_ascii=False, indent=2)
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
        "dev_death_rate": 0.0, "warnings": []
    }

    try:
        details = memepump_token_details(addr)
        result["audit_score"] = float(details.get("auditScore", details.get("score", -1)))
        raw_lp = float(details.get("lpLockedPercent", details.get("lpLockPercent", -1)))
        if raw_lp >= 0:
            result["lp_pct"] = raw_lp if raw_lp <= 1 else raw_lp / 100
        result["lp_burned"] = bool(details.get("lpBurned", details.get("isLpBurned", False)))
    except Exception as e:
        result["warnings"].append(f"tokenDetails: {e}")

    try:
        dev_info = token_dev_info(addr)
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
        result["bundle_ath"] = float(bundle_info.get("bundlerAthPercent", 0))
    except Exception as e:
        result["warnings"].append(f"bundleInfo: {e}")

    try:
        aped = memepump_aped_wallet(addr)
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

    live      = candles[0]
    live_drop = (float(live[4]) - float(live[1])) / max(float(live[1]), 1e-12) * 100
    if live_drop <= -30:
        push_feed({"symbol": sym, "tier": "REJECTED", "reject_reason": f"DUMP {live_drop:.0f}%", "t": now})
        return {"symbol": sym, "addr": addr, "tier": "DEV_SELL", "t": now}

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
    conf = min(conf, 100)

    entry_price = float(candles[0][4])

    return {
        "symbol": sym, "addr": addr, "tier": tier, "launch": launch_type,
        "sig_a": sig_a, "sig_a_ratio": round(signal_a_ratio, 2),
        "sig_b": sig_b, "sig_b_ratio": round(sig_b_ratio, 2),
        "sig_c": sig_c, "ratio_c": round(ratio_c, 2),
        "entry": entry_price,
        "mc": mc_est,
        "age_m": round(token["_age"] / 60, 1),
        "confidence": conf,
        "near_migration": near_migration,
        "stairstep": stairstep,
        "t": now,
    }
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
<title>OXScan — Live Bot v1.0</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{font-family:'Courier New','SF Mono',monospace;background:#000408;color:#7ab8c8;font-size:12px;display:flex;flex-direction:column}
body::after{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,229,255,0.018) 3px,rgba(0,229,255,0.018) 4px);pointer-events:none;z-index:9999}
.stats-bar{display:flex;align-items:stretch;background:#000810;border-bottom:1px solid #0a2a3a;flex-shrink:0;min-height:88px}
.stat-cell{padding:11px 18px;border-right:1px solid #0a2030;display:flex;flex-direction:column;justify-content:center;min-width:110px}
.stat-cell.pnl-cell{min-width:175px}
.stat-cell.chart-cell{flex:1;padding:10px 14px}
.stat-lbl{font-size:10px;color:#1a6070;text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px}
.stat-big{font-size:26px;font-weight:800;line-height:1}
.stat-sub{font-size:10px;color:#1a5060;margin-top:4px}
.c-red{color:#ff3300;text-shadow:0 0 8px rgba(255,51,0,.6)}
.c-grn{color:#00e5ff;text-shadow:0 0 8px rgba(0,229,255,.5)}
.c-pur{color:#a040ff;text-shadow:0 0 6px rgba(160,64,255,.4)}
.c-yel{color:#00e5ff}.c-blu{color:#40f0ff}.c-ora{color:#ff8c00;text-shadow:0 0 6px rgba(255,140,0,.4)}
.chart-meta{display:flex;gap:14px;font-size:11px;color:#1a5060;margin-bottom:5px}
.chart-meta .v{color:#7ab8c8}.chart-meta .vg{color:#00e5ff}.chart-meta .vr{color:#ff3300}
#pnl-chart{width:100%;height:40px}
.chart-foot{font-size:10px;color:#1a5060;margin-top:3px}
.chart-foot .vg{color:#00e5ff}.chart-foot .vr{color:#ff3300}
.soul-bar{display:flex;align-items:center;gap:10px;padding:5px 14px;background:#000810;border-bottom:1px solid #061828;font-size:11px;flex-shrink:0;height:28px;overflow:hidden}
.s-name{color:#00e5ff;font-weight:700;white-space:nowrap;text-shadow:0 0 8px rgba(0,229,255,.6)}
.s-stage{color:#1a4a5a}.s-sep{color:#0a2030;flex-shrink:0}
.s-phil{color:#0d3040;font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.soul-thoughts{background:#000810;border-bottom:1px solid #0a2a3a;flex-shrink:0;height:100px;overflow:hidden;position:relative}
.st-hdr{display:flex;align-items:center;gap:6px;padding:3px 14px 0;font-size:9px;color:#00e5ff;text-transform:uppercase;letter-spacing:.1em;text-shadow:0 0 6px rgba(0,229,255,.4)}
.st-hdr .st-dot{width:6px;height:6px;border-radius:50%;background:#00e5ff;box-shadow:0 0 6px #00e5ff;animation:stpulse 2s infinite}
@keyframes stpulse{0%,100%{opacity:.3}50%{opacity:1}}
.st-list{padding:1px 14px 4px;overflow:hidden;height:82px}
.st-row{display:flex;align-items:baseline;gap:6px;line-height:1.6;white-space:nowrap;overflow:hidden}
.st-time{color:#1a6070;font-size:9px;flex-shrink:0}.st-msg{color:#a0d4b8;font-size:10px;overflow:hidden;text-overflow:ellipsis}
.session-stats{background:#000408;border-bottom:1px solid #061420;flex-shrink:0;height:24px;display:flex;align-items:center;gap:6px;padding:0 14px;overflow:hidden}
.ss-pill{font-size:10px;color:#1a4a5a;white-space:nowrap}.ss-pill b{color:#00b8cc;font-weight:600}
.prog{height:2px;background:#0a2030;flex-shrink:0}
.prog-bar{height:2px;background:#00e5ff;box-shadow:0 0 6px #00e5ff;width:0;transition:width 2s linear}
.col-hdr{display:flex;align-items:center;justify-content:space-between;padding:6px 12px;background:#000c18;border-bottom:1px solid #0a2a3a;font-size:11px;font-weight:700;color:#00e5ff;text-shadow:0 0 8px rgba(0,229,255,.5);letter-spacing:.06em;text-transform:uppercase;flex-shrink:0}
.cnt{background:#001a28;color:#00b8cc;border:1px solid #0a3040;border-radius:3px;padding:1px 7px;font-size:10px;font-weight:600}
.main{flex:1;display:grid;grid-template-columns:290px 360px 1fr;overflow:hidden;min-height:0}
.col{display:flex;flex-direction:column;border-right:1px solid #0a2030;overflow:hidden;min-height:0}
.col.no-border{border-right:none}
.scr{flex:1;overflow-y:auto;overflow-x:hidden;min-height:0}
.scr::-webkit-scrollbar{width:3px}
.scr::-webkit-scrollbar-thumb{background:#0a2a3a;border-radius:0}
.frow{display:flex;align-items:center;gap:6px;padding:3px 10px;border-bottom:1px solid #00080f;min-height:22px}
.frow:hover{background:#000c18;border-left:2px solid #00e5ff}
.ftime{color:#1a4a5a;font-size:10px;flex-shrink:0;width:46px}
.fbadge{font-size:9px;font-weight:700;padding:1px 5px;border-radius:2px;flex-shrink:0;text-transform:uppercase;letter-spacing:.05em}
.fb-skip{background:#040c10;color:#1a5060;border:1px solid #061820}
.fb-buy{background:#001a10;color:#ff8c00;border:1px solid #1a3010;box-shadow:0 0 5px rgba(255,140,0,.2)}
.fb-sell{background:#140400;color:#00e5ff;border:1px solid #0a1a20}
.fb-safe{background:#0a0818;color:#8060ff;border:1px solid #100820}
.fb-info{background:#000c18;color:#00b8cc;border:1px solid #061828}
.fmsg{color:#2a7080;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.scard{padding:9px 12px;border-bottom:1px solid #061420;cursor:default;border-left:2px solid transparent;transition:border-left-color .15s}
.scard:hover{background:#000c18;border-left-color:#00e5ff}
.shead{display:flex;align-items:center;gap:6px;margin-bottom:5px}
.sname{font-weight:700;font-size:13px;color:#b0eeff;text-shadow:0 0 6px rgba(176,238,255,.3)}
.tier{font-size:10px;font-weight:700;padding:2px 8px;border-radius:2px;text-transform:uppercase;letter-spacing:.06em}
.t-scalp{background:#001828;color:#40a0b8;border:1px solid #0a3040}
.t-minimum{background:#00101e;color:#00d4f0;border:1px solid #005068;text-shadow:0 0 6px rgba(0,212,240,.5)}
.t-strong{background:#001428;color:#00f0ff;border:1px solid #0070a0;box-shadow:0 0 8px rgba(0,240,255,.25);text-shadow:0 0 8px rgba(0,240,255,.8)}
.stime{color:#1a3a4a;font-size:10px;margin-left:auto}
.sr1{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;color:#2a6070;margin-bottom:3px}
.sr1 .mc{color:#7ab8c8;font-weight:600}.sr1 .tp{color:#00e5ff}.sr1 .s1{color:#ff3300}
.sr2{display:flex;gap:8px;font-size:10px;color:#1a4050;margin-bottom:3px}
.saddr{font-size:9px;color:#0a2030;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rcol{display:flex;flex-direction:column;overflow:hidden;border-right:none}
.pos-sec{flex:0 0 auto;max-height:44%;display:flex;flex-direction:column;overflow:hidden;border-bottom:1px solid #0a2030}
.pos-sec .scr{flex:1}
.pcard{padding:8px 12px;border-bottom:1px solid #061420;border-left:2px solid #0a2030}
.pcard.in-profit{border-left-color:#00e5ff}.pcard.in-loss{border-left-color:#ff3300}
.phead{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.pname{font-weight:700;font-size:13px;color:#b0eeff}
.ppnl{font-size:12px;font-weight:700;margin-left:auto}
.prow{display:flex;gap:12px;font-size:10px;color:#2a6070}
.prow .hi{color:#00e5ff}.prow .lo{color:#ff3300}
.tcard{padding:6px 12px;border-bottom:1px solid #061420;display:flex;align-items:center;gap:8px}
.tcard .sym{color:#7ab8c8;font-weight:700;font-size:12px;min-width:70px}
.tcard .pnl{font-size:11px;font-weight:700}
.tcard .meta{font-size:10px;color:#1a4050;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.sidebar{background:#000408;border-top:1px solid #0a2030;padding:8px 12px;font-size:10px;color:#0a2a3a;flex-shrink:0}
.sidebar span{color:#1a5060}
#err-bar{display:none;background:#1a0000;color:#ff3300;padding:4px 12px;font-size:10px;flex-shrink:0}
</style>
</head><body>
<div class="stats-bar">
  <div class="stat-cell"><div class="stat-lbl">Cycle</div><div id="st-cyc" class="stat-big c-blu">0</div><div id="st-status" class="stat-sub"></div></div>
  <div class="stat-cell"><div class="stat-lbl">Positions</div><div id="st-pos" class="stat-big c-yel">0</div><div id="st-pos-sub" class="stat-sub"></div></div>
  <div class="stat-cell"><div class="stat-lbl">Trades</div><div id="st-trades" class="stat-big c-pur">0</div><div id="st-wr" class="stat-sub"></div></div>
  <div class="stat-cell pnl-cell"><div class="stat-lbl">Session PnL</div><div id="st-pnl" class="stat-big">0</div><div id="st-pnl-sub" class="stat-sub"></div></div>
  <div class="stat-cell chart-cell"><div class="chart-meta"><span>PnL curve</span></div><canvas id="pnl-chart"></canvas><div class="chart-foot" id="chart-foot"></div></div>
</div>
<div class="soul-bar">
  <span class="s-name" id="soul-name">...</span>
  <span class="s-stage" id="soul-stage"></span>
  <span class="s-sep">|</span>
  <span class="s-phil" id="soul-phil"></span>
</div>
<div class="soul-thoughts">
  <div class="st-hdr"><span class="st-dot"></span> Soul Thoughts</div>
  <div class="st-list" id="soul-thoughts"></div>
</div>
<div class="session-stats" id="session-stats"></div>
<div class="prog"><div class="prog-bar" id="prog-bar"></div></div>
<div class="main">
  <div class="col">
    <div class="col-hdr">Live Feed <span class="cnt" id="feed-cnt">0</span></div>
    <div class="scr" id="feed-list"></div>
  </div>
  <div class="col">
    <div class="col-hdr">Signals <span class="cnt" id="sig-cnt">0</span></div>
    <div class="scr" id="sig-list"></div>
  </div>
  <div class="rcol col no-border">
    <div class="pos-sec">
      <div class="col-hdr">Open Positions <span class="cnt" id="pos-cnt">0</span></div>
      <div class="scr" id="pos-list"></div>
    </div>
    <div class="col-hdr">Trade History <span class="cnt" id="trade-cnt">0</span></div>
    <div class="scr" id="trade-list"></div>
  </div>
</div>
<div id="err-bar"></div>
<div class="sidebar">OXScan v1.0 — <span>Soul READ-ONLY</span> | DynTrail | MomentumDead | VolExhaust | LP Strict</div>
<script>
var lastSeq=0,pnlHistory=[];
function $(id){return document.getElementById(id)}
function updateSoul(s){
  if(!s)return;
  $('soul-name').textContent='🧠 '+s.name;
  $('soul-stage').textContent=s.stage+' | '+s.trades+' trades | WR '+(s.win_rate*100).toFixed(0)+'% | '+s.vibe;
  $('soul-phil').textContent=s.win_philosophy;
}
function updateSoulThoughts(refs){
  if(!refs)return;
  var h='';
  refs.forEach(function(r){h+='<div class="st-row"><span class="st-time">'+r.t+'</span><span class="st-msg">'+r.msg+'</span></div>';});
  $('soul-thoughts').innerHTML=h;
}
function updateSessionStats(s){
  if(!s||!s.tier_stats)return;
  var h='',ts=s.tier_stats;
  for(var t in ts){var d=ts[t];h+='<span class="ss-pill"><b>'+t+'</b> '+(d.rate*100).toFixed(0)+'% ('+d.n+')</span> ';}
  if(s.losses!==undefined)h+='<span class="ss-pill">W:<b>'+s.wins+'</b> L:<b>'+s.losses+'</b></span>';
  $('session-stats').innerHTML=h;
}
function renderFeed(items){
  var h='';
  items.forEach(function(r){
    if(r.sep){h+='<div class="frow" style="background:#000810;border-left:2px solid #0a3040"><span class="ftime">'+r.t+'</span><span class="fbadge fb-info">'+(r.hot?'🌶️HOT':'❄️')+'</span><span class="fmsg" style="color:#1a4050">── Cycle '+r.cycle+' ──</span></div>';return;}
    if(r.sym_note){h+='<div class="frow"><span class="ftime">'+r.t+'</span><span class="fbadge fb-info">INFO</span><span class="fmsg">'+r.msg+'</span></div>';return;}
    var tier=r.tier||'',badge='fb-skip';
    if(tier==='SCALP'||tier==='MINIMUM'||tier==='STRONG')badge='fb-buy';
    else if(tier==='REJECTED'||tier==='DEV_SELL'||tier==='WASH_SUSPECT')badge='fb-safe';
    var msg=r.symbol||'';
    if(r.reject_reason)msg+=' '+r.reject_reason;
    else if(r.sig_a_ratio)msg+=' A:'+r.sig_a_ratio+'× C:'+r.ratio_c+'×';
    if(r.mc)msg+=' MC$'+(r.mc/1000).toFixed(1)+'K';
    if(r.confidence)msg+=' ['+r.confidence+']';
    h+='<div class="frow"><span class="ftime">'+(r.t||'')+'</span><span class="fbadge '+badge+'">'+tier+'</span><span class="fmsg">'+msg+'</span></div>';
  });
  $('feed-list').innerHTML=h;
}
function renderSignals(sigs){
  var h='';
  sigs.forEach(function(s){
    var tc='t-scalp';if(s.tier==='MINIMUM')tc='t-minimum';if(s.tier==='STRONG')tc='t-strong';
    var logo=s.logo?'<img src="'+s.logo+'" style="width:18px;height:18px;border-radius:50%;vertical-align:middle"> ':'';
    var mig=s.near_migration?'<span style="color:#ff8c00;font-size:9px">🔥MIGR</span>':'';
    var conf=s.confidence?(s.confidence>=70?'<span style="color:#00e5ff">['+s.confidence+'⭐]</span>':'<span style="color:#1a5060">['+s.confidence+']</span>'):'';
    h+='<div class="scard"><div class="shead">'+logo+'<span class="sname">'+s.symbol+'</span><span class="tier '+tc+'">'+s.tier+'</span>'+mig+conf+'<span class="stime">'+s.t+'</span></div>';
    h+='<div class="sr1"><span class="mc">MC $'+(s.mc/1000).toFixed(1)+'K</span><span class="tp">TP1 $'+(s.tp1_mc/1000).toFixed(1)+'K</span><span class="tp">TP2 $'+(s.tp2_mc/1000).toFixed(1)+'K</span><span class="s1">S1 $'+(s.s1_mc/1000).toFixed(1)+'K</span></div>';
    h+='<div class="sr2">A:'+s.sig_a_ratio+'× B:'+(s.sig_b_ratio||0).toFixed(1)+'× C:'+s.ratio_c+'× age:'+s.age_m+'m</div>';
    h+='<div class="saddr">'+s.addr+'</div></div>';
  });
  $('sig-list').innerHTML=h;$('sig-cnt').textContent=sigs.length;
}
function renderPositions(pos){
  var keys=Object.keys(pos);$('pos-cnt').textContent=keys.length;
  var h='';
  keys.forEach(function(addr){
    var p=pos[addr],pct=p.pnl_pct||0,cls=pct>=0?'in-profit':'in-loss';
    var mig=p.near_migration?'<span style="color:#ff8c00;font-size:9px;margin-left:4px">🔥</span>':'';
    var logo=p.logo?'<img src="'+p.logo+'" style="width:16px;height:16px;border-radius:50%;vertical-align:middle"> ':'';
    h+='<div class="pcard '+cls+'"><div class="phead">'+logo+'<span class="pname">'+p.symbol+'</span><span class="tier t-'+(p.tier||'scalp').toLowerCase()+'" style="font-size:9px">'+p.tier+'</span>'+mig+'<span class="ppnl '+(pct>=0?'c-grn':'c-red')+'">'+pct.toFixed(1)+'%</span></div>';
    var elapsed=((Date.now()/1000-p.entry_ts)/60).toFixed(1);
    h+='<div class="prow">'+p.sol_in+' SOL | T+'+elapsed+'m | rem:'+((p.remaining||1)*100).toFixed(0)+'%'+(p.tp1_hit?' <span class="hi">TP1✓</span>':'')+(p.stuck?' <span class="lo">STUCK</span>':'')+'</div></div>';
  });
  $('pos-list').innerHTML=h;
}
function renderTrades(trades){
  $('trade-cnt').textContent=trades.length;
  var h='';
  trades.forEach(function(t){
    var cls=t.pnl_pct>=0?'c-grn':'c-red';
    h+='<div class="tcard"><span class="sym">'+t.symbol+'</span><span class="pnl '+cls+'">'+t.pnl_pct.toFixed(1)+'%</span><span class="meta">'+t.tier+' | '+t.reason+'</span></div>';
  });
  $('trade-list').innerHTML=h;
}
function drawPnl(){
  var c=$('pnl-chart');if(!c||!pnlHistory.length)return;
  var ctx=c.getContext('2d'),W=c.width=c.offsetWidth,H=c.height=c.offsetHeight;
  ctx.clearRect(0,0,W,H);
  var vals=pnlHistory.slice(-60),mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals),range=mx-mn||0.001;
  ctx.beginPath();ctx.strokeStyle='#00e5ff';ctx.lineWidth=1.5;
  vals.forEach(function(v,i){var x=i/(vals.length-1)*W,y=H-(v-mn)/range*H;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);});
  ctx.stroke();
  var last=vals[vals.length-1];
  $('chart-foot').innerHTML='<span class="'+(last>=0?'vg':'vr')+'">'+last.toFixed(4)+' SOL</span>';
}
async function poll(){
  try{
    var r=await fetch('/api/state'),d=await r.json();
    $('st-cyc').textContent=d.cycle;
    $('st-status').textContent=d.status;
    var pk=Object.keys(d.positions||{});
    $('st-pos').textContent=pk.length;
    var stats=d.stats||{},wins=stats.wins||0,losses=stats.losses||0,total=wins+losses;
    $('st-trades').textContent=total;
    $('st-wr').textContent=total?'WR '+(wins/total*100).toFixed(0)+'% ('+wins+'W/'+losses+'L)':'';
    var pnl=stats.net_sol||0;
    $('st-pnl').textContent=(pnl>=0?'+':'')+pnl.toFixed(4);
    $('st-pnl').className='stat-big '+(pnl>=0?'c-grn':'c-red');
    pnlHistory.push(pnl);if(pnlHistory.length>120)pnlHistory.shift();
    drawPnl();
    if(d.soul){updateSoul(d.soul);updateSoulThoughts(d.soul.reflections);updateSessionStats(d.soul);}
    var feed=d.feed||[];
    if(feed.length&&feed[0].seq>lastSeq){lastSeq=feed[0].seq;renderFeed(feed.slice(0,100));}
    $('feed-cnt').textContent=feed.length;
    renderSignals((d.signals||[]).slice(0,50));
    renderPositions(d.positions||{});
    renderTrades((d.trades||[]).slice(0,50));
    $('prog-bar').style.width=((d.cycle%10)/10*100)+'%';
    var eb=$('err-bar');if(eb)eb.style.display='none';
  }catch(e){
    var eb=$('err-bar');
    if(eb){eb.textContent='⚠ poll error: '+(e&&e.message?e.message:e);eb.style.display='block';}
  }
}
setInterval(poll,2000);poll();
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
        probe.bind(("0.0.0.0", DASHBOARD_PORT))
        probe.close()
    except OSError:
        print(f"  ⚠️  Port {DASHBOARD_PORT} busy — killing...")
        subprocess.run(f"lsof -ti:{DASHBOARD_PORT} | xargs kill -9", shell=True, capture_output=True)
        time.sleep(1.5)

    HTTPServer.allow_reuse_address = True
    try:
        server = HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashHandler)
    except OSError as e:
        print(f"  ❌ Dashboard bind failed: {e}")
        raise
    server.serve_forever()
```

---

## 第十五部分：入口点

```python
if __name__ == "__main__":
    print("=" * 60)
    print("  扫链策略 Live Bot v1.0")
    print("  Anti-rug: LP Strict | Bundle 22% | Age 6min | Cooldown 10min")
    print("  Exit: DynTrail 8-20% | MomentumDead | VolExhaust | TP2 45%")
    print(f"  Wallet: {WALLET_ADDRESS[:8]}...{WALLET_ADDRESS[-4:]}")
    print(f"  Dashboard: http://localhost:{DASHBOARD_PORT}")
    print(f"  Max exposure: {MAX_SOL} SOL | Max positions: {MAX_POSITIONS}")
    print("=" * 60)

    load_on_startup()
    load_soul()

    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

    print(f"  scanner_loop started (every {LOOP_SEC}s)")
    print(f"  monitor_loop started (every {MONITOR_SEC}s)")

    push_feed({"sym_note": True,
               "msg": (f"🟢 Bot v1.0 started — Soul: {soul.get('name','...')} [{soul.get('stage','Novice')}]  "
                       f"SigA:{SIG_A_THRESHOLD}  BS:{BS_MIN}  MC>${MC_MIN/1000:.0f}K-${MC_CAP/1000:.0f}K  "
                       f"Age≥{AGE_HARD_MIN}s  Bundle≤{BUNDLE_ATH_PCT_MAX}%  LP Strict:{LP_LOCK_STRICT}  "
                       f"Monitor:{MONITOR_SEC}s  TP2:{TP2_PCT*100:.0f}%"),
               "t": time.strftime("%H:%M:%S")})

    print(f"  Dashboard → http://localhost:{DASHBOARD_PORT}")
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