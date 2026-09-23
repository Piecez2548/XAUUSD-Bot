import { useCallback, useEffect, useRef, useState } from "react";

import { getJson } from "../lib/api";

export interface ApiState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useApi<T>(path: string, refreshMs = 0): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const activeRequest = useRef<symbol | null>(null);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      if (activeRequest.current !== null) return;
      const token = Symbol(path);
      activeRequest.current = token;
      try {
        const next = await getJson<T>(path, controller.signal);
        setData(next);
        setError(null);
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Request failed");
        }
      } finally {
        if (activeRequest.current === token) {
          activeRequest.current = null;
          if (!controller.signal.aborted) setLoading(false);
        }
      }
    };
    void load();
    const timer = refreshMs > 0 ? window.setInterval(load, refreshMs) : undefined;
    return () => {
      controller.abort();
      activeRequest.current = null;
      if (timer) window.clearInterval(timer);
    };
  }, [path, refreshMs, revision]);

  return { data, loading, error, refresh };
}
