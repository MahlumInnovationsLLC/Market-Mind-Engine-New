import { useQueryClient } from "@tanstack/react-query";
import {
  getGetEdgeTerminalInputModeQueryKey,
  useGetEdgeTerminalInputMode,
  useSetEdgeTerminalInputMode,
} from "@workspace/api-client-react";

export type InputMode = "pipeline" | "edge_terminal";

export function useInputMode(): {
  mode: InputMode | undefined;
  isEdgeTerminal: boolean;
  isLoading: boolean;
} {
  const key = getGetEdgeTerminalInputModeQueryKey();
  const { data, isLoading } = useGetEdgeTerminalInputMode({
    query: { queryKey: key, staleTime: 15_000, refetchInterval: 60_000 },
  });
  const mode = (data?.mode as InputMode | undefined) ?? undefined;
  return {
    mode,
    isEdgeTerminal: mode === "edge_terminal",
    isLoading,
  };
}

export default function InputModeToggle({
  showPipelineData,
  onTogglePipelineData,
}: {
  showPipelineData: boolean;
  onTogglePipelineData: (next: boolean) => void;
}) {
  const qc = useQueryClient();
  const { mode } = useInputMode();
  const { mutateAsync, isPending } = useSetEdgeTerminalInputMode();

  const flip = async (next: InputMode) => {
    if (next === mode) return;
    await mutateAsync({ data: { mode: next } });
    await qc.invalidateQueries({
      queryKey: getGetEdgeTerminalInputModeQueryKey(),
    });
  };

  return (
    <div
      data-testid="input-mode-toggle"
      className="flex flex-wrap items-center gap-2"
    >
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
        Input
      </span>
      <div className="inline-flex overflow-hidden rounded border border-border">
        <button
          type="button"
          data-testid="input-mode-edge-terminal"
          disabled={isPending}
          onClick={() => flip("edge_terminal")}
          className={
            "px-2 py-1 text-[11px] uppercase tracking-wider " +
            (mode === "edge_terminal"
              ? "bg-accent-cyan/20 text-accent-cyan"
              : "text-muted-foreground hover:bg-muted")
          }
        >
          Edge Terminal
        </button>
        <button
          type="button"
          data-testid="input-mode-pipeline"
          disabled={isPending}
          onClick={() => flip("pipeline")}
          className={
            "px-2 py-1 text-[11px] uppercase tracking-wider " +
            (mode === "pipeline"
              ? "bg-accent-cyan/20 text-accent-cyan"
              : "text-muted-foreground hover:bg-muted")
          }
        >
          Pipeline
        </button>
      </div>
      {mode === "edge_terminal" ? (
        <label className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
          <input
            type="checkbox"
            data-testid="show-pipeline-data-toggle"
            checked={showPipelineData}
            onChange={(e) => onTogglePipelineData(e.target.checked)}
          />
          Show pipeline data
        </label>
      ) : null}
    </div>
  );
}
