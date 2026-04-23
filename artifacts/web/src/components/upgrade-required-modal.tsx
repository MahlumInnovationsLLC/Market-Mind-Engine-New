import { Link } from "wouter";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/**
 * Free-tier 402 upsell modal.
 *
 * Surfaced on the trade-detail page (and any other endpoint that may
 * return a `402 upgrade_required` payload) when a free user has burned
 * their daily distinct-market quota. Backend payload shape:
 *
 *   {
 *     "error": "upgrade_required",
 *     "reason": "free_tier_daily_market_limit",
 *     "limit": 5,
 *     "viewed_today": 5,
 *     "market_id": "...",
 *     "resets_at": "00:00 UTC"
 *   }
 *
 * The modal is intentionally non-dismissible-into-the-content: we don't
 * have anything to render behind it (the backend refused to serve the
 * detail), so closing it just routes the operator back to the dashboard.
 */
export type UpgradeRequiredPayload = {
  error?: string;
  reason?: string;
  limit?: number;
  viewed_today?: number;
  market_id?: string | null;
  resets_at?: string;
};

interface UpgradeRequiredModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  payload?: UpgradeRequiredPayload | null;
}

export default function UpgradeRequiredModal({
  open,
  onOpenChange,
  payload,
}: UpgradeRequiredModalProps) {
  const limit = payload?.limit ?? 5;
  const viewed = payload?.viewed_today ?? limit;
  const resets = payload?.resets_at ?? "00:00 UTC";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="font-mono"
        data-testid="modal-upgrade-required"
      >
        <DialogHeader>
          <div className="text-[10px] uppercase tracking-widest text-primary">
            UPGRADE REQUIRED
          </div>
          <DialogTitle className="text-lg font-bold tracking-tight text-foreground">
            You've hit today's free-tier limit
          </DialogTitle>
          <DialogDescription className="text-xs text-muted-foreground">
            Free accounts can evaluate {limit} distinct markets per UTC day.
            You've opened {viewed} today — the counter resets at {resets}.
          </DialogDescription>
        </DialogHeader>
        <div
          className="border border-primary/40 bg-primary/5 p-3 text-xs text-foreground/90"
          data-testid="text-upgrade-explainer"
        >
          Upgrade to Pro for unlimited market evaluations, live signals
          (no 15-minute delay), and persona-level swarm reasoning.
        </div>
        <DialogFooter className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="border border-border px-3 py-2 text-[10px] uppercase tracking-widest text-muted-foreground hover:border-foreground hover:text-foreground"
            data-testid="button-upgrade-dismiss"
          >
            Maybe later
          </button>
          <Link
            href="/pricing"
            onClick={() => onOpenChange(false)}
            className="border border-primary bg-primary px-3 py-2 text-center text-[10px] font-bold uppercase tracking-widest text-primary-foreground hover:opacity-90"
            data-testid="link-upgrade-pricing"
          >
            Upgrade to Pro →
          </Link>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
