import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  getGetEdgeTerminalStatusQueryKey,
  useGetEdgeTerminalStatus,
} from "@workspace/api-client-react";
import { useInputMode, type InputMode } from "./input-mode-toggle";
import { usePlacedBy, type PlacedByFilter } from "./placed-by-context";

/**
 * Source-filter context (task #846).
 *
 * Layers Edge-Terminal-vs-Pipeline source filtering on top of the
 * existing Manual / System / Both `<PlacedByProvider>`.  Every
 * dashboard tile reads :func:`useEffectivePlacedBy` (instead of the
 * raw `usePlacedBy`) so the same `placed_by` query parameter naturally
 * narrows trade-derived metrics to the currently-active source:
 *
 *   - Input mode = ``edge_terminal`` and ``showPipelineData=false``
 *     → effective `placed_by` is forced to ``"edge_terminal"`` and
 *     the Manual / System / Both selection is ignored for that tick.
 *   - Input mode = ``pipeline`` (and ``showPipelineData=false``)
 *     → effective `placed_by` is forced to ``"pipeline"`` (= every
 *     non-Edge-Terminal trade).
 *   - Otherwise the operator's PlacedByToggle wins (unfiltered, or
 *     manual/system within the mixed view).
 *
 * The provider also owns the `showPipelineData` toggle that previously
 * lived in `dashboard.tsx` local state so any tile (or the input-mode
 * toggle in the header) can read or flip it without prop drilling.
 *
 * Stale-poller status is forwarded via
 * :func:`useEdgeTerminalSourceStale` so every tile can render the
 * shared :component:`TileStaleBadge` and grey itself when the
 * Edge Terminal poller has gone silent.
 */

export type EffectivePlacedBy =
  | PlacedByFilter
  | "edge_terminal"
  | "pipeline";

type Ctx = {
  inputMode: InputMode | undefined;
  isEdgeTerminal: boolean;
  showPipelineData: boolean;
  setShowPipelineData: (v: boolean) => void;
  /** True when at least one tile should render pipeline-grammar data. */
  pipelineVisible: boolean;
};

const SourceFilterContext = createContext<Ctx | null>(null);

export function SourceFilterProvider({ children }: { children: ReactNode }) {
  const { mode, isEdgeTerminal } = useInputMode();
  const [showPipelineData, setShowPipelineData] = useState(false);
  const pipelineVisible = !isEdgeTerminal || showPipelineData;

  const value = useMemo<Ctx>(
    () => ({
      inputMode: mode,
      isEdgeTerminal,
      showPipelineData,
      setShowPipelineData,
      pipelineVisible,
    }),
    [mode, isEdgeTerminal, showPipelineData, pipelineVisible],
  );

  return (
    <SourceFilterContext.Provider value={value}>
      {children}
    </SourceFilterContext.Provider>
  );
}

export function useSourceFilter(): Ctx {
  const ctx = useContext(SourceFilterContext);
  if (!ctx) {
    // Legacy fallback for components rendered outside the provider
    // (e.g. the /trading page's recent-trades card) — behave as if the
    // dashboard were unfiltered so we never silently change behaviour.
    return {
      inputMode: undefined,
      isEdgeTerminal: false,
      showPipelineData: true,
      setShowPipelineData: () => undefined,
      pipelineVisible: true,
    };
  }
  return ctx;
}

/**
 * Return the `placed_by` value the API actually expects, after
 * folding in the source-mode override.  Tiles pass this straight to
 * the OpenAPI client params; the server-side ``_placed_by_predicate``
 * helper knows about the ``edge_terminal`` / ``pipeline`` values.
 */
export function useEffectivePlacedBy(): EffectivePlacedBy {
  const { placedBy } = usePlacedBy();
  const { isEdgeTerminal, showPipelineData } = useSourceFilter();
  return useMemo<EffectivePlacedBy>(() => {
    // When both sources are visible (mixed view), defer to the
    // operator's Manual / System / Both selection.
    if (showPipelineData) return placedBy;
    // Otherwise force the single active source, regardless of the
    // operator's PlacedByToggle — Edge Terminal mode shows only
    // edge_terminal-placed trades; Pipeline mode shows only
    // non-edge-terminal (pipeline) trades.
    return isEdgeTerminal ? "edge_terminal" : "pipeline";
  }, [placedBy, isEdgeTerminal, showPipelineData]);
}

/**
 * Live status of the Edge Terminal poller.  Returns a stable object
 * even when the request is in-flight so callers can deref freely.
 */
export function useEdgeTerminalSourceStale(): {
  stale: boolean;
  ageSeconds: number | null;
  /** True when the active source actually depends on the poller. */
  appliesToActiveSource: boolean;
} {
  const { isEdgeTerminal, showPipelineData } = useSourceFilter();
  const appliesToActiveSource = isEdgeTerminal && !showPipelineData;
  const statusKey = getGetEdgeTerminalStatusQueryKey();
  const { data } = useGetEdgeTerminalStatus({
    query: {
      queryKey: statusKey,
      refetchInterval: 30_000,
      staleTime: 25_000,
      // Skip the poll entirely when no tile would react to it.
      enabled: appliesToActiveSource,
    },
  });
  return {
    stale: appliesToActiveSource && !!data?.stale,
    ageSeconds:
      typeof data?.age_seconds === "number" ? data.age_seconds : null,
    appliesToActiveSource,
  };
}
