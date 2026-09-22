import { createBrowserRouter, RouterProvider } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import {
  DecisionsPage,
  HealthPage,
  LogsPage,
  MarketPage,
  NewsPage,
  PerformancePage,
  RiskPage,
  SettingsPage,
  ShadowPage,
  TradeDetailPage,
  TradesPage,
} from "./pages/ModulePages";
import { OverviewPage } from "./pages/OverviewPage";

const router = createBrowserRouter([
  {
    element: <AppShell />,
    children: [
      { path: "/", element: <OverviewPage /> },
      { path: "/trades", element: <TradesPage /> },
      { path: "/trades/:tradeId", element: <TradeDetailPage /> },
      { path: "/decisions", element: <DecisionsPage /> },
      { path: "/shadow", element: <ShadowPage /> },
      { path: "/performance", element: <PerformancePage /> },
      { path: "/risk", element: <RiskPage /> },
      { path: "/market", element: <MarketPage /> },
      { path: "/news", element: <NewsPage /> },
      { path: "/health", element: <HealthPage /> },
      { path: "/logs", element: <LogsPage /> },
      { path: "/settings", element: <SettingsPage /> },
    ],
  },
]);

export function App() { return <RouterProvider router={router} />; }
