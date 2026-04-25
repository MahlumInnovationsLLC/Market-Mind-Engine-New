import { useMemo, useState } from "react";
import { useAuth } from "@clerk/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch, formatErrorMessage } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

type TelegramStatus = {
  linked: boolean;
  telegram_user_id?: number | null;
  telegram_username?: string | null;
  bound_at?: string | null;
  last_seen_at?: string | null;
  pending_link_code?: string | null;
  pending_link_expires_at?: string | null;
  notify_push?: boolean;
  bot_username?: string | null;
  bot_running?: boolean;
};

type TelegramLinkResponse = TelegramStatus & {
  code?: string | null;
  expires_at?: string | null;
};

const TELEGRAM_QUERY_KEY = ["dashboard", "telegram"] as const;

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function fmtRemaining(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const exp = new Date(iso).getTime();
  if (Number.isNaN(exp)) return null;
  const ms = exp - Date.now();
  if (ms <= 0) return "expired";
  const mins = Math.floor(ms / 60_000);
  const secs = Math.floor((ms % 60_000) / 1000);
  if (mins <= 0) return `${secs}s left`;
  return `${mins}m ${secs}s left`;
}

export default function TelegramConcierge() {
  const { getToken, isSignedIn } = useAuth();
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);
  const [issuedCode, setIssuedCode] = useState<string | null>(null);
  const [issuedExpiresAt, setIssuedExpiresAt] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const tokenGetter = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const statusQuery = useQuery<TelegramStatus>({
    queryKey: TELEGRAM_QUERY_KEY,
    queryFn: () =>
      apiFetch<TelegramStatus>("/api/me/telegram", { getToken: tokenGetter }),
    enabled: !!isSignedIn,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const linkMutation = useMutation<TelegramLinkResponse, unknown, void>({
    mutationFn: () =>
      apiFetch<TelegramLinkResponse>("/api/me/telegram/link", {
        method: "POST",
        body: {},
        getToken: tokenGetter,
      }),
    onSuccess: (data) => {
      setActionError(null);
      setCopied(false);
      const code =
        (typeof data.code === "string" && data.code) ||
        data.pending_link_code ||
        null;
      const expires =
        (typeof data.expires_at === "string" && data.expires_at) ||
        data.pending_link_expires_at ||
        null;
      setIssuedCode(code);
      setIssuedExpiresAt(expires);
      void queryClient.invalidateQueries({ queryKey: TELEGRAM_QUERY_KEY });
    },
    onError: (err) => {
      setActionError(
        formatErrorMessage(
          err,
          err instanceof ApiError
            ? `Generate code failed (HTTP ${err.status})`
            : undefined,
        ),
      );
    },
  });

  const unlinkMutation = useMutation<{ ok: boolean; removed: boolean }, unknown, void>({
    mutationFn: () =>
      apiFetch<{ ok: boolean; removed: boolean }>("/api/me/telegram", {
        method: "DELETE",
        getToken: tokenGetter,
      }),
    onSuccess: () => {
      setActionError(null);
      setIssuedCode(null);
      setIssuedExpiresAt(null);
      setCopied(false);
      void queryClient.invalidateQueries({ queryKey: TELEGRAM_QUERY_KEY });
    },
    onError: (err) => {
      setActionError(
        formatErrorMessage(
          err,
          err instanceof ApiError
            ? `Unlink failed (HTTP ${err.status})`
            : undefined,
        ),
      );
    },
  });

  // Mirrors the bot's /mute and /unmute commands — flips the per-binding
  // notify_push flag so operators can pause proactive halt / emergency-stop
  // pings without leaving the dashboard. Optimistically updates the cached
  // status so the toggle doesn't snap back while the request is in flight.
  const notifyPushMutation = useMutation<TelegramStatus, unknown, boolean>({
    mutationFn: (enabled: boolean) =>
      apiFetch<TelegramStatus>("/api/me/telegram/notify-push", {
        method: "POST",
        body: { enabled },
        getToken: tokenGetter,
      }),
    onMutate: async (enabled) => {
      setActionError(null);
      await queryClient.cancelQueries({ queryKey: TELEGRAM_QUERY_KEY });
      const previous = queryClient.getQueryData<TelegramStatus>(TELEGRAM_QUERY_KEY);
      if (previous) {
        queryClient.setQueryData<TelegramStatus>(TELEGRAM_QUERY_KEY, {
          ...previous,
          notify_push: enabled,
        });
      }
      return { previous };
    },
    onError: (err, _enabled, ctx) => {
      const previous = (ctx as { previous?: TelegramStatus } | undefined)?.previous;
      if (previous) {
        queryClient.setQueryData<TelegramStatus>(TELEGRAM_QUERY_KEY, previous);
      }
      setActionError(
        formatErrorMessage(
          err,
          err instanceof ApiError
            ? `Update failed (HTTP ${err.status})`
            : undefined,
        ),
      );
    },
    onSuccess: (next) => {
      queryClient.setQueryData<TelegramStatus>(TELEGRAM_QUERY_KEY, next);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: TELEGRAM_QUERY_KEY });
    },
  });

  const data = statusQuery.data;
  const botUsername = data?.bot_username ?? null;
  const botRunning = data?.bot_running !== false;

  const pendingCode =
    issuedCode ?? (!data?.linked ? data?.pending_link_code ?? null : null);
  const pendingExpiresAt =
    issuedExpiresAt ??
    (!data?.linked ? data?.pending_link_expires_at ?? null : null);
  const remaining = fmtRemaining(pendingExpiresAt);
  const deepLink =
    pendingCode && botUsername
      ? `https://t.me/${botUsername}?start=${encodeURIComponent(pendingCode)}`
      : null;

  const copyCode = async () => {
    if (!pendingCode) return;
    try {
      await navigator.clipboard.writeText(pendingCode);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can fail in some contexts; ignore silently.
    }
  };

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-focus-id="telegram-concierge"
      data-testid="card-telegram-concierge"
    >
      <SectionHeader
        eyebrow="CONCIERGE"
        title="Telegram concierge"
        subtitle="Bind your Telegram account to receive trade alerts and issue /estop from chat."
        actions={
          botUsername ? (
            <span
              className={`border px-2 py-0.5 text-[10px] uppercase tracking-widest ${
                botRunning
                  ? "border-success/60 bg-success/5 text-success"
                  : "border-destructive/60 bg-destructive/5 text-destructive"
              }`}
              data-testid="badge-telegram-bot-status"
            >
              @{botUsername} {botRunning ? "online" : "offline"}
            </span>
          ) : null
        }
      />

      {statusQuery.isLoading && !data ? (
        <div className="text-xs text-muted-foreground" data-testid="text-telegram-loading">
          Loading…
        </div>
      ) : statusQuery.isError ? (
        <div className="text-xs text-destructive" data-testid="text-telegram-error">
          {formatErrorMessage(statusQuery.error, "Failed to load Telegram status.")}
        </div>
      ) : data?.linked ? (
        <div className="space-y-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="border border-success/60 bg-success/5 px-2 py-0.5 text-[10px] uppercase tracking-widest text-success"
              data-testid="badge-telegram-status"
            >
              Linked
            </span>
            {data.telegram_username ? (
              <span
                className="text-foreground"
                data-testid="text-telegram-username"
              >
                @{data.telegram_username}
              </span>
            ) : data.telegram_user_id != null ? (
              <span className="text-foreground" data-testid="text-telegram-user-id">
                tg id {data.telegram_user_id}
              </span>
            ) : null}
          </div>
          <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <div>
              <dt className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Bound at
              </dt>
              <dd className="text-foreground" data-testid="text-telegram-bound-at">
                {fmtDateTime(data.bound_at)}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Last seen
              </dt>
              <dd className="text-foreground" data-testid="text-telegram-last-seen">
                {fmtDateTime(data.last_seen_at)}
              </dd>
            </div>
          </dl>
          <label
            className="flex items-start justify-between gap-3 border-t border-border pt-3"
            data-testid="row-telegram-notify-push"
          >
            <span className="flex flex-col">
              <span className="text-[11px] font-bold uppercase tracking-widest text-foreground">
                Receive proactive halt pings
              </span>
              <span className="text-[11px] text-muted-foreground">
                Mirrors the bot's /mute and /unmute commands. Turn off to
                pause halt and emergency-stop notifications in this chat.
              </span>
            </span>
            <input
              type="checkbox"
              role="switch"
              checked={data.notify_push !== false}
              disabled={notifyPushMutation.isPending}
              onChange={(e) => notifyPushMutation.mutate(e.target.checked)}
              className="mt-0.5 h-4 w-4 cursor-pointer accent-primary disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="switch-telegram-notify-push"
              aria-label="Receive proactive halt pings"
            />
          </label>
          <div className="flex flex-wrap gap-2 border-t border-border pt-3">
            <button
              type="button"
              onClick={() => unlinkMutation.mutate()}
              disabled={unlinkMutation.isPending}
              className="border border-destructive bg-destructive/5 px-3 py-2 text-[11px] font-bold uppercase tracking-widest text-destructive hover:bg-destructive/10 disabled:opacity-50"
              data-testid="button-telegram-unlink"
            >
              {unlinkMutation.isPending ? "Unlinking…" : "Unlink"}
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground"
              data-testid="badge-telegram-status"
            >
              Not linked
            </span>
            {botUsername ? (
              <span className="text-muted-foreground">
                Bot: @{botUsername}
              </span>
            ) : (
              <span className="text-muted-foreground">
                Bot username not configured.
              </span>
            )}
          </div>

          {pendingCode ? (
            <div
              className="space-y-2 border border-accent-cyan/50 bg-accent-cyan/5 p-3"
              data-testid="block-telegram-pending-code"
            >
              <div className="text-[10px] uppercase tracking-widest text-accent-cyan">
                Link code
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <code
                  className="select-all bg-background/60 px-2 py-1 font-mono text-base font-bold tracking-widest text-foreground"
                  data-testid="text-telegram-link-code"
                >
                  {pendingCode}
                </code>
                <button
                  type="button"
                  onClick={copyCode}
                  className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
                  data-testid="button-telegram-copy-code"
                >
                  {copied ? "Copied" : "Copy"}
                </button>
                {remaining ? (
                  <span
                    className="text-[10px] uppercase tracking-widest text-muted-foreground"
                    data-testid="text-telegram-code-expires"
                  >
                    {remaining}
                  </span>
                ) : null}
              </div>
              {deepLink ? (
                <a
                  href={deepLink}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-block bg-primary px-3 py-2 text-[11px] font-bold uppercase tracking-widest text-primary-foreground hover:opacity-90"
                  data-testid="link-telegram-deep-link"
                >
                  Open in Telegram →
                </a>
              ) : (
                <p className="text-[11px] text-muted-foreground">
                  Send <code>/start {pendingCode}</code> to the concierge bot
                  to finish linking.
                </p>
              )}
            </div>
          ) : null}

          <div className="flex flex-wrap gap-2 border-t border-border pt-3">
            <button
              type="button"
              onClick={() => linkMutation.mutate()}
              disabled={linkMutation.isPending}
              className="bg-primary px-3 py-2 text-[11px] font-bold uppercase tracking-widest text-primary-foreground hover:opacity-90 disabled:opacity-50"
              data-testid="button-telegram-generate-code"
            >
              {linkMutation.isPending
                ? "Generating…"
                : pendingCode
                  ? "Regenerate code"
                  : "Generate link code"}
            </button>
          </div>
        </div>
      )}

      {actionError ? (
        <div
          className="mt-3 border border-destructive p-2 text-xs text-destructive"
          data-testid="text-telegram-action-error"
        >
          {actionError}
        </div>
      ) : null}
    </GlassCard>
  );
}
