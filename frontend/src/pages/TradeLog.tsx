import { useState, useEffect, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { TrendingUp, TrendingDown, Target, Search, Save, RefreshCw } from "lucide-react";
import { api, type TradeLogEntry, type TradeStats } from "@/lib/api";

// ── 每行编辑状�?───────────────────────────────────────────
interface RowEditState {
  note: string;
  disciplineScore: number;
  saving: boolean;
  saved: boolean;
}

function fmt(n: number, d = 2) {
  return n.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function pnlColor(v: number) {
  return v > 0 ? "text-green-500" : v < 0 ? "text-red-500" : "text-muted-foreground";
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function TradeLog() {
  const { t } = useTranslation();
  const [trades, setTrades] = useState<TradeLogEntry[]>([]);
  const [stats, setStats] = useState<TradeStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [syncing, setSyncing] = useState(false);

  // 每行的编辑缓�? trade_id �?RowEditState
  const [edits, setEdits] = useState<Record<string, RowEditState>>({});

  // ── 数据加载 ──────────────────────────────────────────────
  const fetchData = useCallback(async (resetEdits: boolean = false) => {
    setLoading(true);
    setError(null);
    try {
      const params: { symbol?: string; limit?: number } = {};
      if (searchQuery.trim()) params.symbol = searchQuery.trim().toUpperCase();
      const [tradesRes, statsRes] = await Promise.all([
        api.getTradeLog(params),
        api.getTradeStats(),
      ]);
      setTrades(tradesRes);
      setStats(statsRes);

      // 只在首次加载或同步后重置编辑缓存
      if (resetEdits || Object.keys(edits).length === 0) {
        const initialEdits: Record<string, RowEditState> = {};
        for (const entry of tradesRes) {
          initialEdits[entry.trade_id] = {
            note: entry.note ?? "",
            disciplineScore: entry.discipline_score,
            saving: false,
            saved: false,
          };
        }
        setEdits(initialEdits);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [searchQuery]);

  useEffect(() => {
    fetchData(Object.keys(edits).length === 0);
  }, [searchQuery]); // eslint-disable-line

  const handleSync = async () => {
    setSyncing(true);
    try {
      await api.syncTradeLogs();
      fetchData(false);
    } catch (e) {
      console.error(e);
    } finally {
      setSyncing(false);
    }
  };

  // ── 编辑操作 ──────────────────────────────────────────────
  function updateEdit(tradeId: string, patch: Partial<RowEditState>) {
    setEdits((prev) => {
      const cur = prev[tradeId];
      if (!cur) return prev;
      return { ...prev, [tradeId]: { ...cur, ...patch } };
    });
  }

  async function handleSave(tradeId: string) {
    const cur = edits[tradeId];
    if (!cur || cur.saving) return;

    updateEdit(tradeId, { saving: true, saved: false });
    try {
      await api.updateTradeNote(tradeId, {
        note: cur.note || undefined,
        discipline_score: cur.disciplineScore,
      });
      updateEdit(tradeId, { saving: false, saved: true });
      setTimeout(() => updateEdit(tradeId, { saved: false }), 2000);
    } catch {
      updateEdit(tradeId, { saving: false });
    }
  }

  // ── 加载状�?──────────────────────────────────────────────
  if (loading) {
    return (
      <div className="max-w-6xl mx-auto p-4 md:p-6 lg:p-8">
        <div className="flex items-center justify-center h-64 text-muted-foreground">
          {t("tradeLog.loading")}
        </div>
      </div>
    );
  }

  // ── 错误状�?──────────────────────────────────────────────
  if (error) {
    return (
      <div className="max-w-6xl mx-auto p-4 md:p-6 lg:p-8">
        <div className="border rounded-lg p-8 text-center">
          <p className="text-muted-foreground mb-2">{error}</p>
        </div>
      </div>
    );
  }

  // ── 正常渲染 ──────────────────────────────────────────────
  return (
    <div className="max-w-6xl mx-auto p-4 md:p-6 lg:p-8 space-y-6">
      {/* 标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl md:text-2xl font-bold">{t("tradeLog.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("tradeLog.subtitle")}</p>
        </div>
        <button
          onClick={handleSync}
          disabled={syncing}
          className="min-h-[44px] px-4 py-2 border rounded-lg flex items-center gap-2 text-sm hover:bg-accent disabled:opacity-50 transition-colors"
          title="同步 OKX 成交记录"
        >
          <RefreshCw className={`h-4 w-4 ${syncing ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">{syncing ? "同步中..." : "同步"}</span>
        </button>
      </div>

      {/* 统计卡片 */}
      {stats && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            icon={<Target className="h-5 w-5" />}
            label={t("tradeLog.tradeCount")}
            value={`${stats.closed_trades}`}
          />
          <StatCard
            icon={stats.win_rate > 50 ? <TrendingUp className="h-5 w-5" /> : <TrendingDown className="h-5 w-5" />}
            label={t("tradeLog.winRate")}
            value={`${fmt(stats.win_rate * 100, 1)}%`}
          />
          <StatCard
            icon={stats.total_pnl > 0 ? <TrendingUp className="h-5 w-5" /> : <TrendingDown className="h-5 w-5" />}
            label={t("tradeLog.totalPnl")}
            value={`$${fmt(stats.total_pnl)}`}
            valueClass={pnlColor(stats.total_pnl)}
          />
          <StatCard
            icon={<Target className="h-5 w-5" />}
            label={t("tradeLog.avgDiscipline")}
            value={stats.scored_trades === 0 ? t("tradeLog.insufficientData") : `${fmt(stats.avg_discipline_score, 1)}/10`}
          />
        </div>
      )}

      {/* 搜索�?+ 刷新 */}
      <div className="flex flex-col sm:flex-row gap-2">
        <div className="relative w-full md:w-64">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
          <input
            type="text"
            className="w-full pl-9 pr-3 py-2 min-h-[44px] border rounded-lg bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
            placeholder={t("tradeLog.searchPlaceholder")}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && fetchData(true)}
          />
        </div>
        <button
          onClick={() => fetchData(true)}
          className="min-h-[44px] px-4 border rounded-lg text-sm font-medium hover:bg-muted transition-colors"
          type="button"
        >
          {t("newsCenter.refresh")}
        </button>
      </div>

      {/* 空状�?*/}
      {trades.length === 0 ? (
        <div className="border rounded-lg p-8 text-center text-muted-foreground">
          <Target className="h-8 w-8 mx-auto mb-2 opacity-50" />
          <p>{t("tradeLog.noTrades")}</p>
        </div>
      ) : (
        <>
          {/* ── 桌面端表�?─────────────────────────────────── */}
          <div className="hidden md:block overflow-x-auto -mx-4 px-4 md:mx-0 md:px-0">
            <table className="w-full min-w-[800px] text-sm">
              <thead>
                <tr className="border-b text-muted-foreground text-left">
                  <th className="py-2 font-medium">{t("tradeLog.symbol")}</th>
                  <th className="py-2 font-medium">{t("tradeLog.side")}</th>
                  <th className="py-2 font-medium">{t("tradeLog.price")}</th>
                  <th className="py-2 font-medium">{t("tradeLog.pnl")}</th>
                  <th className="py-2 font-medium">{t("tradeLog.time")}</th>
                  <th className="py-2 font-medium">{t("tradeLog.note")}</th>
                  <th className="py-2 font-medium">{t("tradeLog.disciplineScore")}</th>
                  <th className="py-2 font-medium w-[80px]"></th>
                </tr>
              </thead>
              <tbody>
                {trades.map((entry) => {
                  const ed = edits[entry.trade_id];
                  const sideLabel = entry.side === "buy" ? t("tradeLog.buy") : t("tradeLog.sell");
                  return (
                    <tr key={entry.trade_id} className="border-b last:border-0">
                      <td className="py-2 font-medium">{entry.symbol}</td>
                      <td className="py-2">
                        <span className={entry.side === "buy" ? "text-green-500" : "text-red-500"}>
                          {sideLabel}
                        </span>
                      </td>
                      <td className="py-2">${fmt(entry.price)}</td>
                      <td className={`py-2 ${pnlColor(entry.pnl)}`}>${fmt(entry.pnl)}</td>
                      <td className="py-2 text-muted-foreground">{formatTime(entry.fill_time)}</td>
                      <td className="py-2">
                        <input
                          type="text"
                          className="w-full min-h-[44px] px-2 py-1 border rounded bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/20"
                          value={ed?.note ?? ""}
                          onChange={(e) => updateEdit(entry.trade_id, { note: e.target.value, saved: false })}
                          placeholder={t("tradeLog.note")}
                        />
                      </td>
                      <td className="py-2">
                        <input
                          type="number"
                          className="w-16 min-h-[44px] px-2 py-1 border rounded bg-background text-sm text-center focus:outline-none focus:ring-2 focus:ring-primary/20"
                          value={ed?.disciplineScore || entry.discipline_score || ""}
                          placeholder={t("tradeLog.notRated")}
                          min={1}
                          max={10}
                          onChange={(e) => {
                            const v = Math.min(10, Math.max(1, Number(e.target.value) || 1));
                            updateEdit(entry.trade_id, { disciplineScore: v, saved: false });
                          }}
                        />
                      </td>
                      <td className="py-2">
                        {ed?.saved ? (
                          <span className="text-xs text-green-500">{t("tradeLog.saved")}</span>
                        ) : (
                          <button
                            type="button"
                            className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center text-muted-foreground hover:text-foreground disabled:opacity-30 transition-colors"
                            disabled={ed?.saving}
                            onClick={() => handleSave(entry.trade_id)}
                          >
                            <Save className="h-4 w-4" />
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* ── 移动端卡�?─────────────────────────────────── */}
          <div className="md:hidden space-y-3">
            {trades.map((entry) => {
              const ed = edits[entry.trade_id];
              const sideLabel = entry.side === "buy" ? t("tradeLog.buy") : t("tradeLog.sell");
              return (
                <div key={entry.trade_id} className="border rounded-lg p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold">{entry.symbol}</span>
                    <span
                      className={`text-sm font-medium ${
                        entry.side === "buy" ? "text-green-500" : "text-red-500"
                      }`}
                    >
                      {sideLabel}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    <div>
                      <span className="text-muted-foreground">{t("tradeLog.price")}:</span> $
                      {fmt(entry.price)}
                    </div>
                    <div className={pnlColor(entry.pnl)}>
                      <span className="text-muted-foreground">{t("tradeLog.pnl")}:</span> $
                      {fmt(entry.pnl)}
                    </div>
                    <div className="col-span-2 text-muted-foreground">
                      {t("tradeLog.time")}: {formatTime(entry.fill_time)}
                    </div>
                  </div>
                  <div>
                    <label className="text-xs text-muted-foreground block mb-1">
                      {t("tradeLog.note")}
                    </label>
                    <input
                      type="text"
                      className="w-full min-h-[44px] px-2 py-1 border rounded bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/20"
                      value={ed?.note ?? ""}
                      onChange={(e) => updateEdit(entry.trade_id, { note: e.target.value, saved: false })}
                      placeholder={t("tradeLog.note")}
                    />
                  </div>
                  <div className="flex items-end gap-3">
                    <div className="flex-1">
                      <label className="text-xs text-muted-foreground block mb-1">
                        {t("tradeLog.disciplineScore")}
                      </label>
                      <input
                        type="number"
                        className="w-full min-h-[44px] px-2 py-1 border rounded bg-background text-sm text-center focus:outline-none focus:ring-2 focus:ring-primary/20"
                        value={ed?.disciplineScore || entry.discipline_score || ""}
                        placeholder={t("tradeLog.notRated")}
                        min={1}
                        max={10}
                        onChange={(e) => {
                          const v = Math.min(10, Math.max(1, Number(e.target.value) || 1));
                          updateEdit(entry.trade_id, { disciplineScore: v, saved: false });
                        }}
                      />
                    </div>
                    <button
                      type="button"
                      className="min-h-[44px] px-4 border rounded-lg text-sm font-medium hover:bg-muted transition-colors disabled:opacity-30"
                      disabled={ed?.saving}
                      onClick={() => handleSave(entry.trade_id)}
                    >
                      {ed?.saved ? t("tradeLog.saved") : ed?.saving ? "保存中..." : t("tradeLog.save")}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

// ── 统计卡片子组�?──────────────────────────────────────────
function StatCard({
  icon,
  label,
  value,
  valueClass,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="border rounded-lg p-4 md:p-5 space-y-1">
      <div className="flex items-center gap-2 text-muted-foreground">
        {icon}
        <span className="text-xs md:text-sm">{label}</span>
      </div>
      <p className={`text-xl md:text-2xl font-bold ${valueClass || ""}`}>{value}</p>
    </div>
  );
}
