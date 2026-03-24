# Meme Scanner v1.0

> ⚠️ **风险声明**：本项目仅供学习研究，meme 币交易存在极高风险，可能损失全部本金。作者不承担任何财务损失责任。使用前请充分了解相关风险。

---

## 功能特性

- 🔍 基于 OKX Web3 API 实时扫描 Solana meme 代币
- 🛡️ 防 rug 检测（LP 严格锁定验证 / Bundle ≤22% / Dev Hold ≤10% / Age ≥6min）
- 📉 动态 Trailing Stop（8-20%，随盈利自动调整）
- 💀 动量死亡 + 量能枯竭检测，智能提前锁利
- ❌ 被拒代币 10 分钟冷却缓存，避免重复检测
- 🧠 TraderSoul 进化型交易人格系统（只读分析，不干预交易）
- 🖥️ 实时 Web Dashboard（端口 3241）
- 📦 单 `.md` Skill 文件，Claude Code 一键部署

---

## 环境要求

| 必需项 | 说明 | 获取方式 |
|---|---|---|
| **OKX Web3 API Key** | 用于调用链上数据和执行交易 | [OKX 开发者平台](https://www.okx.com/web3/build/docs) |
| **Solana 钱包私钥** | Bot 用于签名和发送交易 | Phantom / Solflare 导出 |
| **Python 3.10+** | 运行环境 | [python.org](https://python.org) |
| **Claude Code** | 用于部署和运行 Skill | [claude.ai/code](https://claude.ai/code) |

> ⚠️ **强烈建议**：新建一个专用钱包用于 Bot，不要使用存有大量资产的主钱包。初始充值 0.1-0.5 SOL 用于测试即可。

---

## 🚀 快速开始

### 第一步：申请 OKX API Key

1. 登录 [OKX 开发者平台](https://www.okx.com/web3/build/docs)
2. 创建新的 API Key，权限勾选：
   - ✅ Read
   - ✅ Trade
3. 记录下三个值：
   - `API Key`
   - `Secret Key`
   - `Passphrase`

---

### 第二步：安装依赖

```bash
pip install requests solders base58
```

---

### 第三步：配置环境变量

**Linux / macOS（临时，当前终端有效）：**
```bash
export OKX_API_KEY="你的API Key"
export OKX_SECRET_KEY="你的Secret Key"
export OKX_PASSPHRASE="你的Passphrase"
export WALLET_PRIVATE_KEY="你的Solana钱包私钥(Base58格式)"
```

**Linux / macOS（永久，重启后依然有效）：**
```bash
cat >> ~/.bashrc << 'EOF'
export OKX_API_KEY="你的API Key"
export OKX_SECRET_KEY="你的Secret Key"
export OKX_PASSPHRASE="你的Passphrase"
export WALLET_PRIVATE_KEY="你的Solana钱包私钥"
EOF
source ~/.bashrc
```

**Windows（PowerShell）：**
```powershell
$env:OKX_API_KEY="你的API Key"
$env:OKX_SECRET_KEY="你的Secret Key"
$env:OKX_PASSPHRASE="你的Passphrase"
$env:WALLET_PRIVATE_KEY="你的Solana钱包私钥"
```

---

### 第四步：获取 Skill 文件

**方式一：克隆完整仓库（推荐）**
```bash
git clone https://github.com/FeeeeelixWong/meme_scanner_v1.0
cd oxscan-live-bot
```

**方式二：只下载 Skill 文件（轻量）**
```bash
# curl
curl -O https://github.com/FeeeeelixWong/meme_scanner_v1.0/blob/main/meme_scanner_v1.0.md

# 或 wget
wget https://github.com/FeeeeelixWong/meme_scanner_v1.0/blob/main/meme_scanner_v1.0.md
```

---

### 第五步：用 Claude Code 部署 Bot

1. 在项目目录下打开 Claude Code
2. 对 Claude 说：

```
请按照 skill 文件里的 AUTO-DEPLOY COMMAND，
部署并启动 scan_live.py，skill 文件路径是 ./meme_scanner_v1.0.md
```

3. Claude Code 会自动执行 STEP 1-5，完成部署

### 第六步：确认运行正常

Bot 启动后，打开浏览器访问：

```
http://localhost:3241
```

看到 Dashboard 界面说明运行正常 ✅

**查看实时日志：**
```bash
tail -f bot.log
```

**停止 Bot：**
```bash
pkill -f scan_live.py
```

---

## 🧪 建议的测试流程

> 不要上来就用真实仓位跑！

### 第一天：观察模式（极小仓位）

先把仓位改成极小值，只观察信号质量：

在 `meme_scanner_v1.0.md` 第一部分找到以下内容并临时修改：

```python
# 改小仓位，先观察
SOL_PER_TRADE = {"SCALP": 0.001, "MINIMUM": 0.001, "STRONG": 0.001}
MAX_SOL = 0.05
```

观察 24 小时，确认：
- [ ] 信号触发频率是否正常（建议每小时 1-5 个）
- [ ] Safety check 是否在正常拦截可疑代币
- [ ] Dashboard 数据是否实时更新
- [ ] 交易能否正常成交

### 第二天：小仓位实盘

确认信号质量后，使用默认配置（0.01 SOL/笔）运行 2-3 天，观察胜率和 PnL 曲线。

### 第三天之后：按表现调整

根据 Dashboard 里的 TraderSoul 分析和历史胜率，决定是否调整仓位大小。

---

## ⚙️ 主要参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `SOL_PER_TRADE` | 0.01 SOL | 每笔交易仓位 |
| `MAX_POSITIONS` | 5 | 最大同时持仓数 |
| `MAX_SOL` | 0.25 SOL | 最大总敞口 |
| `AGE_HARD_MIN` | 360s | 代币最小年龄（6分钟） |
| `BUNDLE_ATH_PCT_MAX` | 22% | Bundle 最大占比 |
| `TP1_PCT` | 15% | 第一止盈点 |
| `TP2_PCT` | 45% | 第二止盈点 |
| `TRAILING_DROP` | 8-20% | 动态 Trailing Stop 幅度 |
| `MAX_HOLD_MIN` | 30min | 最大持仓时间 |
| `MONITOR_SEC` | 3s | 持仓监控间隔 |

---

## v5.3 → v1.0 核心升级

| 维度 | v5.3 | v1.0 |
|---|---|---|
| LP 验证失败 | 放行 | ❌ 拒绝（Strict=True） |
| BUNDLE_ATH_PCT_MAX | 30% | **22%** |
| TOP10_HOLD_MAX | 40% | **33%** |
| AGE_HARD_MIN | 240s | **360s** |
| MIN_HOLDERS | 25 | **35** |
| TP2_PCT | 25% | **45%** |
| Trailing Stop | 固定 8% | **动态 8-20%** |
| 动量死亡检测 | ❌ | ✅ |
| 量能枯竭检测 | ❌ | ✅ |
| Rejected 冷却缓存 | ❌ | ✅ 10min |

---

## ❓ 常见问题

**Q: `WALLET_PRIVATE_KEY` 是什么格式？**

Solana 钱包的 Base58 格式私钥，通常是一串 87-88 位的字母数字。
在 Phantom 中：设置 → 账户 → 导出私钥。

---

**Q: Bot 启动后 Dashboard 打不开？**

```bash
# 检查是否在运行
ps aux | grep scan_live

# 检查端口是否被占用
lsof -i:3241

# 查看错误日志
tail -50 bot.log
```

---

**Q: 出现 `ModuleNotFoundError`？**

```bash
pip3 install requests solders base58
```

---

**Q: 交易一直 PENDING 或失败？**

- 检查钱包 SOL 余额是否充足（建议 > 0.1 SOL 用于 gas）
- 检查 OKX API Key 是否有 Trade 权限
- 查看 `bot.log` 里的具体错误信息

---

**Q: 如何完全重置重新开始？**

```bash
pkill -f scan_live.py
rm -f scan_positions.json scan_trades.json trader_soul.json bot.log
```

---

## 文件说明

```
meme_scanner_v1.0.md    ← Skill 文件（包含完整 Bot 代码）
scan_live.py            ← 由 Skill 自动提取生成，无需手动维护
bot.log                 ← 运行日志
scan_positions.json     ← 持仓记录（自动生成）
scan_trades.json        ← 交易历史（自动生成）
trader_soul.json        ← TraderSoul 进化数据（自动生成）
```

---

## License

MIT License — 可自由使用、修改、分发，但请保留原始声明。

---

## 免责声明

- 本项目仅供学习和研究目的
- meme 币市场波动剧烈，存在极高风险
- Bot 运行产生的任何盈亏由使用者自行承担
- 作者不对代码 bug、API 异常、网络中断等导致的损失负责
- 请遵守所在地区的法律法规
