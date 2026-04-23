import { Link } from "wouter";

/**
 * Inline upsell banner shown above any list whose backend response
 * carries `delayed: true` — i.e. the caller is on the free tier and
 * the 15-minute signal delay has been applied.
 *
 * Kept deliberately compact (single row, no dismiss) so it doesn't
 * crowd the underlying list, and so it stays visible until the user
 * upgrades or signs in as a Pro+ account.
 */
interface DelayedSignalsBannerProps {
  delayMinutes?: number | null;
  testId?: string;
}

export default function DelayedSignalsBanner({
  delayMinutes,
  testId = "banner-delayed-signals",
}: DelayedSignalsBannerProps) {
  const minutes = delayMinutes && delayMinutes > 0 ? delayMinutes : 15;
  return (
    <div
      className="border border-primary/40 bg-primary/5 px-3 py-2 flex items-center justify-between gap-3"
      data-testid={testId}
    >
      <div className="min-w-0">
        <div className="text-[10px] uppercase tracking-widest text-primary font-bold">
          FREE TIER · {minutes}-MIN DELAY
        </div>
        <div className="text-[11px] text-foreground/80 truncate">
          You're seeing signals delayed by {minutes} minutes. Upgrade to Pro
          for live data.
        </div>
      </div>
      <Link
        href="/pricing"
        className="shrink-0 border border-primary/60 px-2 py-1 text-[10px] font-semibold uppercase tracking-widest text-primary hover:bg-primary hover:text-primary-foreground transition-colors"
        data-testid="link-delayed-upgrade"
      >
        UPGRADE →
      </Link>
    </div>
  );
}
