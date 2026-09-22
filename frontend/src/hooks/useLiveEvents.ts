import { useEffect, useState } from "react";

import { websocketUrl } from "../lib/api";
import type { SystemEvent } from "../types";

export function useLiveEvents(seed: SystemEvent[]): { events: SystemEvent[]; connected: boolean; healthRevision: number } {
  const [events, setEvents] = useState(seed);
  const [connected, setConnected] = useState(false);
  const [healthRevision, setHealthRevision] = useState(0);

  useEffect(() => setEvents(seed), [seed]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let disposed = false;
    const connect = () => {
      socket = new WebSocket(websocketUrl());
      socket.onopen = () => setConnected(true);
      socket.onmessage = (message) => {
        const envelope = JSON.parse(message.data as string) as {
          type: string;
          event?: SystemEvent;
        };
        if (envelope.type === "domain_event" && envelope.event) {
          setEvents((current) => {
            if (current.some((item) => item.event_id === envelope.event?.event_id)) return current;
            return [envelope.event as SystemEvent, ...current].slice(0, 500);
          });
        }
        if (envelope.type === "system_health") setHealthRevision((revision) => revision + 1);
      };
      socket.onclose = () => {
        setConnected(false);
        if (!disposed) retry = window.setTimeout(connect, 3000);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      disposed = true;
      if (retry) window.clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return { events, connected, healthRevision };
}
