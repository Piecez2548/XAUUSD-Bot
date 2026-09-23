import type { ReactNode } from "react";

import { useApi } from "../hooks/useApi";
import type { HealthResponse } from "../types";
import { systemHealthContext } from "./systemHealthContext";

export function SystemHealthProvider({ children }: { children: ReactNode }) {
  const health = useApi<HealthResponse>("/api/system/health", 10_000);
  return <systemHealthContext.Provider value={health}>{children}</systemHealthContext.Provider>;
}
