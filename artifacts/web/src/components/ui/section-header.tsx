import * as React from "react"

import { cn } from "@/lib/utils"

export interface SectionHeaderProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  title: React.ReactNode
  subtitle?: React.ReactNode
  actions?: React.ReactNode
  eyebrow?: React.ReactNode
  align?: "left" | "center"
}

const SectionHeader = React.forwardRef<HTMLDivElement, SectionHeaderProps>(
  ({ className, title, subtitle, actions, eyebrow, align = "left", ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "mb-3 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between",
          align === "center" && "items-center text-center sm:flex-col",
          className,
        )}
        {...props}
      >
        <div className={cn("min-w-0", align === "center" && "w-full")}>
          {eyebrow ? (
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              {eyebrow}
            </div>
          ) : null}
          <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
            {title}
          </h2>
          {subtitle ? (
            <p className="mt-1 text-xs text-muted-foreground/80">{subtitle}</p>
          ) : null}
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
    )
  },
)
SectionHeader.displayName = "SectionHeader"

export { SectionHeader }
