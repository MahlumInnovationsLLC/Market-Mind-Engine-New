import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  customFetch,
  useGetMyAccess,
  getGetMyAccessQueryKey,
} from "@workspace/api-client-react";

interface DirectiveRow {
  id: number;
  vertical: string | null;
  title: string | null;
  hypothesis: string | null;
  evidence: Record<string, unknown> | null;
  status: string;
  priority: number | null;
  source: string | null;
  created_at: string | null;
  updated_at: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
}

interface DirectivesResponse {
  items: DirectiveRow[];
  count: number;
  error?: string;
}

const STATUSES = ["all", "open", "investigating", "resolved", "dismissed"];

type Decision = "investigating" | "resolved" | "dismissed" | "reopen";

const STATUS_BADGE: Record<string, string> = {
  open: "bg-warning/20 text-warning",
  investigating: "bg-primary/20 text-primary",
  resolved: "bg-success/20 text-success",
  dismissed: "bg-muted/40 text-muted-foreground",
};

export default function ResearchPage() {
  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 60_000 },
  });
  const isAdmin = !!access?.is_admin;
  const [status, setStatus] = useState<string>("open");
  const queryClient = useQueryClient();

  const list = useQuery({
    queryKey: ["research-directives", status],
    queryFn: async () => {
      const qs =
        status && status !== "all"
          ? `?status=${encodeURIComponent(status)}`
          : "";
      return customFetch<DirectivesResponse>(`/api/research/directives${qs}`);
    },
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const decide = useMutation({
    mutationFn: async (vars: {
      id: number;
      decision: Decision;
      note?: string;
    }) => {
      return customFetch(`/api/research/directives/${vars.id}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision: vars.decision, note: vars.note }),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["research-directives"] });
    },
  });

  const rows = list.data?.items ?? [];

  return (
    <div className="space-y-4 font-mono" data-testid="page-research">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold tracking-tight">Auto-research</h1>
        <p className="text-xs uppercase tracking-widest text-muted-foreground">
          Directives surfaced by the auto-research loop · admins triage:
          investigate, resolve, dismiss, or reopen
        </p>
      </header>

      <div className="flex items-center gap-3">
        <label className="text-[10px] uppercase tracking-widest text-muted-foreground">
          Status
        </label>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="bg-input border border-border text-foreground rounded-md px-2 py-1 text-xs"
          data-testid="select-directive-status"
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <div className="ml-auto text-[10px] uppercase tracking-widest text-muted-foreground">
          {rows.length} directive{rows.length === 1 ? "" : "s"}
        </div>
      </div>

      {list.isLoading && (
        <div className="text-xs text-muted-foreground">Loading directives…</div>
      )}
      {list.isError && (
        <div className="text-xs text-destructive">
          Failed to load: {String((list.error as Error)?.message ?? list.error)}
        </div>
      )}
      {!list.isLoading && !list.isError && rows.length === 0 && (
        <div className="text-xs text-muted-foreground border border-dashed border-border rounded-md p-6 text-center">
          No directives in this state — the auto-research loop hasn't surfaced
          any matching hypotheses.
        </div>
      )}

      <ul className="space-y-3">
        {rows.map((d) => {
          const isOpen = d.status === "open" || d.status === "investigating";
          return (
            <li
              key={d.id}
              className="border border-border rounded-md p-4 space-y-2"
              data-testid={`directive-${d.id}`}
            >
              <div className="flex items-center justify-between">
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  #{d.id}
                  {d.vertical ? ` · ${d.vertical}` : ""}
                  {d.source ? ` · ${d.source}` : ""}
                  {d.priority != null ? ` · p${d.priority}` : ""}
                  {d.created_at
                    ? ` · ${new Date(d.created_at).toLocaleString()}`
                    : ""}
                </div>
                <span
                  className={`text-[10px] uppercase tracking-widest px-2 py-0.5 rounded ${
                    STATUS_BADGE[d.status] ?? "bg-muted/30 text-muted-foreground"
                  }`}
                >
                  {d.status}
                </span>
              </div>
              {d.title && (
                <div className="text-sm font-semibold text-foreground">
                  {d.title}
                </div>
              )}
              {d.hypothesis && (
                <div className="text-xs text-foreground/80 whitespace-pre-wrap">
                  {d.hypothesis}
                </div>
              )}
              {d.evidence && Object.keys(d.evidence).length > 0 && (
                <details className="text-xs text-muted-foreground">
                  <summary className="cursor-pointer hover:text-foreground">
                    Evidence
                  </summary>
                  <pre className="mt-2 bg-muted/30 p-2 rounded overflow-x-auto text-[11px]">
                    {JSON.stringify(d.evidence, null, 2)}
                  </pre>
                </details>
              )}
              {d.resolved_at && (
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Decided {new Date(d.resolved_at).toLocaleString()}
                  {d.resolution_note ? ` — ${d.resolution_note}` : ""}
                </div>
              )}
              {isAdmin && (
                <div className="flex flex-wrap gap-2 pt-1">
                  {d.status === "open" && (
                    <button
                      type="button"
                      className="text-[10px] uppercase tracking-widest border border-primary text-primary rounded px-3 py-1 hover:bg-primary/10 disabled:opacity-50"
                      disabled={decide.isPending}
                      onClick={() =>
                        decide.mutate({ id: d.id, decision: "investigating" })
                      }
                      data-testid={`investigate-${d.id}`}
                    >
                      Investigate
                    </button>
                  )}
                  {isOpen && (
                    <>
                      <button
                        type="button"
                        className="text-[10px] uppercase tracking-widest border border-success text-success rounded px-3 py-1 hover:bg-success/10 disabled:opacity-50"
                        disabled={decide.isPending}
                        onClick={() =>
                          decide.mutate({ id: d.id, decision: "resolved" })
                        }
                        data-testid={`resolve-${d.id}`}
                      >
                        Resolve
                      </button>
                      <button
                        type="button"
                        className="text-[10px] uppercase tracking-widest border border-destructive text-destructive rounded px-3 py-1 hover:bg-destructive/10 disabled:opacity-50"
                        disabled={decide.isPending}
                        onClick={() =>
                          decide.mutate({ id: d.id, decision: "dismissed" })
                        }
                        data-testid={`dismiss-${d.id}`}
                      >
                        Dismiss
                      </button>
                    </>
                  )}
                  {(d.status === "resolved" || d.status === "dismissed") && (
                    <button
                      type="button"
                      className="text-[10px] uppercase tracking-widest border border-warning text-warning rounded px-3 py-1 hover:bg-warning/10 disabled:opacity-50"
                      disabled={decide.isPending}
                      onClick={() =>
                        decide.mutate({ id: d.id, decision: "reopen" })
                      }
                      data-testid={`reopen-${d.id}`}
                    >
                      Reopen
                    </button>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
