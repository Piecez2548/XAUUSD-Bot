import type { ReactNode } from "react";

export function Panel({
  title,
  kicker,
  action,
  className = "",
  children,
}: {
  title: string;
  kicker?: string;
  action?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-header">
        <div className="panel-title-row">
          <h2>{title}</h2>
          {kicker && <span className="panel-context">{kicker}</span>}
        </div>
        {action && <div className="panel-action">{action}</div>}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}
