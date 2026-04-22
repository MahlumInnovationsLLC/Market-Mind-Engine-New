import { cn } from "@/lib/utils"

type SkeletonProps = React.HTMLAttributes<HTMLDivElement> & {
  variant?: "default" | "shimmer" | "stat-row" | "chart" | "table" | "text"
}

function Skeleton({ className, variant = "default", ...props }: SkeletonProps) {
  if (variant === "stat-row") {
    return (
      <div className={cn("flex items-center gap-3", className)} {...props}>
        <div className="shimmer h-3 w-20 rounded-md" />
        <div className="shimmer h-3 w-16 rounded-md" />
        <div className="shimmer h-3 w-24 rounded-md" />
      </div>
    )
  }
  if (variant === "chart") {
    return (
      <div className={cn("shimmer h-48 w-full rounded-md", className)} {...props} />
    )
  }
  if (variant === "table") {
    return (
      <div className={cn("space-y-2", className)} {...props}>
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="shimmer h-8 w-full rounded-md" />
        ))}
      </div>
    )
  }
  if (variant === "text") {
    return (
      <div className={cn("shimmer h-3 w-full rounded-md", className)} {...props} />
    )
  }
  if (variant === "shimmer") {
    return (
      <div className={cn("shimmer rounded-md", className)} {...props} />
    )
  }
  return (
    <div
      className={cn("animate-pulse rounded-md bg-primary/10", className)}
      {...props}
    />
  )
}

export { Skeleton }
