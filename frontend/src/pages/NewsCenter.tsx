import { useState, useEffect, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Newspaper, Search, RefreshCw, ExternalLink, Rss } from "lucide-react";
import { api, type ApiError } from "@/lib/api";

// --- Types (matching backend contracts) ---

interface NewsItem {
  title: string;
  source: string;
  summary: string;
  published_at: string;
  link: string;
}

interface NewsSource {
  name: string;
  status: "connected" | "error";
}

// --- Source badge color mapping ---

const SOURCE_COLORS: Record<string, string> = {
  CoinDesk: "bg-blue-100 text-blue-700",
  CoinTelegraph: "bg-purple-100 text-purple-700",
  CryptoSlate: "bg-green-100 text-green-700",
  Decrypt: "bg-orange-100 text-orange-700",
  "The Block": "bg-pink-100 text-pink-700",
  "Bitcoin Magazine": "bg-yellow-100 text-yellow-700",
};

const KNOWN_SOURCES = [
  "CoinDesk",
  "CoinTelegraph",
  "CryptoSlate",
  "Decrypt",
  "The Block",
  "Bitcoin Magazine",
];

// --- Relative time helper (no external date libraries) ---

function relativeTime(dateStr: string): string {
  const now = Date.now();
  const date = new Date(dateStr).getTime();
  const diffMs = now - date;
  const diffMinutes = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMinutes < 1) return "刚刚";
  if (diffMinutes < 60) return `${diffMinutes}分钟前`;
  if (diffHours < 24) return `${diffHours}小时前`;
  if (diffDays < 7) return `${diffDays}天前`;

  const d = new Date(dateStr);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// --- Component ---

export function NewsCenter() {
  const { t } = useTranslation();
  const [news, setNews] = useState<NewsItem[]>([]);
  const [sources, setSources] = useState<NewsSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [selectedSource, setSelectedSource] = useState("");

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [newsData, sourcesData] = await Promise.all([
        api.getNews({
          keyword: keyword || undefined,
          source: selectedSource || undefined,
        }),
        api.getNewsSources(),
      ]);
      setNews(newsData);
      setSources(sourcesData);
    } catch (e) {
      const apiErr = e as ApiError;
      setError(apiErr.message || "加载新闻失败");
    } finally {
      setLoading(false);
    }
  }, [keyword, selectedSource]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleRefresh = () => {
    fetchData();
  };

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    fetchData();
  };

  const getSourceStatus = (name: string): NewsSource | undefined =>
    sources.find((s) => s.name === name);

  return (
    <div className="max-w-6xl mx-auto p-4 md:p-6 lg:p-8 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl md:text-2xl font-bold">
            {t("newsCenter.title")}
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            {t("newsCenter.subtitle")}
          </p>
        </div>
        <button
          onClick={handleRefresh}
          disabled={loading}
          className="min-h-[44px] px-4 py-2 border rounded-lg flex items-center gap-2 text-sm hover:bg-accent disabled:opacity-50 transition-colors"
          aria-label={t("newsCenter.refresh")}
        >
          <RefreshCw
            className={`h-4 w-4 ${loading ? "animate-spin" : ""}`}
          />
          <span className="hidden sm:inline">{t("newsCenter.refresh")}</span>
        </button>
      </div>

      {/* RSS Source Status Bar */}
      <div>
        <h2 className="text-sm font-medium text-muted-foreground mb-2 flex items-center gap-1.5">
          <Rss className="h-3.5 w-3.5" />
          {t("newsCenter.rssStatus")}
        </h2>
        <div className="flex gap-2 overflow-x-auto pb-2 scrollbar-thin">
          {KNOWN_SOURCES.map((name) => {
            const src = getSourceStatus(name);
            const status = src?.status;
            return (
              <div
                key={name}
                className="flex items-center gap-1.5 px-3 py-1.5 border rounded-full text-xs whitespace-nowrap flex-shrink-0"
              >
                <span
                  className={`h-2 w-2 rounded-full flex-shrink-0 ${
                    status === "connected"
                      ? "bg-green-500"
                      : status === "error"
                        ? "bg-red-500"
                        : "bg-gray-300"
                  }`}
                />
                <span>{name}</span>
                {status && (
                  <span
                    className={
                      status === "connected"
                        ? "text-green-600"
                        : "text-red-600"
                    }
                  >
                    {status === "connected"
                      ? t("newsCenter.connected")
                      : t("newsCenter.error")}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Search Bar */}
      <form
        onSubmit={handleSearchSubmit}
        className="flex flex-col sm:flex-row gap-3"
      >
        <div className="relative flex-1 w-full md:w-64">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder={t("newsCenter.searchPlaceholder")}
            className="w-full pl-10 pr-4 py-2 border rounded-lg text-sm bg-background min-h-[44px]"
          />
        </div>
        <select
          value={selectedSource}
          onChange={(e) => setSelectedSource(e.target.value)}
          className="w-full md:w-64 px-3 py-2 border rounded-lg text-sm bg-background min-h-[44px]"
        >
          <option value="">{t("newsCenter.allSources")}</option>
          {KNOWN_SOURCES.map((src) => (
            <option key={src} value={src}>
              {src}
            </option>
          ))}
          {sources
            .filter((s) => !KNOWN_SOURCES.includes(s.name))
            .map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
              </option>
            ))}
        </select>
      </form>

      {/* Error State */}
      {error && (
        <div className="border rounded-lg p-8 text-center">
          <p className="text-muted-foreground mb-3">{error}</p>
          <button
            onClick={handleRefresh}
            className="min-h-[44px] px-4 py-2 text-sm border rounded-lg hover:bg-accent transition-colors"
          >
            {t("newsCenter.refresh")}
          </button>
        </div>
      )}

      {/* Loading State */}
      {loading && !error && news.length === 0 && (
        <div className="flex items-center justify-center h-64 text-muted-foreground">
          {t("newsCenter.loading")}
        </div>
      )}

      {/* Empty State */}
      {!loading && !error && news.length === 0 && (
        <div className="border rounded-lg p-8 text-center text-muted-foreground">
          <Newspaper className="h-8 w-8 mx-auto mb-2 opacity-50" />
          <p>{t("newsCenter.noNews")}</p>
        </div>
      )}

      {/* News List */}
      {news.length > 0 && (
        <div className="space-y-3">
          {news.map((item, i) => (
            <article
              key={`${item.link}-${i}`}
              className="border rounded-lg p-4 md:p-5 hover:bg-accent/30 transition-colors"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  {/* Source badge + time */}
                  <div className="flex items-center gap-2 mb-2 flex-wrap">
                    <span
                      className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                        SOURCE_COLORS[item.source] ||
                        "bg-gray-100 text-gray-700"
                      }`}
                    >
                      {item.source}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {relativeTime(item.published_at)}
                    </span>
                  </div>

                  {/* Title */}
                  <a
                    href={item.link}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm md:text-base font-semibold hover:text-primary transition-colors line-clamp-2 flex items-start gap-1 group"
                  >
                    <span className="flex-1">{item.title}</span>
                    <ExternalLink className="h-3.5 w-3.5 flex-shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 transition-opacity" />
                  </a>

                  {/* Summary */}
                  <p className="text-xs md:text-sm text-muted-foreground mt-1.5 line-clamp-2">
                    {item.summary && item.summary.length > 150
                      ? item.summary.slice(0, 150) + "..."
                      : item.summary}
                  </p>
                </div>

                {/* Mobile external link button */}
                <a
                  href={item.link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="min-h-[44px] min-w-[44px] flex items-center justify-center border rounded-lg flex-shrink-0 hover:bg-accent transition-colors md:hidden"
                  aria-label={t("newsCenter.readMore")}
                >
                  <ExternalLink className="h-4 w-4" />
                </a>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
