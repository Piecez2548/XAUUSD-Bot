import { AlertTriangle, LoaderCircle } from "lucide-react";

import { EmptyState } from "./EmptyState";

export function DataState({
  loading,
  error,
  emptyTitle,
  emptyDetail,
}: {
  loading: boolean;
  error: string | null;
  emptyTitle: string;
  emptyDetail: string;
}) {
  if (loading) {
    return <div className="empty-state loading-state" role="status"><LoaderCircle size={21} aria-hidden="true" /><strong>Loading verified data</strong><span>Waiting for the read-only API response.</span></div>;
  }
  if (error) {
    const offline = error.includes("BACKEND_NOT_CONFIGURED") || error.includes("Failed to fetch");
    return <div className="empty-state error-state" role="alert"><AlertTriangle size={21} aria-hidden="true" /><strong>{offline ? "Trading Runtime ออฟไลน์" : "Data unavailable"}</strong><span>{offline ? "Dashboard ออนไลน์ แต่ Trading Runtime บนเครื่องไม่ได้เชื่อมต่อ" : `${error}. No value is being inferred.`}</span></div>;
  }
  return <EmptyState title={emptyTitle} detail={emptyDetail} />;
}
