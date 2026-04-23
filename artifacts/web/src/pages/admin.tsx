import { useMemo, useState } from "react";
import { Link, Redirect } from "wouter";
import { useAuth } from "@clerk/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import {
  AdminAuditLogEntry,
  AdminUser,
  getAdminGetAuditLogQueryKey,
  getAdminGetUserQueryKey,
  getAdminListUsersQueryKey,
  getGetMyAccessQueryKey,
  useAdminGetUser,
  useAdminListUsers,
  useAdminSetUserAdmin,
  useAdminSetUserApproved,
  useGetVerticalsOverview,
  useAdminGetVerticalVisibility,
  useAdminSetVerticalVisibility,
  getAdminGetVerticalVisibilityQueryKey,
  getGetVerticalsOverviewQueryKey,
  getGetVerticalVisibilityQueryKey,
} from "@workspace/api-client-react";

type Filter = "all" | "pending" | "approved" | "admin";

export default function AdminPage() {
  // /admin has been folded into /system for admins. Keep the path live
  // but redirect operators to the unified Users tab so any bookmarks
  // (or links from older mobile builds) still land somewhere useful.
  return <Redirect to="/system?tab=users" />;
}

export function UserManagementPanel() {
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [activeUserId, setActiveUserId] = useState<string | null>(null);

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
    <main
      className="grid grid-cols-1 lg:grid-cols-12 gap-6"
      data-testid="panel-user-management"
    >
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

export function RuntimePanel() {
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

export function StatusPills({ user }: { user: AdminUser }) {
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

export function UserRow({
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

export function UserDetail({
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

export function AuditLogPanel() {
  // Server-side pagination (added per UI overhaul code review): we drive
  // /api/admin/audit-log with `limit` + `offset` query params via raw
  // apiFetch so we don't have to regen the typed client just to add an
  // offset arg. The endpoint returns `{ items, limit, offset }`; we infer
  // hasMore from `items.length === pageSize` (cheap and avoids a count(*)).
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const PAGE_SIZES = [25, 50, 100, 200] as const;
  const [pageSize, setPageSize] = useState<number>(50);
  const [page, setPage] = useState<number>(0); // zero-indexed

  // Filter inputs are split into "draft" (the live <input> value) and
  // "applied" (what the query actually sends). This avoids re-fetching on
  // every keystroke and lets the operator hit Enter / blur to commit.
  const [targetDraft, setTargetDraft] = useState("");
  const [actorDraft, setActorDraft] = useState("");
  const [actionApplied, setActionApplied] = useState("");
  const [targetApplied, setTargetApplied] = useState("");
  const [actorApplied, setActorApplied] = useState("");

  const applyFilters = () => {
    setTargetApplied(targetDraft.trim());
    setActorApplied(actorDraft.trim());
    setPage(0);
  };
  const clearFilters = () => {
    setTargetDraft("");
    setActorDraft("");
    setTargetApplied("");
    setActorApplied("");
    setActionApplied("");
    setPage(0);
  };

  type AuditPage = { items: AdminAuditLogEntry[]; limit: number; offset: number };
  const offset = page * pageSize;
  const buildUrl = () => {
    const qs = new URLSearchParams();
    qs.set("limit", String(pageSize));
    qs.set("offset", String(offset));
    if (targetApplied) qs.set("target", targetApplied);
    if (actorApplied) qs.set("actor", actorApplied);
    if (actionApplied) qs.set("action", actionApplied);
    return `/api/admin/audit-log?${qs.toString()}`;
  };
  const query = useQuery<AuditPage>({
    queryKey: [
      "admin",
      "audit-log",
      pageSize,
      offset,
      targetApplied,
      actorApplied,
      actionApplied,
    ],
    queryFn: () => apiFetch<AuditPage>(buildUrl(), { getToken: tokenFn }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  });

  type ActionsList = { items: string[] };
  const actionsQuery = useQuery<ActionsList>({
    queryKey: ["admin", "audit-log", "actions"],
    queryFn: () =>
      apiFetch<ActionsList>("/api/admin/audit-log/actions", { getToken: tokenFn }),
    enabled: !!isSignedIn,
    staleTime: 60_000,
  });
  const knownActions = actionsQuery.data?.items ?? [];
  const hasActiveFilters = !!(targetApplied || actorApplied || actionApplied);

  const items: AdminAuditLogEntry[] = query.data?.items ?? [];
  const hasMore = items.length === pageSize;
  const isLoading = query.isLoading;
  const isError = query.isError;

  const goPrev = () => setPage((p) => Math.max(0, p - 1));
  const goNext = () => {
    if (hasMore) setPage((p) => p + 1);
  };
  const onChangePageSize = (next: number) => {
    setPageSize(next);
    setPage(0);
  };

  const rangeStart = items.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + items.length;

  return (
    <div className="space-y-3" data-testid="panel-audit-log">
      <div className="flex flex-wrap items-end gap-2 text-[11px] uppercase tracking-widest text-muted-foreground border border-border bg-card p-2">
        <label className="flex flex-col gap-1">
          <span>Target user</span>
          <input
            value={targetDraft}
            onChange={(e) => setTargetDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") applyFilters();
            }}
            onBlur={() => {
              if (targetDraft.trim() !== targetApplied) applyFilters();
            }}
            placeholder="email or user id"
            data-testid="input-audit-filter-target"
            className="bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal w-56 max-w-full normal-case text-foreground"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span>Actor</span>
          <input
            value={actorDraft}
            onChange={(e) => setActorDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") applyFilters();
            }}
            onBlur={() => {
              if (actorDraft.trim() !== actorApplied) applyFilters();
            }}
            placeholder="email or user id"
            data-testid="input-audit-filter-actor"
            className="bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal w-56 max-w-full normal-case text-foreground"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span>Action</span>
          <select
            value={actionApplied}
            onChange={(e) => {
              setActionApplied(e.target.value);
              setPage(0);
            }}
            data-testid="select-audit-filter-action"
            className="bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal text-foreground min-w-[10rem]"
          >
            <option value="">all actions</option>
            {knownActions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={applyFilters}
          data-testid="button-audit-apply-filters"
          className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-foreground hover:bg-muted/30"
        >
          Apply
        </button>
        <button
          type="button"
          onClick={clearFilters}
          disabled={!hasActiveFilters && !targetDraft && !actorDraft}
          data-testid="button-audit-clear-filters"
          className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
        >
          Clear
        </button>
        {hasActiveFilters ? (
          <span
            className="text-primary text-[10px] tracking-widest"
            data-testid="text-audit-filter-active"
          >
            filters on
          </span>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] uppercase tracking-widest text-muted-foreground">
        <div data-testid="text-audit-range">
          {isLoading && !query.data
            ? "Loading…"
            : isError
              ? "—"
              : `Showing ${rangeStart}–${rangeEnd} (page ${page + 1})`}
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1">
            <span>Page size</span>
            <select
              value={pageSize}
              onChange={(e) => onChangePageSize(Number(e.target.value))}
              className="border border-border bg-card px-1 py-0.5 text-[11px] uppercase tracking-widest text-foreground"
              data-testid="select-audit-page-size"
            >
              {PAGE_SIZES.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={goPrev}
            disabled={page === 0 || isLoading}
            className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            data-testid="button-audit-prev"
          >
            ← Prev
          </button>
          <button
            type="button"
            onClick={goNext}
            disabled={!hasMore || isLoading}
            className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            data-testid="button-audit-next"
          >
            Next →
          </button>
        </div>
      </div>
      <div className="border border-border bg-card overflow-x-auto">
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
            {isLoading && !query.data ? (
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
                  {hasActiveFilters
                    ? "No audit entries match the current filters."
                    : page === 0
                      ? "No audit entries yet."
                      : "No more entries."}
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
    </div>
  );
}

// ---------------------------------------------------------------------------
// Risk knob editor panel
// ---------------------------------------------------------------------------

type RiskKnob = {
  key: string;
  label: string;
  description: string;
  minimum: number;
  maximum: number;
  step: number;
  kind: "float" | "int";
  unit: "pp" | "fraction" | "usd" | "count";
  default_value: number | null;
  override_value: number | null;
  effective_value: number | null;
};

type RiskKnobState = {
  aggression_mode: string;
  knobs: RiskKnob[];
};

function formatKnob(value: number | null | undefined, unit: RiskKnob["unit"]) {
  if (value == null) return "—";
  if (unit === "pp") return `${value.toFixed(1)}pp`;
  if (unit === "fraction") return `${(value * 100).toFixed(0)}%`;
  if (unit === "usd")
    return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  return String(value);
}

export function RiskKnobsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "risk-knobs"] as const;

  const { data, isLoading, error } = useQuery<RiskKnobState>({
    queryKey,
    queryFn: () =>
      apiFetch<RiskKnobState>("/api/admin/risk-knobs", { getToken: tokenFn }),
    enabled: !!isSignedIn,
  });

  // Local draft of slider positions so the UI stays responsive while
  // the operator drags. Only flushed to the server on "Apply".
  const [draft, setDraft] = useState<Record<string, number>>({});
  const draftKeys = Object.keys(draft);

  const dirty = useMemo(() => {
    if (!data) return [] as string[];
    return draftKeys.filter((k) => {
      const knob = data.knobs.find((n) => n.key === k);
      if (!knob) return false;
      const eff = knob.effective_value ?? 0;
      return Math.abs(eff - draft[k]) > 1e-9;
    });
  }, [draft, data, draftKeys]);

  const apply = useMutation({
    mutationFn: () =>
      apiFetch<RiskKnobState>("/api/admin/risk-knobs", {
        method: "POST",
        body: { overrides: Object.fromEntries(dirty.map((k) => [k, draft[k]])) },
        getToken: tokenFn,
      }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
      setDraft({});
    },
  });

  const reset = useMutation({
    mutationFn: (key: string) =>
      apiFetch<RiskKnobState>("/api/admin/risk-knobs", {
        method: "POST",
        body: { overrides: { [key]: null } },
        getToken: tokenFn,
      }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
      setDraft((d) => {
        const out = { ...d };
        return out;
      });
    },
  });

  return (
    <main
      className="grid grid-cols-1 gap-4"
      data-testid="panel-risk-knobs"
    >
      <div className="border border-border bg-card p-4">
        <div className="flex items-start justify-between gap-4 mb-3">
          <div>
            <h2 className="text-xs uppercase tracking-widest text-muted-foreground">
              Live risk knobs
            </h2>
            <p className="text-xs text-muted-foreground mt-1 normal-case tracking-normal">
              Per-knob overrides applied on top of the active aggression mode.
              Changes take effect on the next orchestrator cycle and are
              recorded in the admin audit log. Clear an override to fall back
              to the mode default.
            </p>
          </div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground whitespace-nowrap">
            Mode: {data?.aggression_mode ?? "—"}
          </div>
        </div>
        {isLoading ? (
          <div className="h-32 animate-pulse bg-muted/30" />
        ) : error ? (
          <div className="text-loss text-xs">Failed to load risk knobs.</div>
        ) : data ? (
          <div className="space-y-3">
            {data.knobs.map((knob) => {
              const draftVal =
                draft[knob.key] ?? knob.effective_value ?? knob.minimum;
              const isDirty = dirty.includes(knob.key);
              return (
                <div
                  key={knob.key}
                  className="border border-border p-3 bg-background"
                  data-testid={`row-knob-${knob.key}`}
                >
                  <div className="flex items-baseline justify-between gap-4">
                    <div>
                      <div className="text-xs uppercase tracking-widest text-foreground">
                        {knob.label}
                      </div>
                      <div className="text-[10px] text-muted-foreground normal-case tracking-normal">
                        {knob.description}
                      </div>
                    </div>
                    <div className="text-right text-[10px] uppercase tracking-widest text-muted-foreground">
                      <div>
                        Default {formatKnob(knob.default_value, knob.unit)}
                      </div>
                      <div>
                        Override{" "}
                        <span
                          className={
                            knob.override_value != null
                              ? "text-primary"
                              : "text-muted-foreground"
                          }
                        >
                          {knob.override_value != null
                            ? formatKnob(knob.override_value, knob.unit)
                            : "none"}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 mt-3">
                    <input
                      type="range"
                      min={knob.minimum}
                      max={knob.maximum}
                      step={knob.step}
                      value={draftVal}
                      onChange={(e) =>
                        setDraft((d) => ({
                          ...d,
                          [knob.key]: Number(e.target.value),
                        }))
                      }
                      className="flex-1 accent-primary"
                      data-testid={`slider-knob-${knob.key}`}
                    />
                    <input
                      type="number"
                      min={knob.minimum}
                      max={knob.maximum}
                      step={knob.step}
                      value={draftVal}
                      onChange={(e) =>
                        setDraft((d) => ({
                          ...d,
                          [knob.key]: Number(e.target.value),
                        }))
                      }
                      className="w-24 bg-input border border-border px-2 py-1 text-xs"
                      data-testid={`input-knob-${knob.key}`}
                    />
                    <span
                      className={`text-xs uppercase tracking-widest ${
                        isDirty ? "text-primary" : "text-muted-foreground"
                      }`}
                    >
                      {formatKnob(draftVal, knob.unit)}
                    </span>
                    {knob.override_value != null ? (
                      <button
                        type="button"
                        disabled={reset.isPending}
                        onClick={() => reset.mutate(knob.key)}
                        data-testid={`button-knob-reset-${knob.key}`}
                        className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-destructive hover:border-destructive"
                      >
                        Clear
                      </button>
                    ) : null}
                  </div>
                </div>
              );
            })}
            <div className="flex items-center gap-3 pt-2">
              <button
                type="button"
                disabled={dirty.length === 0 || apply.isPending}
                onClick={() => apply.mutate()}
                data-testid="button-knobs-apply"
                className={`border px-4 py-2 text-xs uppercase tracking-widest ${
                  dirty.length === 0
                    ? "border-border text-muted-foreground"
                    : "border-primary text-primary hover:bg-primary/10"
                }`}
              >
                {apply.isPending
                  ? "Applying…"
                  : `Apply ${dirty.length} change${dirty.length === 1 ? "" : "s"}`}
              </button>
              {dirty.length > 0 ? (
                <button
                  type="button"
                  disabled={apply.isPending}
                  onClick={() => setDraft({})}
                  data-testid="button-knobs-discard"
                  className="border border-border px-3 py-2 text-xs uppercase tracking-widest text-muted-foreground hover:text-foreground"
                >
                  Discard
                </button>
              ) : null}
              {apply.isError ? (
                <span className="text-loss text-xs">
                  {(apply.error as Error)?.message ?? "Failed to apply."}
                </span>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Trigger cycle panel
// ---------------------------------------------------------------------------

type CycleResult = {
  cycle_id: string;
  vertical: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  picks_evaluated: number;
  trades_placed: number;
  fresh_debates: number;
  errors: string[];
  trace: Array<{
    cycle_id?: string;
    stage?: string;
    event?: string;
    ts?: string;
    [k: string]: unknown;
  }>;
};

export function TriggerCyclePanel() {
  const { getToken } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const [vertical, setVertical] = useState("sports");
  const [last, setLast] = useState<CycleResult | null>(null);

  const trigger = useMutation({
    mutationFn: () =>
      apiFetch<CycleResult>("/api/admin/orchestrator/run-cycle", {
        method: "POST",
        body: { vertical },
        getToken: tokenFn,
      }),
    onSuccess: (res) => setLast(res),
  });

  return (
    <main className="grid grid-cols-1 gap-4" data-testid="panel-trigger-cycle">
      <div className="border border-border bg-card p-4">
        <h2 className="text-xs uppercase tracking-widest text-muted-foreground mb-3">
          Trigger one orchestrator cycle
        </h2>
        <p className="text-xs text-muted-foreground mb-4 normal-case tracking-normal">
          Runs a single out-of-band cycle for the chosen vertical and shows
          the pipeline trace it produced. Refused (503) until the readiness
          gate has flipped.
        </p>
        <div className="flex items-center gap-2 mb-3">
          <label className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Vertical
          </label>
          <input
            value={vertical}
            onChange={(e) => setVertical(e.target.value)}
            data-testid="input-cycle-vertical"
            className="bg-input border border-border px-2 py-1 text-xs w-40"
          />
          <button
            type="button"
            disabled={trigger.isPending || !vertical.trim()}
            onClick={() => trigger.mutate()}
            data-testid="button-trigger-cycle"
            className={`border px-4 py-1.5 text-xs uppercase tracking-widest ${
              trigger.isPending
                ? "border-border text-muted-foreground"
                : "border-primary text-primary hover:bg-primary/10"
            }`}
          >
            {trigger.isPending ? "Running…" : "Trigger cycle"}
          </button>
          {trigger.isError ? (
            <span className="text-loss text-xs">
              {(trigger.error as Error)?.message ?? "Cycle failed."}
            </span>
          ) : null}
        </div>
        {last ? (
          <div
            className="border border-border bg-background p-3"
            data-testid="block-cycle-result"
          >
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
              <ThresholdStat label="CYCLE_ID" value={last.cycle_id.slice(0, 12)} />
              <ThresholdStat label="VERTICAL" value={last.vertical} />
              <ThresholdStat
                label="DURATION"
                value={
                  last.duration_seconds != null
                    ? `${last.duration_seconds.toFixed(2)}s`
                    : "—"
                }
              />
              <ThresholdStat
                label="PICKS"
                value={String(last.picks_evaluated)}
              />
              <ThresholdStat
                label="TRADES"
                value={String(last.trades_placed)}
              />
            </div>
            {last.errors.length > 0 ? (
              <div className="mt-3 text-xs text-loss">
                {last.errors.length} error(s):
                <ul className="list-disc list-inside">
                  {last.errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            <div className="mt-4">
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
                Pipeline trace ({last.trace.length} events)
              </div>
              <div
                className="max-h-96 overflow-auto border border-border bg-muted/10"
                data-testid="block-cycle-trace"
              >
                <table className="w-full text-[11px]">
                  <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest text-[10px]">
                    <tr>
                      <th className="text-left p-2">Stage</th>
                      <th className="text-left p-2">Event</th>
                      <th className="text-left p-2">Time</th>
                      <th className="text-left p-2">Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {last.trace.length === 0 ? (
                      <tr>
                        <td
                          colSpan={4}
                          className="p-3 text-center text-muted-foreground"
                        >
                          No trace events captured.
                        </td>
                      </tr>
                    ) : (
                      last.trace.map((ev, i) => {
                        const detail = { ...ev };
                        delete (detail as Record<string, unknown>).stage;
                        delete (detail as Record<string, unknown>).event;
                        delete (detail as Record<string, unknown>).ts;
                        delete (detail as Record<string, unknown>).cycle_id;
                        return (
                          <tr
                            key={i}
                            className="border-t border-border/50 align-top"
                          >
                            <td className="p-2 text-foreground">
                              {String(ev.stage ?? "")}
                            </td>
                            <td className="p-2 text-primary">
                              {String(ev.event ?? "")}
                            </td>
                            <td className="p-2 text-muted-foreground">
                              {String(ev.ts ?? "")}
                            </td>
                            <td className="p-2 text-muted-foreground font-mono break-all">
                              {Object.keys(detail).length > 0
                                ? JSON.stringify(detail)
                                : ""}
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Models browser panel
// ---------------------------------------------------------------------------

type ModelRow = {
  name: string;
  version: string;
  sha256: string;
  size: number;
  mtime: number;
  loaded: boolean;
  last_loaded_at: string | null;
  smoke_load_seconds: number | null;
  is_active: boolean;
  is_override: boolean;
};

type ModelsPayload = {
  items: ModelRow[];
  ready: boolean;
  in_progress: boolean;
  active_predictor: { name?: string; version?: string } | null;
  active_override: string | null;
  manifest: Record<string, unknown> | null;
};

function fmtSize(bytes: number): string {
  if (!bytes) return "—";
  const units = ["B", "KiB", "MiB", "GiB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}

function fmtTime(epochSeconds: number): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toISOString().replace("T", " ").slice(0, 19);
}

export function ModelsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "models"] as const;

  const { data, isLoading, error } = useQuery<ModelsPayload>({
    queryKey,
    queryFn: () =>
      apiFetch<ModelsPayload>("/api/admin/models", { getToken: tokenFn }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  const setActive = useMutation({
    mutationFn: (name: string | null) =>
      apiFetch<ModelsPayload>("/api/admin/models/active", {
        method: "POST",
        body: { name },
        getToken: tokenFn,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
    },
  });

  return (
    <main className="grid grid-cols-1 gap-4" data-testid="panel-models">
      <div className="border border-border bg-card p-4">
        <div className="flex items-start justify-between gap-4 mb-3">
          <div>
            <h2 className="text-xs uppercase tracking-widest text-muted-foreground">
              Predictor catalog
            </h2>
            <p className="text-xs text-muted-foreground mt-1 normal-case tracking-normal">
              Versioned predictor artifacts in storage. Click "Make active" to
              pin one as the predictor used by the next /predict call. Pinning
              survives a restart and is recorded in the audit log.
            </p>
          </div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground text-right">
            <div>
              Active:{" "}
              <span className="text-primary">
                {data?.active_predictor?.name ?? "—"}
              </span>
            </div>
            <div>
              Override:{" "}
              <span
                className={
                  data?.active_override ? "text-primary" : "text-muted-foreground"
                }
              >
                {data?.active_override ?? "none"}
              </span>
            </div>
            {data?.active_override ? (
              <button
                type="button"
                disabled={setActive.isPending}
                onClick={() => setActive.mutate(null)}
                data-testid="button-clear-active-override"
                className="mt-1 border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-destructive hover:border-destructive"
              >
                Clear override
              </button>
            ) : null}
          </div>
        </div>
        {isLoading ? (
          <div className="h-32 animate-pulse bg-muted/30" />
        ) : error ? (
          <div className="text-loss text-xs">Failed to load model catalog.</div>
        ) : data ? (
          <div className="border border-border bg-background overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest">
                <tr>
                  <th className="text-left p-2">Name</th>
                  <th className="text-left p-2">Version</th>
                  <th className="text-left p-2">SHA-256</th>
                  <th className="text-right p-2">Size</th>
                  <th className="text-left p-2">Updated</th>
                  <th className="text-left p-2">Last loaded</th>
                  <th className="text-right p-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {data.items.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="p-6 text-center text-muted-foreground">
                      No predictors in catalog.
                    </td>
                  </tr>
                ) : (
                  data.items.map((m) => (
                    <tr
                      key={m.name}
                      className="border-t border-border/50 align-top"
                      data-testid={`row-model-${m.name}`}
                    >
                      <td className="p-2 text-foreground break-all">
                        {m.name}
                        {m.is_active ? (
                          <span className="ml-2 border border-primary text-primary px-1.5 py-0.5 text-[10px] uppercase tracking-widest">
                            Active
                          </span>
                        ) : null}
                        {m.loaded && !m.is_active ? (
                          <span className="ml-2 border border-success text-success px-1.5 py-0.5 text-[10px] uppercase tracking-widest">
                            Loaded
                          </span>
                        ) : null}
                      </td>
                      <td className="p-2 text-muted-foreground">{m.version || "—"}</td>
                      <td className="p-2 text-muted-foreground font-mono">
                        {m.sha256 ? `${m.sha256.slice(0, 12)}…` : "—"}
                      </td>
                      <td className="p-2 text-muted-foreground text-right">
                        {fmtSize(m.size)}
                      </td>
                      <td className="p-2 text-muted-foreground">{fmtTime(m.mtime)}</td>
                      <td className="p-2 text-muted-foreground">
                        {m.last_loaded_at ?? "—"}
                      </td>
                      <td className="p-2 text-right">
                        {m.is_override ? (
                          <span className="text-[10px] uppercase tracking-widest text-primary">
                            Pinned
                          </span>
                        ) : (
                          <button
                            type="button"
                            disabled={setActive.isPending}
                            onClick={() => setActive.mutate(m.name)}
                            data-testid={`button-make-active-${m.name}`}
                            className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest hover:text-primary hover:border-primary"
                          >
                            Make active
                          </button>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        ) : null}
        {setActive.isError ? (
          <div className="text-loss text-xs mt-2">
            {(setActive.error as Error)?.message ?? "Failed to set active model."}
          </div>
        ) : null}
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Personas tab — per-vertical persona enable/disable (Task #194)
// ---------------------------------------------------------------------------

type PersonaToggleRow = {
  id: string;
  display: string;
  provider: string | null;
  model_id: string | null;
  enabled: boolean;
};

type PersonaToggleVertical = {
  vertical: string;
  vertical_label: string;
  persona_profile: string | null;
  profile_description: string | null;
  personas: PersonaToggleRow[];
  orphaned?: boolean;
};

type PersonaToggleSummary = {
  items: PersonaToggleVertical[];
  available_profiles: { name: string; description: string; personas: string[] }[];
};

export function PersonasPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "persona-toggles"] as const;

  const { data, isLoading, error } = useQuery<PersonaToggleSummary>({
    queryKey,
    queryFn: () =>
      apiFetch<PersonaToggleSummary>("/api/admin/persona-toggles", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
  });

  const mutation = useMutation({
    mutationFn: (vars: {
      vertical: string;
      persona: string;
      enabled: boolean;
    }) =>
      apiFetch<unknown>("/api/admin/persona-toggles", {
        method: "POST",
        body: vars,
        getToken: tokenFn,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
    },
  });

  return (
    <main
      className="grid grid-cols-1 gap-6"
      data-testid="panel-persona-toggles"
    >
      <section className="border border-border bg-card p-4">
        <h2 className="text-xs uppercase tracking-widest text-muted-foreground mb-3">
          Per-vertical persona profiles
        </h2>
        <p className="text-xs text-muted-foreground normal-case tracking-normal mb-4">
          Each vertical runs its own swarm profile. Toggle individual personas
          off when you want the swarm to ignore one in that domain — the change
          applies on the next debate. Disabling every persona in a vertical
          falls back to the full profile so the swarm never silently drops to
          a single LLM call. The moderator and gatekeeper layer is unaffected.
        </p>
        {isLoading ? (
          <div className="h-24 animate-pulse bg-muted/30" />
        ) : error ? (
          <div className="text-loss text-xs">
            Failed to load persona toggles.
          </div>
        ) : data ? (
          <div className="space-y-4">
            {data.items.map((v) => (
              <div
                key={v.vertical}
                className="border border-border"
                data-testid={`persona-vertical-${v.vertical}`}
              >
                <header className="flex flex-wrap items-baseline gap-2 px-3 py-2 bg-muted/20">
                  <h3 className="text-xs uppercase tracking-widest">
                    {v.vertical_label}
                  </h3>
                  <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    profile: {v.persona_profile ?? "—"}
                    {v.orphaned ? " (orphaned override)" : ""}
                  </span>
                  {v.profile_description ? (
                    <span className="text-[11px] text-muted-foreground normal-case tracking-normal ml-auto">
                      {v.profile_description}
                    </span>
                  ) : null}
                </header>
                <table className="w-full text-xs">
                  <thead className="bg-muted/10 text-muted-foreground uppercase tracking-widest">
                    <tr>
                      <th className="text-left p-2">Persona</th>
                      <th className="text-left p-2">Provider / model</th>
                      <th className="text-right p-2">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {v.personas.map((p) => (
                      <tr key={p.id} className="border-t border-border">
                        <td className="p-2">
                          <div>{p.display}</div>
                          <div className="text-[10px] text-muted-foreground font-mono">
                            {p.id}
                          </div>
                        </td>
                        <td className="p-2 text-muted-foreground">
                          {p.provider ?? "—"} / {p.model_id ?? "—"}
                        </td>
                        <td className="p-2 text-right">
                          <button
                            type="button"
                            disabled={mutation.isPending}
                            onClick={() =>
                              mutation.mutate({
                                vertical: v.vertical,
                                persona: p.id,
                                enabled: !p.enabled,
                              })
                            }
                            data-testid={`button-toggle-${v.vertical}-${p.id}`}
                            className={`border px-3 py-1 text-[10px] uppercase tracking-widest ${
                              p.enabled
                                ? "border-primary text-primary"
                                : "border-border text-muted-foreground hover:text-foreground"
                            } ${mutation.isPending ? "opacity-50" : ""}`}
                          >
                            {p.enabled ? "Enabled" : "Disabled"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        ) : null}
        {mutation.isError ? (
          <div className="text-loss text-xs mt-2">
            {(mutation.error as Error)?.message ?? "Toggle failed."}
          </div>
        ) : null}
      </section>
    </main>
  );
}

export function VerticalVisibilityPanel() {
  const qc = useQueryClient();
  const overview = useGetVerticalsOverview(
    { days: 7 },
    {
      query: {
        queryKey: getGetVerticalsOverviewQueryKey({ days: 7 }),
        staleTime: 30_000,
      },
    },
  );
  const visibility = useAdminGetVerticalVisibility({
    query: {
      queryKey: getAdminGetVerticalVisibilityQueryKey(),
      staleTime: 30_000,
    },
  });

  const mutation = useAdminSetVerticalVisibility({
    mutation: {
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: getAdminGetVerticalVisibilityQueryKey() });
        qc.invalidateQueries({ queryKey: getGetVerticalsOverviewQueryKey() });
        qc.invalidateQueries({ queryKey: getGetVerticalVisibilityQueryKey() });
      },
    },
  });

  const all = overview.data?.verticals ?? [];
  const hidden = new Set(visibility.data?.hidden ?? []);
  const pinned = visibility.data?.pinned ?? [];

  function toggleHidden(name: string) {
    const next = new Set(hidden);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    mutation.mutate({ data: { hidden: Array.from(next), pinned } });
  }

  function togglePinned(name: string) {
    const next = pinned.includes(name)
      ? pinned.filter((p) => p !== name)
      : [...pinned, name];
    mutation.mutate({ data: { hidden: Array.from(hidden), pinned: next } });
  }

  return (
    <main className="border border-border bg-card p-4">
      <header className="mb-3 pb-2 border-b border-border">
        <h2 className="text-sm uppercase tracking-widest font-bold text-primary">
          Vertical visibility
        </h2>
        <p className="text-xs text-muted-foreground mt-1">
          Pinned verticals lead the dashboard strip in pin order. Hidden
          verticals are removed from the strip for non-admin operators
          (admins still see them in the picker, dimmed). Every change is
          written to <code>system_settings</code> and audited.
        </p>
      </header>

      {(overview.isLoading || visibility.isLoading) && (
        <div className="text-xs text-muted-foreground uppercase tracking-widest">
          Loading…
        </div>
      )}

      <table className="w-full text-xs">
        <thead className="text-muted-foreground uppercase tracking-widest">
          <tr>
            <th className="text-left p-2">Vertical</th>
            <th className="text-left p-2">Description</th>
            <th className="text-left p-2">Pinned</th>
            <th className="text-left p-2">Hidden</th>
          </tr>
        </thead>
        <tbody>
          {all.map((entry) => {
            const pinIdx = pinned.indexOf(entry.vertical);
            const isPinned = pinIdx >= 0;
            const isHidden = hidden.has(entry.vertical);
            return (
              <tr
                key={entry.vertical}
                className="border-t border-border"
                data-testid={`row-vertical-vis-${entry.vertical}`}
              >
                <td className="p-2 uppercase font-bold text-primary">{entry.vertical}</td>
                <td className="p-2 text-muted-foreground">{entry.description ?? "—"}</td>
                <td className="p-2">
                  <button
                    type="button"
                    onClick={() => togglePinned(entry.vertical)}
                    disabled={mutation.isPending}
                    className={`px-2 py-0.5 border text-[10px] uppercase tracking-widest ${
                      isPinned
                        ? "border-primary text-primary"
                        : "border-border text-muted-foreground hover:text-primary"
                    }`}
                    data-testid={`button-pin-${entry.vertical}`}
                  >
                    {isPinned ? `★ ${pinIdx + 1}` : "Pin"}
                  </button>
                </td>
                <td className="p-2">
                  <button
                    type="button"
                    onClick={() => toggleHidden(entry.vertical)}
                    disabled={mutation.isPending}
                    className={`px-2 py-0.5 border text-[10px] uppercase tracking-widest ${
                      isHidden
                        ? "border-destructive text-destructive"
                        : "border-border text-muted-foreground hover:text-destructive"
                    }`}
                    data-testid={`button-hide-${entry.vertical}`}
                  >
                    {isHidden ? "Hidden" : "Hide"}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {mutation.isError && (
        <div className="mt-3 text-xs text-destructive">
          Update failed. {(mutation.error as Error)?.message ?? ""}
        </div>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// RevenueCat (mobile IAP) webhook health tile
//
// Surfaces the last 24h of webhook deliveries split by status. A silent
// outage means paying mobile customers stop being upgraded, so we want
// the operator to spot a spike or a long stretch of silence at a
// glance. Backed by /api/admin/rc-webhook/stats.
// ---------------------------------------------------------------------------

type RcWebhookStats = {
  hours: number;
  totals: Record<string, number>;
  hourly: { hour: string | null; status: string; count: number }[];
  last_event_at: string | null;
  last_5xx_at: string | null;
  alert: {
    failure_threshold_pct: number;
    failures_last_hour: number;
    failure_rate_pct: number;
    silence_threshold_seconds: number;
    business_hours_utc: number[];
    in_business_hours: boolean;
  };
};

const RC_STATUSES = ["ok", "duplicate", "http_4xx", "http_5xx"] as const;
const RC_STATUS_LABEL: Record<(typeof RC_STATUSES)[number], string> = {
  ok: "OK",
  duplicate: "Duplicate",
  http_4xx: "4xx",
  http_5xx: "5xx",
};
const RC_STATUS_COLOR: Record<(typeof RC_STATUSES)[number], string> = {
  ok: "bg-emerald-500/70",
  duplicate: "bg-sky-500/60",
  http_4xx: "bg-amber-500/70",
  http_5xx: "bg-rose-500/80",
};

function _fmtRelative(iso: string | null): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export function RcWebhookPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const query = useQuery<RcWebhookStats>({
    queryKey: ["admin", "rc-webhook-stats"],
    queryFn: () =>
      apiFetch<RcWebhookStats>("/api/admin/rc-webhook/stats?hours=24", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  });

  const data = query.data;

  // Bucket the hourly rows by hour key for a 24-column stacked bar.
  const buckets = useMemo(() => {
    if (!data) return [] as { hour: string; counts: Record<string, number>; total: number }[];
    const byHour = new Map<string, Record<string, number>>();
    for (const r of data.hourly) {
      if (!r.hour) continue;
      const slot = byHour.get(r.hour) ?? {};
      slot[r.status] = (slot[r.status] ?? 0) + r.count;
      byHour.set(r.hour, slot);
    }
    const sorted = Array.from(byHour.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([hour, counts]) => {
        const total = RC_STATUSES.reduce((s, k) => s + (counts[k] ?? 0), 0);
        return { hour, counts, total };
      });
    return sorted.slice(-24);
  }, [data]);

  const maxBucket = useMemo(
    () => buckets.reduce((m, b) => Math.max(m, b.total), 0) || 1,
    [buckets],
  );

  // Health summary line shown above the chart.
  const healthBadge = useMemo(() => {
    if (!data) return { label: "Loading", tone: "text-muted-foreground" };
    const failPct = data.alert.failure_rate_pct;
    const threshold = data.alert.failure_threshold_pct;
    const lastEventSec = data.last_event_at
      ? (Date.now() - Date.parse(data.last_event_at)) / 1000
      : Infinity;
    if (failPct >= threshold && data.alert.failures_last_hour > 0) {
      return {
        label: `5xx rate ${failPct.toFixed(0)}% — alert tripped`,
        tone: "text-destructive",
      };
    }
    if (
      data.alert.in_business_hours &&
      lastEventSec > data.alert.silence_threshold_seconds
    ) {
      return {
        label: "Silent during business hours — alert tripped",
        tone: "text-destructive",
      };
    }
    if (data.totals.http_5xx > 0) {
      return {
        label: `${data.totals.http_5xx} 5xx in 24h`,
        tone: "text-amber-500",
      };
    }
    return { label: "Healthy", tone: "text-emerald-500" };
  }, [data]);

  return (
    <main className="space-y-3" data-testid="panel-rc-webhook">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-xs uppercase tracking-widest text-muted-foreground">
            Mobile IAP webhook (RevenueCat) · 24h
          </h2>
          <p className="text-[11px] text-muted-foreground mt-1 normal-case tracking-normal">
            Counts every delivery the api-server has seen — including
            duplicates and rejects — so a silent dashboard pause or a
            spike in 5xx errors is visible without grepping logs.
          </p>
        </div>
        <div
          className={`text-[11px] uppercase tracking-widest ${healthBadge.tone}`}
          data-testid="text-rc-webhook-health"
        >
          {healthBadge.label}
        </div>
      </div>

      {query.isLoading && !data ? (
        <div className="h-32 animate-pulse bg-muted/30" />
      ) : query.isError ? (
        <div className="text-xs text-destructive">
          Failed to load webhook stats. {(query.error as Error)?.message ?? ""}
        </div>
      ) : data ? (
        <>
          <div
            className="grid grid-cols-2 gap-2 sm:grid-cols-4"
            data-testid="grid-rc-webhook-totals"
          >
            {RC_STATUSES.map((s) => (
              <div
                key={s}
                className="border border-border bg-card p-2"
                data-testid={`tile-rc-${s}`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`inline-block h-2 w-2 rounded-sm ${RC_STATUS_COLOR[s]}`}
                  />
                  <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    {RC_STATUS_LABEL[s]}
                  </span>
                </div>
                <div className="mt-1 font-mono text-lg text-foreground">
                  {data.totals[s] ?? 0}
                </div>
              </div>
            ))}
          </div>

          <div className="border border-border bg-card p-2">
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
              Hourly deliveries
            </div>
            {buckets.length === 0 ? (
              <div className="text-[11px] text-muted-foreground py-6 text-center">
                No webhook deliveries in the last 24h.
              </div>
            ) : (
              <div
                className="flex h-24 items-end gap-1"
                data-testid="chart-rc-webhook-hourly"
              >
                {buckets.map((b) => {
                  const heightPct = (b.total / maxBucket) * 100;
                  return (
                    <div
                      key={b.hour}
                      className="flex-1 flex flex-col-reverse"
                      title={`${b.hour}\n${RC_STATUSES.map(
                        (s) => `${RC_STATUS_LABEL[s]}: ${b.counts[s] ?? 0}`,
                      ).join("\n")}`}
                      style={{ height: `${heightPct}%` }}
                    >
                      {RC_STATUSES.map((s) => {
                        const c = b.counts[s] ?? 0;
                        if (c === 0) return null;
                        const segPct = (c / b.total) * 100;
                        return (
                          <div
                            key={s}
                            className={RC_STATUS_COLOR[s]}
                            style={{ height: `${segPct}%` }}
                          />
                        );
                      })}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 gap-1 text-[11px] text-muted-foreground sm:grid-cols-2">
            <div>
              Last event:{" "}
              <span
                className="text-foreground"
                data-testid="text-rc-last-event"
              >
                {_fmtRelative(data.last_event_at)}
              </span>
            </div>
            <div>
              Last 5xx:{" "}
              <span className="text-foreground">
                {_fmtRelative(data.last_5xx_at)}
              </span>
            </div>
            <div>
              5xx alert at ≥{data.alert.failure_threshold_pct.toFixed(0)}%/hr ·
              now {data.alert.failure_rate_pct.toFixed(1)}%
            </div>
            <div>
              Silence alert ≥{Math.round(data.alert.silence_threshold_seconds / 60)}m
              {" "}({data.alert.in_business_hours ? "in business hrs" : "off-hours"})
            </div>
          </div>
        </>
      ) : null}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Per-vertical halt toggle (Task #271)
//
// Sits alongside Vertical visibility on /system → controls. A halt writes
// to system_settings (risk.vertical_halted.<name>), cancels every resting
// Kalshi order attached to the vertical, and is picked up by the risk-gate
// chain on the very next cycle (no restart required). Resume clears the
// row. Both actions audit-log.
// ---------------------------------------------------------------------------

type VerticalHaltsPayload = {
  halts: Record<
    string,
    {
      by_user_id?: string | null;
      by_email?: string | null;
      at?: string | null;
      reason?: string | null;
    }
  >;
};

type HaltCancelSummary = {
  scanned: number;
  matched: number;
  cancelled: number;
  failed: number;
};

export function VerticalHaltsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const qc = useQueryClient();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const overview = useGetVerticalsOverview(
    { days: 7 },
    {
      query: {
        queryKey: getGetVerticalsOverviewQueryKey({ days: 7 }),
        staleTime: 30_000,
      },
    },
  );

  const haltsQuery = useQuery<VerticalHaltsPayload>({
    queryKey: ["admin", "vertical-halts"],
    queryFn: () =>
      apiFetch<VerticalHaltsPayload>("/api/admin/verticals/halts", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  });

  const [lastCancel, setLastCancel] = useState<
    { vertical: string; summary: HaltCancelSummary } | null
  >(null);

  const haltMutation = useMutation({
    mutationFn: async (vertical: string) =>
      apiFetch<{ resting_orders: HaltCancelSummary; changed: boolean }>(
        `/api/admin/verticals/${encodeURIComponent(vertical)}/halt`,
        {
          method: "POST",
          getToken: tokenFn,
          body: JSON.stringify({ reason: null }),
          headers: { "Content-Type": "application/json" },
        },
      ),
    onSuccess: (result, vertical) => {
      setLastCancel({ vertical, summary: result.resting_orders });
      qc.invalidateQueries({ queryKey: ["admin", "vertical-halts"] });
    },
  });

  const resumeMutation = useMutation({
    mutationFn: async (vertical: string) =>
      apiFetch<{ changed: boolean }>(
        `/api/admin/verticals/${encodeURIComponent(vertical)}/resume`,
        { method: "POST", getToken: tokenFn },
      ),
    onSuccess: () => {
      setLastCancel(null);
      qc.invalidateQueries({ queryKey: ["admin", "vertical-halts"] });
    },
  });

  const busyVertical =
    (haltMutation.isPending && (haltMutation.variables as string | undefined)) ||
    (resumeMutation.isPending && (resumeMutation.variables as string | undefined)) ||
    null;

  const halts = haltsQuery.data?.halts ?? {};
  const rows = overview.data?.verticals ?? [];

  return (
    <main data-testid="panel-vertical-halts">
      <p className="text-xs text-muted-foreground mb-3 normal-case tracking-normal">
        Halting a vertical stops the risk-gate chain from approving its
        candidates on the next cycle and immediately cancels every
        resting Kalshi order attached to it. The rest of the portfolio
        keeps trading. Every change writes to <code>admin_audit_log</code>.
      </p>

      {(overview.isLoading || haltsQuery.isLoading) && !haltsQuery.data ? (
        <div className="text-xs text-muted-foreground uppercase tracking-widest">
          Loading…
        </div>
      ) : null}

      <table className="w-full text-xs">
        <thead className="text-muted-foreground uppercase tracking-widest">
          <tr>
            <th className="text-left p-2">Vertical</th>
            <th className="text-left p-2">State</th>
            <th className="text-left p-2">Halted by</th>
            <th className="text-left p-2">Halted at</th>
            <th className="text-left p-2 w-32">Action</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((entry) => {
            const meta = halts[entry.vertical];
            const isHalted = !!meta;
            const busy = busyVertical === entry.vertical;
            return (
              <tr
                key={entry.vertical}
                className="border-t border-border"
                data-testid={`row-vertical-halt-${entry.vertical}`}
              >
                <td className="p-2 uppercase font-bold text-primary">
                  {entry.vertical}
                </td>
                <td className="p-2">
                  <span
                    className={`px-2 py-0.5 border text-[10px] uppercase tracking-widest ${
                      isHalted
                        ? "border-destructive text-destructive"
                        : "border-emerald-500/60 text-emerald-500"
                    }`}
                    data-testid={`state-vertical-halt-${entry.vertical}`}
                  >
                    {isHalted ? "Halted" : "Live"}
                  </span>
                </td>
                <td className="p-2 text-muted-foreground">
                  {meta?.by_email ?? meta?.by_user_id ?? "—"}
                </td>
                <td className="p-2 text-muted-foreground">
                  {meta?.at ? _fmtRelative(meta.at) : "—"}
                </td>
                <td className="p-2">
                  {isHalted ? (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => resumeMutation.mutate(entry.vertical)}
                      className="px-2 py-0.5 border border-emerald-500/60 text-emerald-500 text-[10px] uppercase tracking-widest hover:bg-emerald-500/10 disabled:opacity-50"
                      data-testid={`button-resume-${entry.vertical}`}
                    >
                      {busy ? "…" : "Resume"}
                    </button>
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => haltMutation.mutate(entry.vertical)}
                      className="px-2 py-0.5 border border-destructive text-destructive text-[10px] uppercase tracking-widest hover:bg-destructive/10 disabled:opacity-50"
                      data-testid={`button-halt-${entry.vertical}`}
                    >
                      {busy ? "…" : "Halt"}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {lastCancel ? (
        <div
          className="mt-3 text-[11px] text-muted-foreground"
          data-testid="text-vertical-halt-cancel-summary"
        >
          Halted <span className="text-primary">{lastCancel.vertical}</span> —
          cancelled {lastCancel.summary.cancelled}/{lastCancel.summary.matched}{" "}
          resting orders
          {lastCancel.summary.failed > 0
            ? ` (${lastCancel.summary.failed} cancel failures)`
            : ""}
          .
        </div>
      ) : null}

      {haltMutation.isError || resumeMutation.isError ? (
        <div className="mt-3 text-xs text-destructive">
          {(
            (haltMutation.error as Error) ??
            (resumeMutation.error as Error)
          )?.message ?? "Toggle failed."}
        </div>
      ) : null}
    </main>
  );
}
