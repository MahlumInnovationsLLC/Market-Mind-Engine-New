import * as React from "react"

import { cn } from "@/lib/utils"

const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          "flex h-9 w-full rounded-md border [border-color:var(--border-subtle)] bg-navy-900/40 px-3 py-1 text-base shadow-sm transition-[color,box-shadow,border-color] file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:[border-color:hsl(var(--accent-cyan)/0.55)] focus-visible:shadow-[0_0_0_1px_hsl(var(--accent-cyan)/0.35),0_0_14px_-4px_hsl(var(--accent-cyan)/0.55)] disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
          className
        )}
        ref={ref}
        {...props}
      />
    )
  }
)
Input.displayName = "Input"

export { Input }
