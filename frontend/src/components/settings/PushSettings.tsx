import { useState, useEffect } from "react";
import { Bell, Plus, Trash2, Save, Clock } from "lucide-react";

interface PushConfig {
  enabled: boolean;
  symbols: string[];
  price_alerts: { enabled: boolean; threshold_percent: number };
  hourly_push: { enabled: boolean };
  news_push: { enabled: boolean; times: string[] };
}

const fieldClass = "w-full rounded-md border bg-background px-3 py-2 text-sm min-h-[44px]";
const labelClass = "text-sm font-medium";
const hintClass = "text-xs text-muted-foreground";

export function PushSettings() {
  const [config, setConfig] = useState<PushConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [newSymbol, setNewSymbol] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetch("/push/config")
      .then((r) => r.json())
      .then((data) => setConfig(data as PushConfig))
      .catch(() => setConfig({
        enabled: false,
        symbols: ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
        price_alerts: { enabled: true, threshold_percent: 5 },
        hourly_push: { enabled: true },
        news_push: { enabled: true, times: ["08:00", "20:00"] },
      }))
      .finally(() => setLoading(false));
  }, []);

  const save = async () => {
    if (!config) return;
    setSaving(true);
    try {
      await fetch("/push/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      console.error(e);
    } finally {
      setSaving(false);
    }
  };

  const addSymbol = () => {
    if (!config || !newSymbol.trim()) return;
    const s = newSymbol.trim().toUpperCase();
    if (!s.includes("-")) {
      alert("请输入完整交易对格式，如 BTC-USDT");
      return;
    }
    if (config.symbols.includes(s)) return;
    setConfig({ ...config, symbols: [...config.symbols, s] });
    setNewSymbol("");
  };

  const removeSymbol = (sym: string) => {
    if (!config || config.symbols.length <= 1) return;
    setConfig({ ...config, symbols: config.symbols.filter((s) => s !== sym) });
  };

  const addNewsTime = () => {
    if (!config) return;
    setConfig({
      ...config,
      news_push: { ...config.news_push, times: [...config.news_push.times, "12:00"] },
    });
  };

  const removeNewsTime = (t: string) => {
    if (!config) return;
    setConfig({
      ...config,
      news_push: { ...config.news_push, times: config.news_push.times.filter((x) => x !== t) },
    });
  };

  if (loading) return <div className="p-4 text-muted-foreground text-sm">加载中...</div>;
  if (!config) return <div className="p-4 text-muted-foreground text-sm">加载失败</div>;

  return (
    <div className="rounded-lg border bg-card p-5 shadow-sm space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Bell className="h-4 w-4 text-primary" />
          <h2 className="text-base font-semibold">飞书推送设置</h2>
        </div>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={config.enabled}
            onChange={(e) => setConfig({ ...config, enabled: e.target.checked })}
            className="w-4 h-4 rounded"
          />
          <span className="text-sm">启用推送</span>
        </label>
      </div>

      {!config.enabled && (
        <p className={hintClass}>开启后，价格预警和定时推送将通过飞书 Webhook 发送。</p>
      )}

      {config.enabled && (
        <>
          {/* Symbols */}
          <div>
            <label className={labelClass}>监控币种</label>
            <div className="flex flex-wrap gap-2 mt-2 mb-2">
              {config.symbols.map((s) => (
                <span key={s} className="inline-flex items-center gap-1 px-2 py-1 bg-muted rounded text-sm">
                  {s}
                  {config.symbols.length > 1 && (
                    <button onClick={() => removeSymbol(s)} className="text-muted-foreground hover:text-red-500">
                      <Trash2 className="h-3 w-3" />
                    </button>
                  )}
                </span>
              ))}
            </div>
            <div className="flex gap-2">
              <input
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                placeholder="BTC-USDT"
                className={`${fieldClass} flex-1`}
                onKeyDown={(e) => e.key === "Enter" && addSymbol()}
              />
              <button onClick={addSymbol} className="min-h-[44px] px-3 border rounded-md hover:bg-accent">
                <Plus className="h-4 w-4" />
              </button>
            </div>
          </div>

          {/* Price Alerts */}
          <div>
            <label className="flex items-center gap-2 cursor-pointer mb-2">
              <input
                type="checkbox"
                checked={config.price_alerts.enabled}
                onChange={(e) => setConfig({ ...config, price_alerts: { ...config.price_alerts, enabled: e.target.checked } })}
                className="w-4 h-4 rounded"
              />
              <span className={labelClass}>价格预警</span>
            </label>
            {config.price_alerts.enabled && (
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">24h 涨跌幅超过</span>
                <input
                  type="number"
                  value={config.price_alerts.threshold_percent}
                  onChange={(e) => setConfig({ ...config, price_alerts: { ...config.price_alerts, threshold_percent: Number(e.target.value) } })}
                  className="w-20 rounded-md border px-2 py-1 text-sm min-h-[44px] text-center"
                  min={1}
                  max={50}
                />
                <span className="text-sm text-muted-foreground">% 时推送</span>
              </div>
            )}
          </div>

          {/* Hourly Push */}
          <div>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={config.hourly_push.enabled}
                onChange={(e) => setConfig({ ...config, hourly_push: { ...config.hourly_push, enabled: e.target.checked } })}
                className="w-4 h-4 rounded"
              />
              <span className={labelClass}>每小时行情快报</span>
            </label>
            <p className={hintClass}>整点推送 BTC/ETH/SOL 价格和涨跌幅</p>
          </div>

          {/* News Push */}
          <div>
            <label className="flex items-center gap-2 cursor-pointer mb-2">
              <input
                type="checkbox"
                checked={config.news_push.enabled}
                onChange={(e) => setConfig({ ...config, news_push: { ...config.news_push, enabled: e.target.checked } })}
                className="w-4 h-4 rounded"
              />
              <span className={labelClass}>新闻推送</span>
            </label>
            {config.news_push.enabled && (
              <div>
                <div className="flex flex-wrap gap-2 mb-2">
                  {config.news_push.times.map((t) => (
                    <span key={t} className="inline-flex items-center gap-1 px-2 py-1 bg-muted rounded text-sm">
                      <Clock className="h-3 w-3" />
                      {t}
                      {config.news_push.times.length > 1 && (
                        <button onClick={() => removeNewsTime(t)} className="text-muted-foreground hover:text-red-500">
                          <Trash2 className="h-3 w-3" />
                        </button>
                      )}
                    </span>
                  ))}
                </div>
                <button onClick={addNewsTime} className="text-sm text-primary hover:underline flex items-center gap-1 min-h-[44px]">
                  <Plus className="h-3 w-3" /> 添加推送时间
                </button>
              </div>
            )}
          </div>
        </>
      )}

      {/* Save */}
      <button
        onClick={save}
        disabled={saving}
        className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 transition min-h-[44px] disabled:opacity-50"
      >
        <Save className="h-4 w-4" />
        {saved ? "已保存" : saving ? "保存中..." : "保存设置"}
      </button>

      <p className={hintClass}>
        需要先在飞书群中添加机器人 Webhook，然后将 webhook_url 配置到 ~/.vibe-trading/agent.json 中。
      </p>
    </div>
  );
}
