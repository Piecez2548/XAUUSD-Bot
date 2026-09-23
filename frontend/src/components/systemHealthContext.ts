import { createContext } from "react";

import type { ApiState } from "../hooks/useApi";
import type { HealthResponse } from "../types";

export const systemHealthContext = createContext<ApiState<HealthResponse> | null>(null);
