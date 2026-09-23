import { createBrowserRouter, RouterProvider } from "react-router-dom";

import { AppErrorBoundary } from "./components/AppErrorBoundary";
import { dashboardRoutes } from "./routes";

const router = createBrowserRouter(dashboardRoutes);

export function App() { return <AppErrorBoundary><RouterProvider router={router} /></AppErrorBoundary>; }
