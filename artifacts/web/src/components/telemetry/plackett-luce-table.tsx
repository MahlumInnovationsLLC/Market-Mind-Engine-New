import { useState } from "react";

export type PlackettLuceSketch = {
  model?: string | null;
  entrant?: string | null;
  entrant_rating?: number | null;
  entrant_probability?: number | null;
  field_size?: number | null;
  ratings?: Record<string, number> | null;
  probabilities?: Record<string, number> | null;
};

type SketchRow = {
  entrant: string;
  rating: number | null;
  probability: number;
  isTraded: boolean;
};

export function extractPlackettLuceSketch(
  gameContext: unknown,
): PlackettLuceSketch | null {
  if (!gameContext || typeof gameContext !== "object") return null;
  const ctx = gameContext as Record<string, unknown>;
  const raw = ctx["plackett_luce_sketch"];
  return coercePlackettLuceSketch(raw);
}

export function coercePlackettLuceSketch(
  raw: unknown,
): PlackettLuceSketch | null {
  if (!raw || typeof raw !== "object") return null;
  const sketch = raw as Record<string, unknown>;
  const probsRaw = sketch["probabilities"];
  if (!probsRaw || typeof probsRaw !== "object") return null;
  const probs: Record<string, number> = {};
  for (const [k, v] of Object.entries(probsRaw as Record<string, unknown>)) {
    if (typeof v === "number" && Number.isFinite(v)) probs[k] = v;
  }
  if (Object.keys(probs).length === 0) return null;
  const ratingsRaw = sketch["ratings"];
  let ratings: Record<string, number> | null = null;
  if (ratingsRaw && typeof ratingsRaw === "object") {
    ratings = {};
    for (const [k, v] of Object.entries(ratingsRaw as Record<string, unknown>)) {
      if (typeof v === "number" && Number.isFinite(v)) ratings[k] = v;
    }
  }
  return {
    model: typeof sketch["model"] === "string" ? (sketch["model"] as string) : null,
    entrant: typeof sketch["entrant"] === "string" ? (sketch["entrant"] as string) : null,
    entrant_rating:
      typeof sketch["entrant_rating"] === "number"
        ? (sketch["entrant_rating"] as number)
        : null,
    entrant_probability:
      typeof sketch["entrant_probability"] === "number"
        ? (sketch["entrant_probability"] as number)
        : null,
    field_size:
      typeof sketch["field_size"] === "number" ? (sketch["field_size"] as number) : null,
    ratings,
    probabilities: probs,
  };
}

type SortKey = "probability" | "rating" | "entrant";
type SortDir = "asc" | "desc";

export function PlackettLuceTable({
  testIdSuffix,
  sketch,
}: {
  testIdSuffix: string | number;
  sketch: PlackettLuceSketch;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("probability");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const probs = sketch.probabilities ?? {};
  const ratings = sketch.ratings ?? {};
  const tradedEntrant = sketch.entrant ?? null;

  const rows: SketchRow[] = Object.entries(probs).map(([entrant, probability]) => ({
    entrant,
    probability,
    rating: typeof ratings[entrant] === "number" ? ratings[entrant] : null,
    isTraded: tradedEntrant !== null && entrant === tradedEntrant,
  }));

  const sorted = [...rows].sort((a, b) => {
    const dir = sortDir === "asc" ? 1 : -1;
    if (sortKey === "entrant") {
      return a.entrant.localeCompare(b.entrant) * dir;
    }
    if (sortKey === "rating") {
      const ar = a.rating ?? Number.NEGATIVE_INFINITY;
      const br = b.rating ?? Number.NEGATIVE_INFINITY;
      if (ar === br) return 0;
      return (ar < br ? -1 : 1) * dir;
    }
    if (a.probability === b.probability) return 0;
    return (a.probability < b.probability ? -1 : 1) * dir;
  });

  const probSum = rows.reduce((acc, r) => acc + r.probability, 0);
  const fieldSize = sketch.field_size ?? rows.length;

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "entrant" ? "asc" : "desc");
    }
  };

  const sortIndicator = (key: SortKey) =>
    sortKey === key ? (sortDir === "desc" ? " ▾" : " ▴") : "";

  const hasRatings = rows.some((r) => r.rating !== null);

  return (
    <div
      className="mt-2 border border-border/60 bg-background/40"
      data-testid={`debate-pl-sketch-${testIdSuffix}`}
    >
      <div className="flex items-center justify-between border-b border-border/60 px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span className="font-bold text-foreground">FIELD_PROBABILITIES</span>
        <span>
          N={fieldSize} · Σ={probSum.toFixed(3)}
        </span>
      </div>
      <table className="w-full text-[10px]">
        <thead>
          <tr className="border-b border-border/40 text-muted-foreground uppercase tracking-wider">
            <th className="px-2 py-1 text-left font-normal">
              <button
                type="button"
                onClick={() => toggleSort("entrant")}
                className="hover:text-primary"
                data-testid={`debate-pl-sort-entrant-${testIdSuffix}`}
              >
                ENTRANT{sortIndicator("entrant")}
              </button>
            </th>
            <th className="px-2 py-1 text-right font-normal">
              <button
                type="button"
                onClick={() => toggleSort("rating")}
                className="hover:text-primary"
                data-testid={`debate-pl-sort-rating-${testIdSuffix}`}
              >
                RATING{sortIndicator("rating")}
              </button>
            </th>
            <th className="px-2 py-1 text-right font-normal">
              <button
                type="button"
                onClick={() => toggleSort("probability")}
                className="hover:text-primary"
                data-testid={`debate-pl-sort-probability-${testIdSuffix}`}
              >
                WIN_PROB{sortIndicator("probability")}
              </button>
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr
              key={r.entrant}
              data-testid={`debate-pl-row-${testIdSuffix}-${r.entrant}`}
              data-traded={r.isTraded ? "true" : "false"}
              className={`border-b border-border/20 last:border-0 ${
                r.isTraded ? "bg-primary/10 text-primary font-bold" : ""
              }`}
            >
              <td className="px-2 py-1 text-left">
                <div className="flex items-center gap-1">
                  <span className="truncate" title={r.entrant}>
                    {r.entrant}
                  </span>
                  {r.isTraded ? (
                    <span
                      className="border border-primary/60 px-1 py-px text-[9px] uppercase tracking-widest text-primary"
                      data-testid={`debate-pl-traded-badge-${testIdSuffix}`}
                    >
                      TRADED
                    </span>
                  ) : null}
                </div>
              </td>
              <td className="px-2 py-1 text-right tabular-nums text-muted-foreground">
                {r.rating !== null ? r.rating.toFixed(0) : "—"}
              </td>
              <td className="px-2 py-1 text-right tabular-nums">
                {(r.probability * 100).toFixed(1)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {hasRatings ? null : (
        <div className="px-2 py-1 text-[10px] text-muted-foreground border-t border-border/40">
          Ratings unavailable for this field — probabilities only.
        </div>
      )}
    </div>
  );
}
