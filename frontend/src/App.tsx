import { createBrowserRouter, RouterProvider } from "react-router-dom";

import { AppErrorBoundary } from "./components/AppErrorBoundary";
import { AuthGate } from "./auth/AuthGate";
import { dashboardRoutes } from "./routes";

const router = createBrowserRouter(dashboardRoutes);

export function App() { return <AppErrorBoundary><AuthGate><RouterProvider router={router} /></AuthGate></AppErrorBoundary>; }
