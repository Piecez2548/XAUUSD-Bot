import { createContext, useContext } from "react";

import type { AuthSession } from "../lib/api";

export const AuthSessionContext = createContext<AuthSession | null>(null);

export function useCurrentAuthSession(): AuthSession | null {
  return useContext(AuthSessionContext);
}
