import { useEffect, useState } from "react";

import { bangkokClock } from "../lib/runtime";

export function BangkokClock() {
  const [now, setNow] = useState(() => new Date().toISOString());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date().toISOString()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  return <time className="operator-clock" dateTime={now}><strong>{bangkokClock(now)}</strong><span>BKK · UTC+7</span></time>;
}
