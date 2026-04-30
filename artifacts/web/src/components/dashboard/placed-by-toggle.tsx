import { usePlacedBy, type PlacedByFilter } from "./placed-by-context";

/**
 * 3-segment Manual / System / Both toggle (T004).
 *
 * Visually matches `PeriodStrip` so the dashboard header reads as a
 * single horizontal control bar.  Backed by `<PlacedByProvider>` —
 * see :file:`placed-by-context.tsx` for URL plumbing.
 *
 * `Both` is the documented default and maps to ``placed_by=all`` on
 * the wire (the API treats `all` as no-filter, surfacing every row
 * including ``rh_import`` and ``unknown`` provenance buckets).
 */
const SEGMENTS: { key: PlacedByFilter; label: string; testId: string }[] = [
  { key: "manual", label: "MANUAL", testId: "placed-by-manual" },
  { key: "system", label: "SYSTEM", testId: "placed-by-system" },
  { key: "all", label: "BOTH", testId: "placed-by-all" },
];

export function PlacedByToggle() {
  const { placedBy, setPlacedBy } = usePlacedBy();
  return (
    <div
      className="inline-flex items-center gap-1 border border-border bg-muted/20 p-0.5"
      role="group"
      aria-label="Trade provenance filter"
      data-testid="placed-by-toggle"
    >
      {SEGMENTS.map((seg) => {
        const active = placedBy === seg.key;
        return (
          <button
            key={seg.key}
            type="button"
            onClick={() => setPlacedBy(seg.key)}
            data-testid={`button-${seg.testId}`}
            aria-pressed={active}
            className={`px-3 py-1 text-[10px] uppercase tracking-widest font-mono transition-colors ${
              active
                ? "bg-primary/15 text-primary"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {seg.label}
          </button>
        );
      })}
    </div>
  );
}
