import { Suspense, lazy, type ComponentType } from "react";
import { createBrowserRouter } from "react-router-dom";
import { Layout } from "@/components/layout/Layout";

const Home = lazy(() => import("@/pages/Home").then((m) => ({ default: m.Home })));
const Agent = lazy(() => import("@/pages/Agent").then((m) => ({ default: m.Agent })));
const Settings = lazy(() =>
  import("@/pages/Settings").then((m) => ({ default: m.Settings })),
);
const TradeLog = lazy(() =>
  import("@/pages/TradeLog").then((m) => ({ default: m.TradeLog })),
);
const NewsCenter = lazy(() =>
  import("@/pages/NewsCenter").then((m) => ({ default: m.NewsCenter })),
);
const Portfolio = lazy(() =>
  import("@/pages/Portfolio").then((m) => ({ default: m.Portfolio })),
);

function PageLoader() {
  return (
    <div className="flex h-[60vh] items-center justify-center text-muted-foreground">
      加载中…
    </div>
  );
}

function wrap(Component: ComponentType) {
  return (
    <Suspense fallback={<PageLoader />}>
      <Component />
    </Suspense>
  );
}

export const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: wrap(Home) },
      { path: "/agent", element: wrap(Agent) },
      { path: "/settings", element: wrap(Settings) },
      { path: "/trade-log", element: wrap(TradeLog) },
      { path: "/news", element: wrap(NewsCenter) },
      { path: "/portfolio", element: wrap(Portfolio) },
    ],
  },
]);
