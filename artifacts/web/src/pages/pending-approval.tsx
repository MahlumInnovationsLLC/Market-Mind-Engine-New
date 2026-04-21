import { useClerk } from "@clerk/react";

interface PendingApprovalProps {
  email?: string | null;
}

export default function PendingApproval({ email }: PendingApprovalProps) {
  const { signOut } = useClerk();

  return (
    <div
      className="min-h-[100dvh] bg-background text-foreground font-mono flex flex-col items-center justify-center p-6 selection:bg-primary/30"
      data-testid="page-pending-approval"
    >
      <div className="max-w-md w-full border border-border bg-card p-8 space-y-6">
        <div className="space-y-2">
          <div className="text-xs text-muted-foreground uppercase tracking-widest">
            AXIOM_PREDICT
          </div>
          <h1 className="text-2xl font-bold uppercase tracking-tight text-primary">
            Awaiting approval
          </h1>
          <p className="text-sm text-muted-foreground">
            Your account is signed in but is not yet on the operator
            allow-list. The operator must approve your access before any live
            telemetry, P&amp;L, fills, or swarm debates become visible.
          </p>
        </div>

        {email ? (
          <div className="border border-border bg-background/40 px-3 py-2 text-xs">
            <div className="text-muted-foreground uppercase tracking-widest">
              Signed in as
            </div>
            <div
              className="text-foreground truncate mt-1"
              data-testid="text-pending-email"
            >
              {email}
            </div>
          </div>
        ) : null}

        <div className="text-[10px] text-muted-foreground uppercase tracking-widest border border-border/60 p-3 leading-relaxed">
          STATUS :: pending_approval
          <br />
          Send the email above to your operator and they will add you to the
          allow-list. This page will refresh once you have access.
        </div>

        <button
          type="button"
          onClick={() => signOut({ redirectUrl: import.meta.env.BASE_URL })}
          className="w-full border border-border px-3 py-2 uppercase tracking-widest text-[11px] text-muted-foreground hover:text-destructive hover:border-destructive transition-colors"
          data-testid="button-pending-sign-out"
        >
          Sign out
        </button>
      </div>
    </div>
  );
}
