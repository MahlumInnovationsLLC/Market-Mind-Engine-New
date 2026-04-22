import { useQueryClient } from "@tanstack/react-query";
import {
  getAdminGetVerticalVisibilityQueryKey,
  getGetVerticalVisibilityQueryKey,
  getGetVerticalsOverviewQueryKey,
  useAdminGetVerticalVisibility,
  useAdminSetVerticalVisibility,
} from "@workspace/api-client-react";

interface Props {
  vertical: string;
  isAdmin: boolean;
}

/**
 * Inline pin/hide toggles for a vertical card. Admin-only — renders
 * nothing for non-admins. Reads the canonical hidden+pinned lists from
 * /api/admin/vertical-visibility, computes the next list locally, and
 * POSTs the replacement set.
 */
export default function VerticalAdminControls({ vertical, isAdmin }: Props) {
  const qc = useQueryClient();
  const visKey = getAdminGetVerticalVisibilityQueryKey();
  const { data, isLoading } = useAdminGetVerticalVisibility({
    query: { queryKey: visKey, enabled: isAdmin, staleTime: 30_000 },
  });

  const setVis = useAdminSetVerticalVisibility({
    mutation: {
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: visKey });
        qc.invalidateQueries({ queryKey: getGetVerticalVisibilityQueryKey() });
        qc.invalidateQueries({
          queryKey: getGetVerticalsOverviewQueryKey({ days: 7 }),
        });
      },
    },
  });

  if (!isAdmin) return null;
  if (isLoading || !data) {
    return (
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        …
      </div>
    );
  }

  const hidden = data.hidden ?? [];
  const pinned = data.pinned ?? [];
  const isHidden = hidden.includes(vertical);
  const isPinned = pinned.includes(vertical);

  const togglePin = (e: React.MouseEvent | React.KeyboardEvent) => {
    e.stopPropagation();
    e.preventDefault();
    const next = isPinned
      ? pinned.filter((p) => p !== vertical)
      : [...pinned, vertical];
    setVis.mutate({ data: { pinned: next } });
  };
  const toggleHide = (e: React.MouseEvent | React.KeyboardEvent) => {
    e.stopPropagation();
    e.preventDefault();
    const next = isHidden
      ? hidden.filter((h) => h !== vertical)
      : [...hidden, vertical];
    setVis.mutate({ data: { hidden: next } });
  };

  const busy = setVis.isPending;

  return (
    <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        disabled={busy}
        onClick={togglePin}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") togglePin(e);
        }}
        className={`border px-2 py-0.5 text-[10px] uppercase tracking-widest transition-colors disabled:opacity-40 ${
          isPinned
            ? "border-primary/60 bg-primary/10 text-primary"
            : "border-border text-muted-foreground hover:bg-muted/20"
        }`}
        data-testid={`button-toggle-pin-${vertical}`}
        title={isPinned ? "Unpin" : "Pin"}
      >
        {isPinned ? "★ Pinned" : "☆ Pin"}
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={toggleHide}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") toggleHide(e);
        }}
        className={`border px-2 py-0.5 text-[10px] uppercase tracking-widest transition-colors disabled:opacity-40 ${
          isHidden
            ? "border-destructive/60 bg-destructive/10 text-destructive"
            : "border-border text-muted-foreground hover:bg-muted/20"
        }`}
        data-testid={`button-toggle-hide-${vertical}`}
        title={isHidden ? "Show" : "Hide"}
      >
        {isHidden ? "Hidden" : "Hide"}
      </button>
    </div>
  );
}
