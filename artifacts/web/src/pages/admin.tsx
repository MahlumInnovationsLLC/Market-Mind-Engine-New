import { useMemo, useState } from "react";
import { Link } from "wouter";
import { useAuth, useClerk } from "@clerk/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import {
  AdminAuditLogEntry,
  AdminUser,
  getAdminGetAuditLogQueryKey,
  getAdminGetUserQueryKey,
  getAdminListUsersQueryKey,
  getGetMyAccessQueryKey,
  useAdminGetAuditLog,
  useAdminGetUser,
  useAdminListUsers,
  useAdminSetUserAdmin,
  useAdminSetUserApproved,
} from "@workspace/api-client-react";

type Filter = "all" | "pending" | "approved" | "admin";

export default function AdminPage() {
  const { signOut } = useClerk();
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [activeUserId, setActiveUserId] = useState<string | null>(null);
  const [tab, setTab] = useState<"users" | "audit" | "runtime">("users");

  const usersQuery = useAdminListUsers({
    query: {
      queryKey: getAdminListUsersQueryKey(),
      refetchInterval: 30_000,
    },
  });

  const items: AdminUser[] = usersQuery.data?.items ?? [];

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return items.filter((u) => {
      if (filter === "pending" && u.approved) return false;
      if (filter === "approved" && !u.approved) return false;
      if (filter === "admin" && !u.is_admin) return false;
      if (q) {
        const hay = `${u.email ?? ""} ${u.display_name ?? ""} ${
          u.clerk_user_id
        }`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [items, filter, search]);

  return (
    <div
      className="min-h-[100dvh] bg-background text-foreground p-4 md:p-6 font-mono selection:bg-primary/30"
      data-testid="page-admin"
    >
      <header className="mb-6 flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase">
            AXIOM_PREDICT :: ADMIN_CONSOLE
          </h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">
            User access &amp; role management
          </p>
        </div>
        <div className="text-xs text-muted-foreground flex flex-wrap items-center gap-4">
          <Link
            href="/"
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="link-dashboard"
          >
            ← Dashboard
          </Link>
          <button
            type="button"
            onClick={() => signOut({ redirectUrl: import.meta.env.BASE_URL })}
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-destructive hover:border-destructive transition-colors"
            data-testid="button-admin-sign-out"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="mb-4 flex gap-2 text-xs uppercase tracking-widest">
        {(["users", "audit", "runtime"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            data-testid={`tab-${t}`}
            className={`border px-3 py-1 ${
              tab === t
                ? "border-primary text-primary"
                : "border-border text-muted-foreground hover:text-foreground"
            }`}
          >
            {t === "users"
              ? "Users"
              : t === "audit"
                ? "Audit log"
                : "Runtime"}
          </button>
        ))}
      </div>

      {tab === "users" ? (
        <main className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          <section className="lg:col-span-8 space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-widest">
              {(["all", "pending", "approved", "admin"] as const).map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => setFilter(f)}
                  data-testid={`filter-${f}`}
                  className={`border px-2 py-1 ${
                    filter === f
                      ? "border-primary text-primary"
                      : "border-border text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {f}
                </button>
              ))}
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="search email / name / id"
                data-testid="input-admin-search"
                className="ml-auto bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal w-72 max-w-full"
              />
            </div>

            <div className="border border-border bg-card overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest">
                  <tr>
                    <th className="text-left p-2">User</th>
                    <th className="text-left p-2">Status</th>
                    <th className="text-left p-2">Signed up</th>
                    <th className="text-left p-2">Last sign-in</th>
                    <th className="text-left p-2">Last seen</th>
                    <th className="text-right p-2">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {usersQuery.isLoading ? (
                    <tr>
                      <td colSpan={6} className="p-6 text-center text-muted-foreground">
                        Loading…
                      </td>
                    </tr>
                  ) : filtered.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="p-6 text-center text-muted-foreground">
                        No users match the current filter.
                      </td>
                    </tr>
                  ) : (
                    filtered.map((u) => (
                      <UserRow
                        key={u.clerk_user_id}
                        user={u}
                        active={u.clerk_user_id === activeUserId}
                        onSelect={() => setActiveUserId(u.clerk_user_id)}
                      />
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <aside className="lg:col-span-4">
            {activeUserId ? (
              <UserDetail
                clerkUserId={activeUserId}
                onClose={() => setActiveUserId(null)}
              />
            ) : (
              <div className="border border-border bg-card p-4 text-xs text-muted-foreground uppercase tracking-widest">
                Select a user to inspect their account.
              </div>
            )}
          </aside>
        </main>
      ) : tab === "audit" ? (
        <AuditLogPanel />
      ) : (
        <RuntimePanel />
      )}
    </div>
  );
}

type RuntimeSettings = {
  aggression_mode: string;
  thresholds: {
    min_edge_pp: number;
    kelly_fraction_cap: number;
    min_agreement_rate: number;
    max_confidence_band: number;
  };
  available_modes: string[];
  updated_at: string | null;
  updated_by: string | null;
};

function RuntimePanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "runtime-settings"] as const;

  const { data, isLoading, error } = useQuery<RuntimeSettings>({
    queryKey,
    queryFn: () =>
      apiFetch<RuntimeSettings>("/api/admin/runtime-settings", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
  });

  const mutation = useMutation({
    mutationFn: (mode: string) =>
      apiFetch<RuntimeSettings>("/api/admin/runtime-settings", {
        method: "POST",
        body: { aggression_mode: mode },
        getToken: tokenFn,
      }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
      queryClient.invalidateQueries({
        queryKey: ["dashboard", "runtime-settings"],
      });
    },
  });

  return (
    <main className="grid grid-cols-1 lg:grid-cols-12 gap-6">
      <section className="lg:col-span-7 space-y-3">
        <div
          className="border border-border bg-card p-4"
          data-testid="panel-aggression-control"
        >
          <h2 className="text-xs uppercase tracking-widest text-muted-foreground mb-3">
            Aggression mode
          </h2>
          <p className="text-xs text-muted-foreground mb-4 normal-case tracking-normal">
            Sets the global Conservative / Balanced / Aggressive bias for the
            swarm and risk gates. Changes take effect on the next orchestrator
            cycle — the moderator and gatekeeper rebias, and min-edge / Kelly
            defaults retune to match.
          </p>
          {isLoading ? (
            <div className="h-10 animate-pulse bg-muted/30" />
          ) : error ? (
            <div className="text-loss text-xs">
              Failed to load runtime settings.
            </div>
          ) : data ? (
            <div className="space-y-4">
              <div className="flex flex-wrap gap-2">
                {data.available_modes.map((m) => {
                  const active = data.aggression_mode === m;
                  return (
                    <button
                      key={m}
                      type="button"
                      disabled={mutation.isPending || active}
                      onClick={() => mutation.mutate(m)}
                      data-testid={`button-aggression-${m}`}
                      className={`border px-4 py-2 uppercase text-xs tracking-widest ${
                        active
                          ? "border-primary text-primary bg-primary/10"
                          : "border-border text-muted-foreground hover:text-foreground hover:border-foreground"
                      } ${mutation.isPending ? "opacity-50" : ""}`}
                    >
                      {m}
                    </button>
                  );
                })}
              </div>
              {mutation.isError ? (
                <div className="text-loss text-xs">
                  Failed to update aggression mode.
                </div>
              ) : null}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 border-t border-border pt-4 text-xs">
                <ThresholdStat
                  label="MIN_EDGE"
                  value={`${data.thresholds.min_edge_pp.toFixed(1)}pp`}
                />
                <ThresholdStat
                  label="KELLY_CAP"
                  value={`${(data.thresholds.kelly_fraction_cap * 100).toFixed(0)}%`}
                />
                <ThresholdStat
                  label="MIN_AGREE"
                  value={`${(data.thresholds.min_agreement_rate * 100).toFixed(0)}%`}
                />
                <ThresholdStat
                  label="MAX_BAND"
                  value={`${(data.thresholds.max_confidence_band * 100).toFixed(0)}%`}
                />
              </div>
              {data.updated_at ? (
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground border-t border-border pt-3">
                  Updated {data.updated_at}
                  {data.updated_by ? ` by ${data.updated_by}` : ""}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </section>
    </main>
  );
}

function ThresholdStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div className="text-sm font-bold text-foreground">{value}</div>
    </div>
  );
}

function StatusPills({ user }: { user: AdminUser }) {
  return (
    <div className="flex flex-wrap gap-1">
      {user.is_admin ? (
        <span
          className="border border-primary text-primary px-1.5 py-0.5 text-[10px] uppercase tracking-widest"
          data-testid={`pill-admin-${user.clerk_user_id}`}
        >
          Admin
        </span>
      ) : null}
      {user.approved ? (
        <span
          className="border border-success text-success px-1.5 py-0.5 text-[10px] uppercase tracking-widest"
          data-testid={`pill-approved-${user.clerk_user_id}`}
        >
          Approved
        </span>
      ) : (
        <span
          className="border border-destructive text-destructive px-1.5 py-0.5 text-[10px] uppercase tracking-widest"
          data-testid={`pill-pending-${user.clerk_user_id}`}
        >
          Pending
        </span>
      )}
    </div>
  );
}

function UserRow({
  user,
  active,
  onSelect,
}: {
  user: AdminUser;
  active: boolean;
  onSelect: () => void;
}) {
  const qc = useQueryClient();
  const refresh = () => {
    qc.invalidateQueries({ queryKey: getAdminListUsersQueryKey() });
    qc.invalidateQueries({
      queryKey: getAdminGetUserQueryKey(user.clerk_user_id),
    });
    qc.invalidateQueries({ queryKey: getAdminGetAuditLogQueryKey() });
    qc.invalidateQueries({ queryKey: getGetMyAccessQueryKey() });
  };
  const setApproved = useAdminSetUserApproved({
    mutation: { onSuccess: refresh },
  });
  const setAdmin = useAdminSetUserAdmin({ mutation: { onSuccess: refresh } });

  const onApproveToggle = () => {
    const next = !user.approved;
    const verb = next ? "approve" : "revoke approval for";
    if (!window.confirm(`Are you sure you want to ${verb} ${user.email ?? user.clerk_user_id}?`))
      return;
    setApproved.mutate({ clerkUserId: user.clerk_user_id, data: { approved: next } });
  };

  const onAdminToggle = () => {
    const next = !user.is_admin;
    const verb = next ? "promote to admin" : "demote from admin";
    if (!window.confirm(`Are you sure you want to ${verb}: ${user.email ?? user.clerk_user_id}?`))
      return;
    setAdmin.mutate({ clerkUserId: user.clerk_user_id, data: { is_admin: next } });
  };

  return (
    <tr
      className={`border-t border-border ${active ? "bg-muted/20" : "hover:bg-muted/10"} cursor-pointer`}
      onClick={onSelect}
      data-testid={`row-user-${user.clerk_user_id}`}
    >
      <td className="p-2 align-top">
        <div className="text-foreground" data-testid={`text-user-email-${user.clerk_user_id}`}>
          {user.email ?? "(no email)"}
        </div>
        <div className="text-[10px] text-muted-foreground truncate max-w-[200px]">
          {user.display_name ?? user.clerk_user_id}
        </div>
      </td>
      <td className="p-2 align-top">
        <StatusPills user={user} />
        <div className="text-[10px] text-muted-foreground mt-1">
          seen {user.activity_count}x
        </div>
      </td>
      <td className="p-2 align-top text-muted-foreground">
        {user.clerk_created_at?.slice(0, 10) ?? "—"}
      </td>
      <td className="p-2 align-top text-muted-foreground">
        {user.clerk_last_sign_in_at?.replace("T", " ").replace("Z", "") ?? "—"}
      </td>
      <td className="p-2 align-top text-muted-foreground">
        {user.last_seen_at?.replace("T", " ").replace("Z", "") ?? "—"}
      </td>
      <td className="p-2 align-top">
        <div className="flex flex-wrap gap-1 justify-end" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            onClick={onApproveToggle}
            disabled={setApproved.isPending}
            data-testid={`button-approve-${user.clerk_user_id}`}
            className={`border px-2 py-1 text-[10px] uppercase tracking-widest disabled:opacity-50 ${
              user.approved
                ? "border-destructive text-destructive hover:bg-destructive/10"
                : "border-success text-success hover:bg-success/10"
            }`}
          >
            {user.approved ? "Revoke" : "Approve"}
          </button>
          <button
            type="button"
            onClick={onAdminToggle}
            disabled={setAdmin.isPending}
            data-testid={`button-admin-${user.clerk_user_id}`}
            className={`border px-2 py-1 text-[10px] uppercase tracking-widest disabled:opacity-50 ${
              user.is_admin
                ? "border-destructive text-destructive hover:bg-destructive/10"
                : "border-primary text-primary hover:bg-primary/10"
            }`}
          >
            {user.is_admin ? "Demote" : "Promote"}
          </button>
        </div>
      </td>
    </tr>
  );
}

function UserDetail({
  clerkUserId,
  onClose,
}: {
  clerkUserId: string;
  onClose: () => void;
}) {
  const { data, isLoading, isError } = useAdminGetUser(clerkUserId, {
    query: { queryKey: getAdminGetUserQueryKey(clerkUserId) },
  });
  return (
    <div className="border border-border bg-card" data-testid="panel-user-detail">
      <div className="flex items-center justify-between border-b border-border p-2 px-3 bg-muted/20">
        <h2 className="text-xs uppercase tracking-widest text-muted-foreground">User detail</h2>
        <button
          type="button"
          onClick={onClose}
          className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-destructive"
          data-testid="button-close-detail"
        >
          ✕
        </button>
      </div>
      <div className="p-3 text-xs space-y-3">
        {isLoading ? (
          <div className="text-muted-foreground">Loading…</div>
        ) : isError || !data ? (
          <div className="text-destructive">Failed to load user.</div>
        ) : (
          <>
            <Field label="Email" value={data.email ?? "—"} />
            <Field label="Display name" value={data.display_name ?? "—"} />
            <Field label="Clerk user id" value={data.clerk_user_id} />
            <Field label="Approval source" value={data.approval_source} />
            <Field label="Activity count" value={String(data.activity_count)} />
            <Field label="First seen" value={data.first_seen_at ?? "—"} />
            <Field label="Last seen" value={data.last_seen_at ?? "—"} />
            <Field
              label="Signed up (Clerk)"
              value={
                ((data.clerk_profile as Record<string, unknown> | undefined)?.created_at as
                  | number
                  | undefined)
                  ? new Date(
                      (data.clerk_profile as Record<string, number>).created_at,
                    ).toISOString()
                  : "—"
              }
            />
            <Field
              label="Last sign-in (Clerk)"
              value={
                ((data.clerk_profile as Record<string, unknown> | undefined)?.last_sign_in_at as
                  | number
                  | undefined)
                  ? new Date(
                      (data.clerk_profile as Record<string, number>).last_sign_in_at,
                    ).toISOString()
                  : "—"
              }
            />
            <div className="border-t border-border pt-2 mt-2">
              <div className="text-muted-foreground uppercase tracking-widest text-[10px] mb-1">
                Clerk profile
              </div>
              <pre className="text-[10px] leading-tight bg-background/40 border border-border p-2 max-h-64 overflow-auto">
{JSON.stringify(data.clerk_profile ?? {}, null, 2)}
              </pre>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-muted-foreground uppercase tracking-widest text-[10px]">
        {label}
      </span>
      <span className="text-foreground truncate max-w-[180px]" title={value}>
        {value}
      </span>
    </div>
  );
}

function AuditLogPanel() {
  const { data, isLoading, isError } = useAdminGetAuditLog(
    { limit: 100 },
    { query: { queryKey: getAdminGetAuditLogQueryKey({ limit: 100 }), refetchInterval: 30_000 } },
  );
  const items: AdminAuditLogEntry[] = data?.items ?? [];
  return (
    <div className="border border-border bg-card overflow-x-auto" data-testid="panel-audit-log">
      <table className="w-full text-xs">
        <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest">
          <tr>
            <th className="text-left p-2">When</th>
            <th className="text-left p-2">Actor</th>
            <th className="text-left p-2">Target</th>
            <th className="text-left p-2">Action</th>
            <th className="text-left p-2">Before → After</th>
          </tr>
        </thead>
        <tbody>
          {isLoading ? (
            <tr>
              <td colSpan={5} className="p-6 text-center text-muted-foreground">
                Loading…
              </td>
            </tr>
          ) : isError ? (
            <tr>
              <td colSpan={5} className="p-6 text-center text-destructive">
                Failed to load audit log.
              </td>
            </tr>
          ) : items.length === 0 ? (
            <tr>
              <td colSpan={5} className="p-6 text-center text-muted-foreground">
                No audit entries yet.
              </td>
            </tr>
          ) : (
            items.map((e) => (
              <tr
                key={e.id}
                className="border-t border-border"
                data-testid={`row-audit-${e.id}`}
              >
                <td className="p-2 align-top text-muted-foreground">
                  {e.created_at?.replace("T", " ").replace("Z", "") ?? "—"}
                </td>
                <td className="p-2 align-top">{e.actor_email ?? e.actor_user_id}</td>
                <td className="p-2 align-top">{e.target_email ?? e.target_user_id}</td>
                <td className="p-2 align-top text-primary uppercase tracking-widest text-[10px]">
                  {e.action}
                </td>
                <td className="p-2 align-top text-[10px] text-muted-foreground">
                  {JSON.stringify(e.before_value ?? {})} → {JSON.stringify(e.after_value ?? {})}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
