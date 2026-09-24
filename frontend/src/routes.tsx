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
import { ResearchPage } from "./pages/ResearchPage";
import { ForwardValidationPage } from "./pages/ForwardValidationPage";
import { OperatorControlPage } from "./pages/OperatorControlPage";

export const dashboardRoutes = [
  {
    element: <AppShell />,
    children: [
      { path: "/", element: <OverviewPage /> },
      { path: "/trades", element: <TradesPage /> },
      { path: "/trades/:tradeId", element: <TradeDetailPage /> },
      { path: "/decisions", element: <DecisionsPage /> },
      { path: "/shadow", element: <ShadowPage /> },
      { path: "/research", element: <ResearchPage /> },
      { path: "/forward", element: <ForwardValidationPage /> },
      { path: "/forward-validation", element: <ForwardValidationPage /> },
      { path: "/performance", element: <PerformancePage /> },
      { path: "/risk", element: <RiskPage /> },
      { path: "/market", element: <MarketPage /> },
      { path: "/news", element: <NewsPage /> },
      { path: "/health", element: <HealthPage /> },
      { path: "/logs", element: <LogsPage /> },
      { path: "/settings", element: <SettingsPage /> },
      { path: "/control", element: <OperatorControlPage /> },
    ],
  },
];
