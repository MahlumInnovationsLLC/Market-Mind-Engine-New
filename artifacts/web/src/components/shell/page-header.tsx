import type { ReactNode } from "react";

interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: PageHeaderProps) {
  return (
    <div
      className="border-b border-border pb-4 mb-6 flex flex-col md:flex-row md:items-end justify-between gap-3"
      data-testid="shell-page-header"
    >
      <div>
        {eyebrow ? (
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-1">
            {eyebrow}
          </div>
        ) : null}
        <h1 className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase">
          {title}
        </h1>
        {description ? (
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}
