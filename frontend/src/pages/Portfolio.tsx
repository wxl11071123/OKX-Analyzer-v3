import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { PieChart, TrendingUp, TrendingDown, Wallet } from "lucide-react";
import { api, type ApiError } from "@/lib/api";

interface TradingAccount {
  total_equity: number;
  available_balance: number;
  unrealized_pnl: number;
  realized_pnl: number;
  currency: string;
}

interface TradingPosition {
  symbol: string;
  side: string;
  quantity: number;
  avg_price: number;
  mark_price: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  notional: number;
}

function fmt(n: number, d = 2) {
  return n.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function pnlColor(v: number) {
  return v > 0 ? "text-green-500" : v < 0 ? "text-red-500" : "text-muted-foreground";
}

export function Portfolio() {
  const { t } = useTranslation();
  const [account, setAccount] = useState<TradingAccount | null>(null);
  const [positions, setPositions] = useState<TradingPosition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      api.getTradingAccount().catch(() => null),
      api.getTradingPositions().catch(() => null),
    ])
      .then(([acc, pos]) => {
        setAccount(acc as TradingAccount);
        setPositions(pos as TradingPosition[] || []);
      })
      .catch((e: ApiError) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="max-w-6xl mx-auto p-4 md:p-6 lg:p-8">
        <div className="flex items-center justify-center h-64 text-muted-foreground">
          {t("portfolio.loading")}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-6xl mx-auto p-4 md:p-6 lg:p-8">
        <div className="border rounded-lg p-8 text-center">
          <p className="text-muted-foreground mb-2">{error}</p>
          <p className="text-sm text-muted-foreground/60">{t("portfolio.disclaimer")}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto p-4 md:p-6 lg:p-8 space-y-6">
      <div>
        <h1 className="text-xl md:text-2xl font-bold">{t("portfolio.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">{t("portfolio.subtitle")}</p>
      </div>

      {/* Account overview cards */}
      {account && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            icon={<Wallet className="h-5 w-5" />}
            label={t("portfolio.totalEquity")}
            value={`$${fmt(account.total_equity)}`}
          />
          <StatCard
            icon={<Wallet className="h-5 w-5" />}
            label={t("portfolio.availableBalance")}
            value={`$${fmt(account.available_balance)}`}
          />
          <StatCard
            icon={account.unrealized_pnl > 0 ? <TrendingUp className="h-5 w-5" /> : <TrendingDown className="h-5 w-5" />}
            label={t("portfolio.unrealizedPnl")}
            value={`$${fmt(account.unrealized_pnl)}`}
            valueClass={pnlColor(account.unrealized_pnl)}
          />
          <StatCard
            icon={account.realized_pnl > 0 ? <TrendingUp className="h-5 w-5" /> : <TrendingDown className="h-5 w-5" />}
            label={t("portfolio.realizedPnl")}
            value={`$${fmt(account.realized_pnl)}`}
            valueClass={pnlColor(account.realized_pnl)}
          />
        </div>
      )}

      {/* Positions */}
      <div>
        <h2 className="text-lg font-semibold mb-3">
          {t("portfolio.symbol")} ({positions.length})
        </h2>

        {positions.length === 0 ? (
          <div className="border rounded-lg p-8 text-center text-muted-foreground">
            <PieChart className="h-8 w-8 mx-auto mb-2 opacity-50" />
            <p>{t("portfolio.noPositions")}</p>
          </div>
        ) : (
          <>
            {/* Desktop table */}
            <div className="hidden md:block overflow-x-auto -mx-4 px-4 md:mx-0 md:px-0">
              <table className="w-full min-w-[600px] text-sm">
                <thead>
                  <tr className="border-b text-muted-foreground text-left">
                    <th className="py-2 font-medium">{t("portfolio.symbol")}</th>
                    <th className="py-2 font-medium">{t("portfolio.side")}</th>
                    <th className="py-2 font-medium">{t("portfolio.quantity")}</th>
                    <th className="py-2 font-medium">{t("portfolio.avgPrice")}</th>
                    <th className="py-2 font-medium">{t("portfolio.markPrice")}</th>
                    <th className="py-2 font-medium">{t("portfolio.pnl")}</th>
                    <th className="py-2 font-medium">{t("portfolio.pnlPct")}</th>
                    <th className="py-2 font-medium">{t("portfolio.notional")}</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p, i) => (
                    <tr key={`${p.symbol}-${i}`} className="border-b last:border-0">
                      <td className="py-2 font-medium">{p.symbol}</td>
                      <td className="py-2">
                        <span className={p.side === "long" ? "text-green-500" : "text-red-500"}>
                          {p.side === "long" ? t("portfolio.long") : t("portfolio.short")}
                        </span>
                      </td>
                      <td className="py-2">{fmt(p.quantity, 4)}</td>
                      <td className="py-2">${fmt(p.avg_price)}</td>
                      <td className="py-2">${fmt(p.mark_price)}</td>
                      <td className={`py-2 ${pnlColor(p.unrealized_pnl)}`}>${fmt(p.unrealized_pnl)}</td>
                      <td className={`py-2 ${pnlColor(p.unrealized_pnl_pct)}`}>{fmt(p.unrealized_pnl_pct)}%</td>
                      <td className="py-2">${fmt(p.notional)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile cards */}
            <div className="md:hidden space-y-3">
              {positions.map((p, i) => (
                <div key={`${p.symbol}-${i}`} className="border rounded-lg p-4 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold">{p.symbol}</span>
                    <span className={`text-sm font-medium ${p.side === "long" ? "text-green-500" : "text-red-500"}`}>
                      {p.side === "long" ? t("portfolio.long") : t("portfolio.short")}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    <div><span className="text-muted-foreground">{t("portfolio.quantity")}:</span> {fmt(p.quantity, 4)}</div>
                    <div><span className="text-muted-foreground">{t("portfolio.notional")}:</span> ${fmt(p.notional)}</div>
                    <div><span className="text-muted-foreground">{t("portfolio.avgPrice")}:</span> ${fmt(p.avg_price)}</div>
                    <div><span className="text-muted-foreground">{t("portfolio.markPrice")}:</span> ${fmt(p.mark_price)}</div>
                    <div className={`col-span-2 ${pnlColor(p.unrealized_pnl)}`}>
                      {t("portfolio.pnl")}: ${fmt(p.unrealized_pnl)} ({fmt(p.unrealized_pnl_pct)}%)
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      <p className="text-xs text-muted-foreground/60 pt-4 border-t">{t("portfolio.disclaimer")}</p>
    </div>
  );
}

function StatCard({ icon, label, value, valueClass }: {
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
