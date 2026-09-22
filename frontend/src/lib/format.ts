export const unavailable = "—";

export function number(value: number | null | undefined, digits: number | string = 2): string {
  return value == null || !Number.isFinite(value)
    ? typeof digits === "string" ? digits : unavailable
    : new Intl.NumberFormat("en-US", {
        minimumFractionDigits: typeof digits === "number" ? digits : 2,
        maximumFractionDigits: typeof digits === "number" ? digits : 2,
      }).format(value);
}

export function money(value: number | null | undefined, currency = "USD"): string {
  return value == null || !Number.isFinite(value)
    ? unavailable
    : new Intl.NumberFormat("en-US", { style: "currency", currency }).format(value);
}

export function percent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? unavailable : `${number(value)}%`;
}

export function utcTime(value: string | null | undefined, includeDate = false): string {
  if (!value) return unavailable;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return unavailable;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    day: includeDate ? "2-digit" : undefined,
    month: includeDate ? "short" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed);
}
