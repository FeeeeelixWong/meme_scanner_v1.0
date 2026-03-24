# Meme Scanner Bot v1.0

> ⚠️ **风险声明**：本项目仅供学习研究，
> meme 币交易存在极高风险，可能损失全部本金。
> 作者不承担任何财务损失责任。

## 功能特性
- 基于 OKX Web3 API 扫描 Solana meme 代币
- 防 rug 检测（LP 锁定验证 / Bundle / Dev Hold）
- 动态 Trailing Stop（8-20%）
- 动量死亡 + 量能枯竭检测
- TraderSoul 进化型交易人格系统（只读分析）
- 实时 Web Dashboard（端口 3241）

## 环境要求
- Python 3.10+
- OKX Web3 API Key（需要交易权限）
- Solana 钱包私钥

## 快速开始
参考 Skill 文件内 AUTO-DEPLOY COMMAND 章节。

## 环境变量
\`\`\`bash
export OKX_API_KEY="..."
export OKX_SECRET_KEY="..."
export OKX_PASSPHRASE="..."
export WALLET_PRIVATE_KEY="..."
\`\`\`

## 免责声明
...
