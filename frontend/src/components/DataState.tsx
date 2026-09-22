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
    return <div className="empty-state error-state" role="alert"><AlertTriangle size={21} aria-hidden="true" /><strong>Data unavailable</strong><span>{error}. No value is being inferred.</span></div>;
  }
  return <EmptyState title={emptyTitle} detail={emptyDetail} />;
}
