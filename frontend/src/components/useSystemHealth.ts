import { useContext } from "react";

import { systemHealthContext } from "./systemHealthContext";

export function useSystemHealth() {
  const health = useContext(systemHealthContext);
  if (!health) throw new Error("SystemHealthProvider is required");
  return health;
}
