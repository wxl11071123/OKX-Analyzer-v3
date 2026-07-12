---
name: coin-scanner
description: 永续合约全市场扫描选币。五层漏斗筛选（流动性->TSMOM趋势->信号即入场->资金费率->纪律检查），从 OKX 全部永续合约中找出符合用户交易纪律 v2.0 的可交易标的。纪律硬约束：TSMOM(120)方向判定 + Hurst>0.55 趋势确认、信号即入场、杠杆≤5X、单笔止损≤15%。
category: crypto
---
# 选币扫描器

## 概述

从 OKX 全部永续合约中，按用户交易纪律 v2.0 的硬性要求筛选可交易标的。不是找"涨得多的币"，而是找**满足 TSMOM 趋势条件且信号已触发**的币。

## 用户交易纪律 v2.0（硬约束）

以下规则在选币中**必须满足**，不可降级：

### 资金管理（动态，需获取当前档位）
- 总资金三等分，每次仅用一份
- 杠杆 ≤ 5X
- 每份资金额度在升级/降级触发前固定不变：
  - 纪律 1.3：亏损后从池子补充，保持战斗资金满额
  - 纪律 1.4：总资金翻倍时重新三等分（如 150->300，每份 100U）
  - 纪律 1.5：总资金腰斩时重新三等分（如 150->75，每份 25U）
- **当前每份资金通过 `okx_portfolio` 查总权益推算档位**：
  - 总权益 ≈ 75 -> 每份 25U
  - 总权益 ≈ 150 -> 每份 50U
  - 总权益 ≈ 300 -> 每份 100U
  - 总权益 ≈ 600 -> 每份 200U
  - 介于两档之间时，取最近的档位

### 趋势定义（TSMOM）
- 过去 120 根 4H K 线收益率 > 0 **且** Hurst 指数 > 0.55 → 做多候选
- 过去 120 根 4H K 线收益率 < 0 **且** Hurst 指数 > 0.55 → 做空候选
- Hurst 指数 ≤ 0.55 → **不交易**（无论收益率方向如何）

### 入场时机
- **信号即入场**：TSMOM 信号触发（方向判定 + Hurst > 0.55）后，下一根 K 线开盘即可入场
- 不再等待回调到 EMA20
- 禁止追突破
- 不逆势抄底/摸顶

### 风控
- 止损 ≤ 该份资金 15%
- 止盈 30%，提取利润，止损上移至成本价
- 每份资金额在升级/降级前固定（纪律 1.3-1.5），不随单笔盈亏变化

## 前置条件

- 所有 OKX API 请求通过 relay：`BASE_URL = os.getenv("OKX_RELAY", "https://www.okx.com") + "/api/v5"`
- K线数据通过 `get_market_data(output_mode="file_cache")` + `compute_indicators` 工具链获取
- 永续合约 instId 格式：`BTC-USDT-SWAP`
- Hurst 指数通过 `src.indicators.ta.compute_hurst(close, window=200)` 计算，需拉取至少 200 根 4H K 线

## 五层筛选流程

### 第一层：流动性过滤

**目标**：淘汰低流动性土狗和疑似对刷标的。

**数据获取**：

```python
import os, requests, pandas as pd

BASE = os.getenv("OKX_RELAY", "https://www.okx.com") + "/api/v5"

# 批量获取所有永续合约行情
resp = requests.get(f"{BASE}/market/tickers", params={"instType": "SWAP"})
tickers = resp.json()["data"]

df = pd.DataFrame([{
    "instId": t["instId"],
    "last": float(t["last"]),
    "vol24h_usdt": float(t["volCcyQuote"]),   # 24h 成交额(USDT)
    "open24h": float(t["open24h"]),
    "change24h_pct": (float(t["last"]) / float(t["open24h"]) - 1) * 100,
    "bid": float(t["bidPx"]),
    "ask": float(t["askPx"]),
} for t in tickers])
```

**筛选条件**：

| 条件 | 阈值 | 原因 |
|------|------|------|
| 24h 成交额 | > $10M | 流动性门槛。用户单笔 50U 5X = 250U 名义价值，$10M 日成交额足以保证无滑点 |
| 买卖价差 | < 0.3% | (ask-bid)/bid，价差大说明做市商不参与 |
| instId 后缀 | -USDT-SWAP | 只看 USDT 计价永续合约 |

> 注：阈值较低（$10M）是因为用户资金量小（单份 50U），不需要机构级流动性门槛。

**输出**：通过流动性的币种列表（通常 40-80 个）。

### 第二层：TSMOM 趋势方向判定（纪律 2.1）

**目标**：用 TSMOM（时间序列动量）+ Hurst 指数判定趋势方向，淘汰无趋势或随机游走币。

**核心逻辑**（对应 `backtest/engines/tsmom_signal.py:TSMOMSignalEngine`）：
- TSMOM 只有 1 个参数（回看窗口 120），最小过拟合风险
- Hurst 过滤确保只在趋势态交易（避免震荡市假突破）

**操作**：对第一层通过的每个币，拉取 4H K线（至少 200 根以确保 Hurst 有足够数据）。

```python
# 拉取 4H K 线数据（至少 200 根，确保 Hurst 计算有效）
get_market_data(codes=["XXX-USDT-SWAP"], start_date=60天前, end_date=今天, interval="4H", output_mode="file_cache")

# TSMOM 信号计算
close = df["close"]
# 过去 120 期收益率
past_return = close.pct_change(120).iloc[-1]

# Hurst 指数（window=200，与回测参数一致）
from src.indicators.ta import compute_hurst
hurst_series = compute_hurst(close, window=200)
hurst = hurst_series.iloc[-1]
```

**筛选条件（纪律硬约束）**：

| 条件 | 判断 | 方向 |
|------|------|------|
| 120 期收益率 > 0 且 Hurst > 0.55 | 趋势向上 | 做多候选 |
| 120 期收益率 < 0 且 Hurst > 0.55 | 趋势向下 | 做空候选 |
| Hurst ≤ 0.55 | 随机游走 | **淘汰**（纪律 2.1：不交易） |

> 注意：Hurst 指数计算需要 window=200 个数据点，确保拉取 ≥ 200 根 4H K 线。如果数据不足 200 根，该币种淘汰（数据不足）。

**输出**：带方向的候选列表（通常 5-20 个）。

### 第三层：入场时机评估（信号即入场）

**目标**：v2.0 纪律改为"信号即入场"——TSMOM 信号触发后即可入场，不再等待回调 EMA20。

**本层职责**：评估信号强度和质量，而非筛选入场时机。

**操作**：复用第二层的 CSV，计算 ATR 和 ADX 辅助判断。

```python
compute_indicators(file="<路径>", indicators=["atr", "adx"], params={"atr": {"period": 14}, "adx": {"period": 14}})
```

**信号质量分级**：

| TSMOM 收益率(%) | Hurst | ADX | 状态 | 操作建议 |
|----------------|-------|-----|------|---------|
| >5% | >0.60 | >25 | 🟢 **强信号** | 趋势明确，信号强劲，建议入场 |
| 2%-5% | >0.55 | >20 | 🟡 **标准信号** | 趋势可确认，按纪律入场 |
| 0-2% | >0.55 | 任意 | 🟡 **弱信号** | 方向确认但动量弱，小仓位或观望 |
| 任意 | >0.55 | <20 | 🔵 **低波动** | 趋势确认但波动不足，可能横盘 |
| 任意 | ≤0.55 | 任意 | 🔴 **淘汰** | 已在第二层淘汰 |

**输出**：信号质量评级。🟢 和 🟡 的币进入下一层。

### 第四层：资金费率确认

**目标**：利用资金费率判断拥挤度，避免在极度拥挤的交易中入场。

**数据获取**：

```python
funding = requests.get(f"{BASE}/public/funding-rate", params={"instId": "XXX-USDT-SWAP"}).json()["data"][0]
rate_8h = float(funding["fundingRate"])
annualized_pct = rate_8h * 3 * 365 * 100
```

**信号解读与纪律交叉验证**：

| 费率(8h) | 含义 | 做多候选 | 做空候选 |
|---------|------|---------|---------|
| > 0.1%（年化>109%） | 多头极度拥挤 | ⚠️ 减仓或回避 | ✅ 做空信号确认 |
| 0.03% ~ 0.1% | 多头偏多 | 谨慎做多 | ✅ 做空信号增强 |
| -0.01% ~ 0.03% | 中性 | ✅ 正常 | ✅ 正常 |
| < -0.05%（年化<-55%） | 空头极度拥挤 | ✅ 做多信号确认 | ⚠️ 减仓或回避 |

**淘汰规则**：
- 做多候选 + 费率 > 0.1%（极度正）→ 降级为观望
- 做空候选 + 费率 < -0.05%（极度负）→ 降级为观望

**输出**：资金费率确认通过的最终候选列表（通常 3-8 个）。

### 第五层：风控计算与纪律总结

**目标**：对通过的币计算具体止损止盈价位，确认符合纪律 3.1-3.2。

**计算**：

```python
# 从 okx_portfolio 获取当前总权益，推算当前档位的每份资金
total_equity = <从 okx_portfolio 工具获取>

# 纪律 1.4-1.5：只有翻倍/腰斩才重新三等分，中间不变
# 推算当前档位（取最近的标准档位）
TIERS = [75, 150, 300, 600, 1200]  # 标准总权益档位
tier = min(TIERS, key=lambda t: abs(t - total_equity))
capital_per_trade = tier / 3  # 每份资金（固定值，不随单笔盈亏变化）

leverage = 5                            # 纪律 1.2：≤5X
notional = capital_per_trade * leverage
max_loss = capital_per_trade * 0.15     # 纪律 3.1：止损≤15%
take_profit = capital_per_trade * 0.30  # 纪律 3.2：止盈30%

# 做多止损价
entry_price = current_price
stop_loss_price = entry_price - (max_loss / (notional / entry_price))
take_profit_price = entry_price + (take_profit / (notional / entry_price))

# 做空则相反
```

**输出**：每个推荐标的附带完整交易计划。

## 输出格式

```
## 选币扫描报告 - {日期}

### 扫描概要
- 扫描标的：{总数} 个永续合约
- 第一层(流动性)通过：{n1}
- 第二层(TSMOM趋势)通过：{n2}（做多 {n2_long} / 做空 {n2_short}）
- 第三层(信号质量)通过：{n3}（🟢强信号 {n3a} / 🟡标准信号 {n3b}）
- 第四层(资金费率)通过：{n4}
- 最终推荐：{n5}

### 🟢 推荐入场（强信号）
| 币种 | 方向 | 现价 | TSMOM收益% | Hurst | ADX | ATR | 费率(年化) | 入场价 | 止损价 | 止盈价 | 风险额 |
|------|------|------|-----------|-------|-----|-----|-----------|--------|--------|--------|--------|
| XXX-USDT-SWAP | 做多 | 100 | +8.2% | 0.68 | 28.3 | 2.5 | 12% | 100 | 96.9 | 106 | {动态} |

### 🟡 标准信号
| 币种 | 方向 | 现价 | TSMOM收益% | Hurst | ADX | 费率(年化) | 状态 |
|------|------|------|-----------|-------|-----|-----------|------|
| YYY-USDT-SWAP | 做空 | 50.3 | -3.1% | 0.57 | 22.5 | 0.01% | 方向确认，动量偏弱 |

### ⏸️ 暂不交易
| 币种 | 淘汰层 | 原因 |
|------|--------|------|
| ZZZ-USDT-SWAP | 第二层 | Hurst=0.48，随机游走态 |
| WWW-USDT-SWAP | 第四层 | 做多候选但费率0.15%，多头极度拥挤 |

### 交易计划（仅 🟢 推荐标的）
对每个推荐标的：
- 方向：做多/做空
- 入场条件：TSMOM 信号已触发，下一根 4H K 线开盘入场
- 入场价：现价
- 止损价：{计算值}（亏损 ≤ 单份资金×15%，纪律 3.1）
- 止盈价：{计算值}（盈利 +单份资金×30%，纪律 3.2）
- 仓位：{总权益/3}U × 5X = {动态}U 名义价值
- 注意：止盈后提取利润，止损上移至成本价
```

## API 调用清单

| 步骤 | 端点/工具 | 参数 | 每币调用 |
|------|----------|------|---------|
| 第一层 | /market/tickers | instType=SWAP | 1次（批量） |
| 第二层 | get_market_data | interval=4H, 60天（≥200根K线） | 1次 |
| 第二层 | compute_hurst | window=200 | 本地计算（复用CSV） |
| 第二层 | pct_change(120) | - | 本地计算（复用CSV） |
| 第三层 | compute_indicators | atr(14), adx(14) | 1次（复用CSV） |
| 第四层 | /public/funding-rate | instId=XXX-USDT-SWAP | 1次 |
| 第五层 | 本地计算 | - | 0次 |

**调用估算**：假设第一层通过 50 个币，第二层淘汰到 15 个，第三层淘汰到 10 个，第四层淘汰到 5 个。总调用 ≈ 1(批量) + 50×1(K线) + 15×1(指标) + 10×1(费率) ≈ 76 次。相比 v1.1 减少约 60%，因为 TSMOM 不需要分别计算 EMA20 和 EMA50。

## 重要提醒

1. **4H 周期是纪律要求**：TSMOM 回测验证基于 4H 周期，不要用日线或其他周期
2. **Hurst window=200**：`compute_hurst(close, window=200)`，与回测参数一致。确保拉取 ≥ 200 根 4H K 线（约 34 天）
3. **TSMOM lookback=120**：过去 120 根 4H K 线的收益率方向，对应约 20 天
4. **只看 USDT-SWAP**：筛选时只保留 instId 以 `-USDT-SWAP` 结尾的合约
5. **relay 必须用**：所有请求通过 `OKX_RELAY` 环境变量
6. **Hurst ≤ 0.55 = 不交易**：这是纪律硬约束，无论收益率多强都不例外
7. **信号即入场**：不需要等回调，TSMOM 信号触发后下一根 K 线即可入场
8. **频率控制**：批量调用时 sleep 0.1-0.2 秒，避免触发 OKX 限频