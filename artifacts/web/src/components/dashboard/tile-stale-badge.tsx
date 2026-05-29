import {
  edgeTerminalReason,
  useEdgeTerminalSourceStale,
} from "./source-filter-context";

/**
 * Shared stale-source indicator used by every trade-derived dashboard
 * tile (task #846).  Renders the same amber badge the Edge Terminal
 * P&L card was already using, and exposes a helper for the tile's
 * outer wrapper to grey itself out in lock-step.
 *
 * When the active source is *not* Edge Terminal (operator is in
 * Pipeline mode or has clicked "Show pipeline data"), the hook
 * silently returns nothing — pipeline trades have no equivalent
 * staleness signal so the badge stays out of the way.
 */
export function useTileStaleClass(): string | undefined {
  const { stale } = useEdgeTerminalSourceStale();
  return stale ? "opacity-60" : undefined;
}

export function TileStaleBadge({
  className,
  testId = "tile-stale-badge",
}: {
  className?: string;
  testId?: string;
}) {
  const { stale, ageSeconds, category, lastError } =
    useEdgeTerminalSourceStale();
  if (!stale) return null;
  const reason = edgeTerminalReason(category, lastError);
  return (
    <div
      data-testid={testId}
      title={reason}
      className={
        "mb-2 inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-300" +
        (className ? ` ${className}` : "")
      }
    >
      Stale poller
      {typeof ageSeconds === "number" ? ` · ${Math.round(ageSeconds)}s` : ""}
    </div>
  );
}
