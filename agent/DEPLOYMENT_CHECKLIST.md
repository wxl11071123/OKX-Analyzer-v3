# TSMOM 实盘部署 Checklist

## 部署前

### API 凭证 ✅ （已完成）
- [x] OKX 交易权限已开
- [x] API Key 绑 IP: 77.90.28.162 (德国 VPS) + 8.141.108.66 (阿里云)
- [x] API 短语: 服务器已有配置

### 飞书 Bot
- [ ] 确认 `~/.vibe-trading/agent.json` 中 feishu app_id/app_secret 正确
- [ ] 飞书开放平台开启卡片回调功能（否则按钮不生效）

## 部署步骤

### Step 1: 代码同步
```
scp -r agent/* root@8.141.108.66:/root/OKX-Analyzer-v3/agent/
```

### Step 2: 依赖确认
```
pip3 install httpx pandas numpy scipy
```

### Step 3: 环境变量确认
```
echo $OKX_API_KEY      # 应有值
echo $OKX_FLAG         # 设为 1 (demo)
```

### Step 4: Demo 验证 (OKX_FLAG=1)
```
cd /root/OKX-Analyzer-v3/agent
python3 -c "
from src.trading.connectors.okx.sdk import place_swap_order
r = place_swap_order(symbol='BTC-USDT-SWAP', side='buy', pos_side='long', sz='1')
print(r)
"
```

### Step 5: 引擎启动 (demo)
```
OKX_FLAG=1 python3 trading_engine.py
```

### Step 6: 确认指标
- [ ] 下单成功，返回 order_id
- [ ] 止损单创建成功
- [ ] 飞书收到开仓通知卡片
- [ ] 平仓正常
- [ ] 飞书收到平仓通知
- [ ] 持仓对账正常
- [ ] 日报推送正常

### Step 7: Demo 观察 24h
- [ ] 至少完成 3+ 笔完整的开->持->平
- [ ] 无崩溃

### Step 8: 切 Live (OKX_FLAG=0)
```
OKX_FLAG=0 python3 trading_engine.py
```

## 回滚方案

### 停止引擎
```
pkill -f trading_engine.py
```

### 紧急停止（不依赖 SSH）
```
飞书 → 日报卡片 → [停止交易] → [确认停止]
```

### 恢复
```
飞书 → 发送"恢复交易"
# 或手动: rm ~/.vibe-trading/live/HALT
```

## 监控

| 事项 | 频率 |
|---|---|
| 飞书日报 | 每天 22:00 |
| 飞书周报 + AI 分析 | 每周一 10:00 |
| 选币 + AI 评估 | 每天 08:00 |
| 持仓对账 | 每 60 秒 |
