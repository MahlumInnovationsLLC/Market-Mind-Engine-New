import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const glassCardVariants = cva(
  "rounded-xl p-4 text-card-foreground transition-shadow",
  {
    variants: {
      tone: {
        glass: "glass",
        strong: "glass-strong",
        solid: "border bg-navy-850 [border-color:var(--border-subtle)] shadow-[var(--shadow-glass)]",
        outline: "border bg-transparent [border-color:var(--border-subtle)]",
      },
      glow: {
        none: "",
        cyan: "shadow-[var(--shadow-glow-cyan)]",
        green: "shadow-[var(--shadow-glow-green)]",
        red: "shadow-[var(--shadow-glow-red)]",
        purple: "shadow-[var(--shadow-glow-purple)]",
      },
      padding: {
        none: "p-0",
        sm: "p-3",
        md: "p-4",
        lg: "p-6",
      },
    },
    defaultVariants: {
      tone: "solid",
      glow: "none",
      padding: "md",
    },
  },
)

export interface GlassCardProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof glassCardVariants> {}

const GlassCard = React.forwardRef<HTMLDivElement, GlassCardProps>(
  ({ className, tone, glow, padding, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(glassCardVariants({ tone, glow, padding }), className)}
      {...props}
    />
  ),
)
GlassCard.displayName = "GlassCard"

export { GlassCard, glassCardVariants }
