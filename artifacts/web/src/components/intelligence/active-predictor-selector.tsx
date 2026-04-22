import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";

interface PredictorRow {
  name: string;
  version: string;
  sha256: string;
  size: number;
  loaded: boolean;
  is_active: boolean;
  is_override: boolean;
}

interface AdminModelsResponse {
  items: PredictorRow[];
  ready: boolean;
  in_progress: boolean;
  active_predictor: { name?: string | null; bound?: boolean } | null;
  active_override: string | null;
}

const QUERY_KEY = ["/api/admin/models"] as const;

export default function ActivePredictorSelector({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery<AdminModelsResponse>({
    queryKey: QUERY_KEY,
    queryFn: () => customFetch<AdminModelsResponse>("/api/admin/models"),
    enabled: isAdmin,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const setActive = useMutation({
    mutationFn: (name: string | null) =>
      customFetch<unknown>("/api/admin/models/active", {
        method: "POST",
        body: JSON.stringify({ name }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QUERY_KEY });
      qc.invalidateQueries({ queryKey: ["/api/admin/models/active"] });
      qc.invalidateQueries({ queryKey: ["/api/dashboard/active-model"] });
      qc.invalidateQueries({ queryKey: ["/api/models/active"] });
      qc.invalidateQueries({ queryKey: ["/api/models/inventory"] });
    },
  });

  const [pending, setPending] = useState<string | null>(null);
  useEffect(() => {
    if (!setActive.isPending) setPending(null);
  }, [setActive.isPending]);

  if (!isAdmin) {
    return (
      <div
        className="border border-dashed border-border bg-card/40 p-3 text-center text-[10px] uppercase tracking-widest text-muted-foreground"
        data-testid="text-active-predictor-admin-only"
      >
        ADMIN_ONLY
      </div>
    );
  }
  if (isLoading) {
    return <div className="h-32 animate-pulse bg-muted/10" />;
  }
  if (isError || !data) {
    return (
      <div className="border border-destructive/40 p-3 text-center text-[10px] uppercase tracking-widest text-destructive">
        ERR_PREDICTOR_LOAD
      </div>
    );
  }

  const onPick = (name: string) => {
    setPending(name);
    setActive.mutate(name);
  };
  const onClear = () => {
    setPending("__clear__");
    setActive.mutate(null);
  };

  return (
    <div className="space-y-2" data-testid="card-active-predictor-selector">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground">
        <span>
          OVERRIDE:{" "}
          <span className="text-foreground" data-testid="text-active-override">
            {data.active_override ?? "—"}
          </span>
        </span>
        <button
          type="button"
          onClick={onClear}
          disabled={setActive.isPending || data.active_override === null}
          className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-foreground hover:bg-muted/20 disabled:opacity-40"
          data-testid="button-clear-active-override"
        >
          {pending === "__clear__" && setActive.isPending ? "…" : "Clear"}
        </button>
      </div>
      <div className="overflow-hidden border border-border">
        <table className="w-full text-xs">
          <thead className="bg-muted/20 text-[10px] uppercase tracking-widest text-muted-foreground">
            <tr>
              <th className="p-2 text-left">Name</th>
              <th className="p-2 text-left">Version</th>
              <th className="p-2 text-right">Loaded</th>
              <th className="p-2 text-right">Action</th>
            </tr>
          </thead>
          <tbody>
            {data.items.length === 0 ? (
              <tr>
                <td
                  colSpan={4}
                  className="p-3 text-center text-[10px] uppercase tracking-widest text-muted-foreground"
                >
                  NO_PREDICTORS
                </td>
              </tr>
            ) : (
              data.items.map((row) => {
                const rowPending = pending === row.name && setActive.isPending;
                return (
                  <tr
                    key={row.name}
                    className="border-t border-border/60"
                    data-testid={`row-predictor-${row.name}`}
                  >
                    <td className="p-2 font-mono text-foreground">
                      {row.name}
                      {row.is_active && (
                        <span
                          className="ml-2 text-[10px] uppercase tracking-widest text-success"
                          data-testid={`tag-active-${row.name}`}
                        >
                          ACTIVE
                        </span>
                      )}
                    </td>
                    <td className="p-2 font-mono text-muted-foreground">
                      {row.version || "—"}
                    </td>
                    <td className="p-2 text-right">
                      {row.loaded ? (
                        <span className="text-success">●</span>
                      ) : (
                        <span className="text-muted-foreground">○</span>
                      )}
                    </td>
                    <td className="p-2 text-right">
                      <button
                        type="button"
                        disabled={rowPending || row.is_active}
                        onClick={() => onPick(row.name)}
                        className="border border-primary/60 px-2 py-0.5 text-[10px] uppercase tracking-widest text-primary hover:bg-primary/10 disabled:opacity-40"
                        data-testid={`button-set-active-${row.name}`}
                      >
                        {rowPending ? "…" : "Set active"}
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      {setActive.isError && (
        <div
          className="border border-destructive/40 p-2 text-center text-[10px] uppercase tracking-widest text-destructive"
          data-testid="text-set-active-error"
        >
          ERR_SET_ACTIVE · {String((setActive.error as Error)?.message ?? "")}
        </div>
      )}
      <button
        type="button"
        onClick={() => refetch()}
        className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
        data-testid="button-refresh-predictors"
      >
        ↻ Refresh
      </button>
    </div>
  );
}
