import { useTranslation } from "react-i18next";
import { TrendingUp, Sparkles, NotebookPen, Globe } from "lucide-react";

interface Example {
  titleKey: string;
  descKey: string;
  promptKey: string;
}

interface Category {
  labelKey: string;
  icon: React.ReactNode;
  color: string;
  examples: Example[];
}

const CATEGORIES: Category[] = [
  {
    labelKey: "welcome.categories.cryptoBacktest",
    icon: <TrendingUp className="h-4 w-4" />,
    color: "text-green-400 border-green-500/30 hover:border-green-500/60 hover:bg-green-500/5",
    examples: [
      { titleKey: "welcome.examples.btcEma", descKey: "welcome.examples.btcEmaDesc", promptKey: "welcome.examples.btcEmaPrompt" },
      { titleKey: "welcome.examples.ethRsi", descKey: "welcome.examples.ethRsiDesc", promptKey: "welcome.examples.ethRsiPrompt" },
      { titleKey: "welcome.examples.multiCoin", descKey: "welcome.examples.multiCoinDesc", promptKey: "welcome.examples.multiCoinPrompt" },
      { titleKey: "welcome.examples.btcMacd", descKey: "welcome.examples.btcMacdDesc", promptKey: "welcome.examples.btcMacdPrompt" },
    ],
  },
  {
    labelKey: "welcome.categories.researchAnalysis",
    icon: <Sparkles className="h-4 w-4" />,
    color: "text-amber-400 border-amber-500/30 hover:border-amber-500/60 hover:bg-amber-500/5",
    examples: [
      { titleKey: "welcome.examples.analyzeMarket", descKey: "welcome.examples.analyzeMarketDesc", promptKey: "welcome.examples.analyzeMarketPrompt" },
      { titleKey: "welcome.examples.checkNews", descKey: "welcome.examples.checkNewsDesc", promptKey: "welcome.examples.checkNewsPrompt" },
    ],
  },
  {
    labelKey: "welcome.categories.tradeJournal",
    icon: <NotebookPen className="h-4 w-4" />,
    color: "text-blue-400 border-blue-500/30 hover:border-blue-500/60 hover:bg-blue-500/5",
    examples: [
      { titleKey: "welcome.examples.analyzeJournal", descKey: "welcome.examples.analyzeJournalDesc", promptKey: "welcome.examples.analyzeJournalPrompt" },
    ],
  },
  {
    labelKey: "welcome.categories.news",
    icon: <Globe className="h-4 w-4" />,
    color: "text-purple-400 border-purple-500/30 hover:border-purple-500/60 hover:bg-purple-500/5",
    examples: [
      { titleKey: "welcome.examples.checkNews", descKey: "welcome.examples.checkNewsDesc", promptKey: "welcome.examples.checkNewsPrompt" },
    ],
  },
];

function Card({ category, onClick }: { category: Category; onClick: (promptKey: string) => void }) {
  const { t } = useTranslation();
  return (
    <div className={`border rounded-lg p-4 md:p-5 transition-colors ${category.color}`}>
      <div className="flex items-center gap-2 mb-3">
        {category.icon}
        <span className="text-sm font-semibold">{t(category.labelKey as any)}</span>
      </div>
      <div className="space-y-2">
        {category.examples.map((ex, i) => (
          <button
            key={`${ex.titleKey}-${i}`}
            onClick={() => onClick(t(ex.promptKey as any))}
            className="w-full text-left px-3 py-2 rounded-md text-xs md:text-sm hover:bg-accent/50 transition-colors min-h-[44px]"
          >
            <div className="font-medium">{t(ex.titleKey as any)}</div>
            <div className="text-muted-foreground text-xs mt-0.5">{t(ex.descKey as any)}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

export function WelcomeScreen({ onPrompt }: { onPrompt: (text: string) => void }) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col items-center justify-center min-h-[70vh] p-4 md:p-8">
      <div className="max-w-3xl w-full text-center space-y-6">
        <h1 className="text-3xl md:text-4xl font-bold tracking-tight">{t("welcome.title")}</h1>
        <p className="text-lg text-muted-foreground">{t("welcome.subtitle")}</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-12 max-w-4xl w-full">
        {CATEGORIES.map((cat) => (
          <Card key={cat.labelKey} category={cat} onClick={(promptKey: string) => onPrompt(promptKey)} />
        ))}
      </div>

      <div className="mt-12 grid grid-cols-2 sm:grid-cols-4 gap-3 max-w-3xl w-full">
        {(t("welcome.capabilities", { returnObjects: true }) as any as Record<string, string>) && Object.entries(t("welcome.capabilities", { returnObjects: true }) as any as Record<string, string>).map(([key, value]) => (
          <div key={key} className="border rounded-lg px-3 py-2 text-xs text-muted-foreground text-center">
            {value}
          </div>
        ))}
      </div>
    </div>
  );
}
