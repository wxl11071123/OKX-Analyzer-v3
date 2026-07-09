# OKX-Analyzer-v3 定制改造计划

> 基于 vibe-trading v0.1.10 fork，改造为「加密货币专用 AI 交易研究助手」
> 
> 原则：只分析不执行、只 crypto、轻量化、本地数据持久化

## 总体策略

```
删 → 非 crypto 的一切
改 → 保留模块适配 crypto-only
加 → 交易日志 + 飞书推送 + RSS 新闻
精 → 依赖裁剪
```

---

## Phase 1: 清理非 Crypto 模块

### 1.1 数据加载器 — 保留 4 个，删除 28 个

**保留:**
- `backtest/loaders/okx.py` — OKX 加密货币
- `backtest/loaders/ccxt_loader.py` — CCXT 多交易所
- `backtest/loaders/local_loader.py` — 本地文件（调试用）
- `backtest/loaders/base.py` + `registry.py` — 基础设施

**删除（A股/美股/港股/期货/付费）:**
- akshare_loader.py, alphavantage_loader.py, baostock_loader.py
- eastmoney_client.py, eastmoney_loader.py
- finnhub_loader.py, fmp_loader.py
- fundamentals_loader.py, futu.py
- mootdx_loader.py, qveris_loader.py
- rsshub_events.py, sec_edgar_client.py
- sina_loader.py, stooq_loader.py
- tencent_loader.py, tiingo_loader.py
- tushare.py, tushare_fundamentals.py
- yahoo_client.py, yahoo_loader.py, yfinance_loader.py

### 1.2 交易连接器 — 保留 2 个，删除 10 个

**保留（改为只读）:**
- `connectors/okx/` — OKX 只读
- `connectors/binance/` — Binance 只读

**删除:**
- alpaca/, dhan/, futu/, ibkr/, longbridge/
- robinhood/, shoonya/, tiger/, trading212/

### 1.3 Agent 工具 — 保留约 30 个，删除约 25 个

**删除（A股/美股专属）:**
- block_trades_tool.py — 大宗交易
- dragon_tiger_tool.py — 龙虎榜
- factor_analysis_tool.py — 股票因子分析
- financial_rigor_tool.py — 财报严格性
- financial_statements_tool.py — 财报
- fred_macro_tool.py — FRED 宏观
- fund_flow_tool.py — 资金流
- get_fundamentals_tool.py — 基本面
- iwencai_tool.py — 问财
- lockup_expiry_tool.py — 限售解禁
- margin_trading_tool.py — 融资融券
- market_screener_tool.py — 市场筛选
- northbound_tool.py — 北向资金
- options_chain_tool.py — 期权链
- options_pricing_tool.py — 期权定价
- propose_mandate_tool.py — 交易授权
- qveris_tool.py — 付费数据
- report_audit_tool.py — 审计报告
- research_reports_tool.py — 研报
- sec_filings_tool.py — SEC文件
- sector_tool.py — 板块
- shareholder_count_tool.py — 股东人数
- stock_profile_tool.py — 股票档案

**保留（通用/crypto相关）:**
- backtest_tool.py, bash_tool.py, compact_tool.py
- doc_reader_tool.py, edit_file_tool.py, goal_tool.py
- hypothesis_tool.py, load_skill_tool.py
- market_data_tool.py, mcp.py, pattern_tool.py
- read_file_tool.py, remember_tool.py
- session_search_tool.py, skill_writer_tool.py
- web_reader_tool.py, web_search_tool.py, write_file_tool.py
- alpha_bench_tool.py, alpha_compare_tool.py, alpha_zoo_tool.py
- autopilot_tool.py, background_tools.py
- shadow_account_tool.py, trade_journal_tool.py
- symbol_search_tool.py, swarm_tool.py

### 1.4 Skills — 清理非 crypto 技能

删除 `agent/src/skills/` 下与股票/A股/宏观/期权相关的 SKILL.md

---

## Phase 2: 修改保留模块

### 2.1 `market_data.py` — 只保留 crypto 路由

```python
# 修改前（6条规则）
_SOURCE_PATTERNS = [
    (re.compile(r"^local:", re.I), "local"),
    (re.compile(r"^\d{6}\.(SZ|SH|BJ)$", re.I), "tencent"),  # 删
    (re.compile(r"^[A-Z]+\.US$", re.I), "yahoo"),            # 删
    (re.compile(r"^\d{3,5}\.HK$", re.I), "yahoo"),           # 删
    (re.compile(r"^[A-Z]+-USDT$", re.I), "okx"),
    (re.compile(r"^[A-Z]+/USDT$", re.I), "ccxt"),
]

# 修改后（3条规则）
_SOURCE_PATTERNS = [
    (re.compile(r"^local:", re.I), "local"),
    (re.compile(r"^[A-Z]+-USDT$", re.I), "okx"),
    (re.compile(r"^[A-Z]+/USDT$", re.I), "ccxt"),
]

# 默认源改为 okx
def detect_source(code: str) -> str:
    for pattern, source in _SOURCE_PATTERNS:
        if pattern.match(code):
            return source
    return "okx"  # 原来是 "tushare"
```

### 2.2 `trading_connector_tool.py` — 改为只读

移除:
- `TradingPlaceOrderTool` 类
- `TradingCancelOrderTool` 类
- `place_order`, `cancel_order` 的 import

保留:
- `TradingConnectionsTool` — 列出连接
- `TradingSelectConnectionTool` — 选择连接
- `TradingCheckTool` — 检查连接
- `TradingAccountTool` — 账户信息（只读）
- `TradingPositionsTool` — 持仓（只读）
- `TradingOrdersTool` — 订单（只读）
- `TradingQuoteTool` — 报价（只读）
- `TradingHistoryTool` — 历史（只读）

### 2.3 `registry.py` — 精简 fallback 链

```python
# 修改后
FALLBACK_CHAINS: dict[str, list[str]] = {
    "crypto": ["okx", "ccxt", "local"],
}
```

### 2.4 `stock_news_tool.py` → 改为 `crypto_news_tool.py`

**需求升级**: 不仅加密货币垂直新闻，需要覆盖所有可能影响 crypto 的事件——
美联储政策、全球宏观、地缘政治、监管动态等。

**数据源**: 纯 RSS 被动聚合，AI 只读不搜。

```
后端任务: RSS 定时轮询（每5分钟）
  └→ 6个源 → feedparser → SQLite news_cache（MD5去重）

AI 工具: crypto_news_tool（只读查询）
  └→ 从 SQLite 读取 → 返回结构化新闻列表（来源+时间+内容）
  └→ AI 不能自己搜新闻，杜绝幻觉
```

**时效性机制**:
- 每条新闻记录原始发布时间 `published_at`（ISO 8601）
- System prompt 注入当前 UTC 时间，AI 知道"现在几点"
- 新闻按时间排序，过期新闻自动降权
- 飞书推送时标注"X分钟前/X小时前"

---

## Phase 3: 新增模块

### 3.1 交易日志工具 `trade_log_tool.py`

```python
class TradeLogTool(BaseTool):
    name = "trade_log"
    description = "记录和分析交易日志..."
    
    # 功能:
    # - 从 OKX API 拉取成交记录 (fills-history)
    # - 写入本地 SQLite
    # - 补充备注和纪律评分
    # - AI 行为分析
```

### 3.2 加密货币新闻工具 `crypto_news_tool.py`

**纯 RSS 聚合，AI 只读不搜**（杜绝幻觉）。

**后端采集**（定时任务，非 AI 触发）:
- 6 个 RSS 源：CoinDesk / CoinTelegraph / CryptoSlate / Decrypt / The Block / Bitcoin Magazine
- `feedparser` 实现，每 5 分钟轮询
- 写入 SQLite `news_cache` 表，MD5 去重
- 字段：title, link, summary, source, published_at, fetched_at

**AI 工具**（只读查询）:
- AI 只能调用 `crypto_news` 工具从本地 DB 读取
- 返回带来源+时间戳的结构化数据，不存在幻觉

### 3.3 飞书推送扩展

在现有 `channels/feishu.py` 基础上扩展：
- 技术指标触发推送（价格突破、RSI 超买超卖等）
- 定时汇总推送（每日行情快照）
- 新闻推送
- Bot 双向对话（查询行情、新闻、回测）

### 3.4 交易日志数据库

SQLite 表设计：
```sql
CREATE TABLE trade_log (
    id INTEGER PRIMARY KEY,
    trade_id TEXT UNIQUE,
    symbol TEXT,
    side TEXT,         -- buy/sell
    inst_type TEXT,    -- SPOT/SWAP
    price REAL,
    quantity REAL,
    fee REAL,
    fee_currency TEXT,
    pnl REAL,
    exec_type TEXT,    -- T/M (taker/maker)
    fill_time INTEGER,
    note TEXT,         -- 用户备注
    discipline_score INTEGER,  -- 纪律评分 1-10
    created_at INTEGER
);
```

---

## Phase 4: 精简依赖（含 AI 时间感知）

> **3.5 AI 时间感知**: System prompt 注入 `datetime.now(timezone.utc)` 确保 AI 知道当前真实时间。

`pyproject.toml` 删除:
- `tushare>=1.2.89` — A股数据
- `yfinance>=0.2.30` — 美股数据
- `akshare>=1.12.0` — 多市场数据
- `smartmoneyconcepts>=0.0.1` — SMC指标（可选保留）

---

## Phase 5: 前端定制

基于 vibe-trading 的 React 前端，做以下调整：
- 移除股票相关页面/组件
- 移除交易执行相关 UI
- 新增交易日志页面（录入 + AI 分析视图）
- 新增飞书推送配置页面
- 中文化（参考旧项目配色）

---

## Phase 6: 验证

1. CLI 回测功能正常（已验证 ✅）
2. OKX 数据拉取正常
3. 所有工具正常注册
4. 飞书 Bot 连通
5. 交易日志读写正常
6. 依赖安装无报错

---

## 风险评估

| 风险 | 应对 |
|------|------|
| 删除文件导致 import 错误 | 逐个模块删除 + 每步验证 |
| 工具注册链断裂 | `build_registry` 自动跳过缺失依赖 |
| 上游更新冲突 | 已决定独立发展，不追上游 |
