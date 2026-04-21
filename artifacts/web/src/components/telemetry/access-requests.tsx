import { useQueryClient } from "@tanstack/react-query";
import {
  AdminAccessRequest,
  getAdminGetAuditLogQueryKey,
  getAdminListAccessRequestsQueryKey,
  getAdminListUsersQueryKey,
  useAdminDecideAccessRequest,
  useAdminListAccessRequests,
} from "@workspace/api-client-react";
import { UserCheck } from "lucide-react";

export default function AccessRequests() {
  const qc = useQueryClient();
  const { data, isLoading, isError } = useAdminListAccessRequests({
    query: {
      queryKey: getAdminListAccessRequestsQueryKey(),
      refetchInterval: 30_000,
    },
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: getAdminListAccessRequestsQueryKey() });
    qc.invalidateQueries({ queryKey: getAdminListUsersQueryKey() });
    qc.invalidateQueries({ queryKey: getAdminGetAuditLogQueryKey() });
  };
  const decide = useAdminDecideAccessRequest({
    mutation: { onSuccess: refresh },
  });

  const items: AdminAccessRequest[] = data?.items ?? [];

  return (
    <div className="border border-border bg-card" data-testid="panel-access-requests">
      <div className="border-b border-border p-2 px-3 flex justify-between items-center bg-muted/20">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground flex items-center gap-2">
          <UserCheck className="w-3 h-3" /> ACCESS_REQUESTS
        </h2>
        <span
          className={items.length > 0 ? "text-primary" : "text-muted-foreground"}
          data-testid="text-access-requests-count"
        >
          {items.length}
        </span>
      </div>
      <div className="p-3 text-xs">
        {isLoading ? (
          <div className="text-muted-foreground">Loading…</div>
        ) : isError ? (
          <div className="text-destructive">ERR_REQ_LOAD</div>
        ) : items.length === 0 ? (
          <div className="text-muted-foreground" data-testid="text-no-access-requests">
            No pending requests.
          </div>
        ) : (
          <ul className="space-y-2">
            {items.map((u) => {
              const id = u.clerk_user_id;
              const onApprove = () => {
                if (
                  !window.confirm(
                    `Approve ${u.email ?? id} for operator access?`,
                  )
                )
                  return;
                decide.mutate({
                  clerkUserId: id,
                  data: { decision: "approve" },
                });
              };
              const onDeny = () => {
                if (
                  !window.confirm(
                    `Deny access for ${u.email ?? id}? They can request again on next sign-in.`,
                  )
                )
                  return;
                decide.mutate({
                  clerkUserId: id,
                  data: { decision: "deny" },
                });
              };
              return (
                <li
                  key={id}
                  className="border border-border p-2 flex flex-col gap-1"
                  data-testid={`row-access-request-${id}`}
                >
                  <div className="text-foreground truncate" title={u.email ?? id}>
                    {u.email ?? "(no email)"}
                  </div>
                  <div className="text-[10px] text-muted-foreground truncate">
                    {u.display_name ?? id}
                  </div>
                  <div className="text-[10px] text-muted-foreground">
                    seen {u.activity_count}x
                    {u.last_seen_at ? ` · ${u.last_seen_at.slice(0, 19).replace("T", " ")}` : ""}
                  </div>
                  <div className="flex gap-1 mt-1">
                    <button
                      type="button"
                      onClick={onApprove}
                      disabled={decide.isPending}
                      data-testid={`button-access-approve-${id}`}
                      className="flex-1 border border-success text-success hover:bg-success/10 px-2 py-1 text-[10px] uppercase tracking-widest disabled:opacity-50"
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      onClick={onDeny}
                      disabled={decide.isPending}
                      data-testid={`button-access-deny-${id}`}
                      className="flex-1 border border-destructive text-destructive hover:bg-destructive/10 px-2 py-1 text-[10px] uppercase tracking-widest disabled:opacity-50"
                    >
                      Deny
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
