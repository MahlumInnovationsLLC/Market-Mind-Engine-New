import { useState } from "react";
import { useClerk } from "@clerk/react";
import { formatErrorMessage } from "@/lib/api";

interface SessionExpiredOverlayProps {
  onDismiss: () => void;
}

export default function SessionExpiredOverlay({
  onDismiss,
}: SessionExpiredOverlayProps) {
  const { signOut } = useClerk();
  const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);

  const handleSignInAgain = async () => {
    setSignOutError(null);
    setSigningOut(true);
    try {
      await signOut({ redirectUrl: `${basePath}/sign-in` });
      onDismiss();
    } catch (err) {
      // Keep the overlay visible so the user is not stranded with a
      // half-broken dashboard if the sign-out request itself fails.
      setSignOutError(
        formatErrorMessage(
          err,
          err instanceof Error
            ? undefined
            : "Could not sign out. Please try again.",
        ),
      );
    } finally {
      setSigningOut(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/95 backdrop-blur-sm font-mono p-4"
      data-testid="overlay-session-expired"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="session-expired-title"
    >
      <div className="max-w-md w-full border border-destructive bg-card p-8 space-y-6">
        <div className="space-y-2">
          <div className="text-xs text-destructive uppercase tracking-widest">
            AUTH_STATUS :: expired
          </div>
          <h2
            id="session-expired-title"
            className="text-xl font-bold uppercase tracking-tight text-foreground"
            data-testid="text-session-expired-title"
          >
            Session expired
          </h2>
          <p className="text-sm text-muted-foreground">
            Your operator session has timed out. Sign in again to resume live
            telemetry.
          </p>
        </div>
        <button
          type="button"
          onClick={handleSignInAgain}
          disabled={signingOut}
          className="w-full bg-primary text-primary-foreground py-3 px-4 uppercase tracking-wider text-sm font-bold hover:opacity-90 transition-opacity disabled:opacity-60 disabled:cursor-not-allowed"
          data-testid="button-session-expired-sign-in"
        >
          {signingOut ? "Signing out…" : "Sign in again"}
        </button>
        {signOutError && (
          <div
            className="text-xs text-destructive uppercase tracking-widest border border-destructive p-2"
            data-testid="text-session-expired-error"
          >
            {signOutError}
          </div>
        )}
        <div className="text-[10px] text-muted-foreground uppercase tracking-widest border-t border-border pt-4">
          SYS_STATUS :: standby
        </div>
      </div>
    </div>
  );
}
