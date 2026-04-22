import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const badgeVariants = cva(
  // @replit
  // Whitespace-nowrap: Badges should never wrap.
  "whitespace-nowrap inline-flex items-center rounded-md border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2" +
  " hover-elevate ",
  {
    variants: {
      variant: {
        default:
          // @replit shadow-xs instead of shadow, no hover because we use hover-elevate
          "border-transparent bg-primary text-primary-foreground shadow-xs",
        secondary:
          // @replit no hover because we use hover-elevate
          "border-transparent bg-secondary text-secondary-foreground",
        destructive:
          // @replit shadow-xs instead of shadow, no hover because we use hover-elevate
          "border-transparent bg-destructive text-destructive-foreground shadow-xs",
          // @replit shadow-xs" - use badge outline variable
        outline: "text-foreground border [border-color:var(--badge-outline)]",
        success:
          "border-transparent bg-accent-green/15 text-accent-green shadow-xs",
        profit:
          "border-transparent bg-accent-green/15 text-accent-green shadow-xs",
        loss:
          "border-transparent bg-accent-red/15 text-accent-red shadow-xs",
        warning:
          "border-transparent bg-accent-amber/15 text-accent-amber shadow-xs",
        warn:
          "border-transparent bg-accent-amber/15 text-accent-amber shadow-xs",
        info:
          "border-transparent bg-accent-cyan/15 text-accent-cyan shadow-xs",
        ai:
          "border-transparent bg-accent-purple/15 text-accent-purple shadow-xs",
        purple:
          "border-transparent bg-accent-purple/15 text-accent-purple shadow-xs",
        live:
          "border-transparent bg-accent-green/15 text-accent-green shadow-xs animate-pulse-slow",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  )
}

export { Badge, badgeVariants }
