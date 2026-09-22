import { unavailable, utcTime } from "../lib/format";
import type { SystemEvent } from "../types";

export function EventTable({ events, description, includeDate = false }: { events: SystemEvent[]; description: (event: SystemEvent) => string; includeDate?: boolean }) {
  return <div className="event-stream" tabIndex={0} aria-label="System event table, horizontally scrollable"><table className="event-table"><caption className="sr-only">Recorded system events in reverse chronological order</caption><thead><tr><th scope="col">UTC</th><th scope="col">Severity</th><th scope="col">Source</th><th scope="col">Event</th><th scope="col">Description</th><th scope="col">Correlation ID</th></tr></thead><tbody>{events.map((event) => <tr key={event.event_id}><td><time dateTime={event.timestamp}>{utcTime(event.timestamp, includeDate)}</time></td><td><span className={`severity ${event.severity.toLowerCase()}`}>{event.severity}</span></td><td>{event.source}</td><td><strong>{event.event_type}</strong></td><td>{description(event)}</td><td><code>{event.correlation_id?.slice(0, 12) ?? unavailable}</code></td></tr>)}</tbody></table></div>;
}
