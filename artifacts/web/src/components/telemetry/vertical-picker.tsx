import { Link } from "wouter";
import {
  getGetVerticalsOverviewQueryKey,
  getGetVerticalVisibilityQueryKey,
  useGetVerticalsOverview,
  useGetVerticalVisibility,
} from "@workspace/api-client-react";
import { useSelectedVerticals } from "@/hooks/use-selected-verticals";

/**
 * Chip-bar picker that scopes every dashboard panel to a subset of
 * verticals.  Hidden verticals (admin-set) are dimmed for everyone except
 * admins, who get a "show hidden" affordance via the `verticals` page.
 *
 * Empty selection = show all (a no-op filter).
 */
export default function VerticalPicker({ isAdmin = false }: { isAdmin?: boolean }) {
  const { data: overview, isLoading } = useGetVerticalsOverview(
    { days: 7 },
    {
      query: {
        queryKey: getGetVerticalsOverviewQueryKey({ days: 7 }),
        staleTime: 60_000,
      },
    },
  );
  const { data: visibility } = useGetVerticalVisibility({
    query: {
      queryKey: getGetVerticalVisibilityQueryKey(),
      staleTime: 60_000,
    },
  });
  const { selected, isSelected, toggle, clear } = useSelectedVerticals();

  const hidden = new Set(visibility?.hidden ?? []);
  const all = (overview?.verticals ?? []).filter((e) =>
    isAdmin ? true : !hidden.has(e.vertical),
  );

  return (
    <div
      className="border border-border bg-card p-3 mb-4 flex flex-wrap items-center gap-2"
      data-testid="vertical-picker"
    >
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground mr-1">
        Verticals
      </span>
      <button
        type="button"
        onClick={clear}
        className={`px-2 py-1 border text-[10px] uppercase tracking-widest ${
          selected.length === 0
            ? "border-primary text-primary"
            : "border-border text-muted-foreground hover:text-foreground"
        }`}
        data-testid="vertical-chip-all"
      >
        All
      </button>
      {isLoading && (
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          Loading…
        </span>
      )}
      {all.map((entry) => {
        const active = isSelected(entry.vertical);
        const isHidden = hidden.has(entry.vertical);
        return (
          <button
            key={entry.vertical}
            type="button"
            onClick={() => toggle(entry.vertical)}
            className={`px-2 py-1 border text-[10px] uppercase tracking-widest ${
              active
                ? "border-primary text-primary bg-primary/10"
                : "border-border text-muted-foreground hover:text-foreground"
            } ${isHidden ? "opacity-50" : ""}`}
            data-testid={`vertical-chip-${entry.vertical}`}
            title={isHidden ? "Hidden by operator" : entry.description ?? entry.vertical}
          >
            {entry.vertical.toUpperCase()}
            {entry.pinned && <span className="ml-1 text-primary">★</span>}
          </button>
        );
      })}
      <Link
        href="/verticals"
        className="ml-auto px-2 py-1 border border-border text-[10px] uppercase tracking-widest text-muted-foreground hover:text-primary hover:border-primary"
        data-testid="link-verticals-overview"
      >
        Overview →
      </Link>
    </div>
  );
}
