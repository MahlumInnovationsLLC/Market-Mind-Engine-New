import { useEffect, useState } from "react";
import { Link, useLocation } from "wouter";
import { hasClerkSession } from "@/lib/clerk-session";

const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");

function useIsSignedIn(): boolean {
  // Cookie-based heuristic so the public nav can render the right CTA
  // before the Clerk SDK chunk has loaded. The server still verifies
  // every authed request via the real session JWT.
  const [signedIn, setSignedIn] = useState<boolean>(() => hasClerkSession());
  useEffect(() => {
    setSignedIn(hasClerkSession());
  }, []);
  return signedIn;
}

export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="brand-mark-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="hsl(189 94% 60%)" />
          <stop offset="100%" stopColor="hsl(258 90% 70%)" />
        </linearGradient>
      </defs>
      <path
        d="M16 4 L28 26 H22 L16 14 L10 26 H4 Z"
        stroke="url(#brand-mark-grad)"
        strokeWidth="1.75"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}

// Slim primary nav contract — three persistent links shared across every
// public page (home / pricing / about / leaderboard).
const LINKS: { label: string; href: string; key: PublicNavActive }[] = [
  { label: "Pricing", href: "/pricing", key: "pricing" },
  { label: "Leaderboard", href: "/leaderboard", key: "leaderboard" },
  { label: "About", href: "/about", key: "about" },
];

export type PublicNavActive = "home" | "pricing" | "about" | "leaderboard";

export interface PublicNavProps {
  /** Highlight the matching link in the nav. */
  active?: PublicNavActive;
}

export function PublicNav({ active }: PublicNavProps) {
  const isSignedIn = useIsSignedIn();
  // Pathname is read solely to keep the nav re-render-aware of route changes.
  useLocation();

  return (
    <nav
      className="sticky top-0 z-30 backdrop-blur-md bg-background/70 border-b border-border/60"
      data-testid="nav-public"
    >
      <div className="max-w-6xl mx-auto px-4 md:px-6 py-3 flex items-center justify-between gap-3">
        <Link
          href="/"
          className="flex items-center gap-2.5 shrink-0"
          data-testid="link-home-logo"
        >
          <BrandMark size={28} />
          <div className="flex flex-col leading-none">
            <span className="text-sm font-bold tracking-wide text-foreground">
              AXIOM
            </span>
            <span className="text-[9px] tracking-[0.25em] text-muted-foreground -mt-0.5">
              PREDICT
            </span>
          </div>
        </Link>

        <div className="flex items-center gap-1 md:gap-1 text-sm">
          {LINKS.map((link) => {
            const isActive = active === link.key;
            const cls = `hidden md:inline px-3 py-2 transition-colors ${
              isActive
                ? "text-primary"
                : "text-muted-foreground hover:text-foreground"
            }`;
            return (
              <Link
                key={link.href}
                href={link.href}
                className={cls}
                data-testid={`link-public-nav-${link.label.toLowerCase()}`}
              >
                {link.label}
              </Link>
            );
          })}
          {isSignedIn ? (
            <Link
              href="/app"
              className="ml-2 inline-flex items-center gap-1.5 rounded-full border border-primary/60 text-primary px-4 py-1.5 text-sm font-medium hover:bg-primary hover:text-primary-foreground transition-colors"
              data-testid="link-open-console"
            >
              Open console
              <span aria-hidden>→</span>
            </Link>
          ) : (
            <>
              <a
                href={`${basePath}/sign-in`}
                className="px-3 py-2 text-muted-foreground hover:text-foreground transition-colors"
                data-testid="button-nav-signin"
              >
                Sign in
              </a>
              <a
                href={`${basePath}/sign-up`}
                className="ml-2 inline-flex items-center gap-1.5 rounded-full border border-primary/60 text-primary px-4 py-1.5 text-sm font-medium hover:bg-primary hover:text-primary-foreground transition-colors"
                data-testid="button-nav-signup"
              >
                Get access
                <span aria-hidden>→</span>
              </a>
            </>
          )}
        </div>
      </div>
    </nav>
  );
}

export function PublicFooter() {
  const isSignedIn = useIsSignedIn();
  return (
    <footer className="border-t border-border/60 py-10">
      <div className="max-w-6xl mx-auto px-4 md:px-6 flex flex-col md:flex-row items-center justify-between gap-4 text-sm text-muted-foreground">
        <div className="flex items-center gap-2">
          <BrandMark size={20} />
          <span>© Axiom Predict — operator-grade prediction-market trading</span>
        </div>
        <div className="flex flex-wrap gap-5">
          <Link
            href="/pricing"
            className="hover:text-primary transition-colors"
            data-testid="link-footer-pricing"
          >
            Pricing
          </Link>
          <Link
            href="/leaderboard"
            className="hover:text-primary transition-colors"
            data-testid="link-footer-leaderboard"
          >
            Leaderboard
          </Link>
          <Link
            href="/about"
            className="hover:text-primary transition-colors"
            data-testid="link-footer-about"
          >
            About
          </Link>
          <Link
            href="/health"
            className="hover:text-primary transition-colors"
            data-testid="link-footer-health"
          >
            Status
          </Link>
          {isSignedIn ? (
            <Link
              href="/app"
              className="hover:text-primary transition-colors"
              data-testid="link-footer-app"
            >
              Console
            </Link>
          ) : (
            <a
              href={`${basePath}/sign-in`}
              className="hover:text-primary transition-colors"
              data-testid="button-footer-signin"
            >
              Sign in
            </a>
          )}
        </div>
      </div>
    </footer>
  );
}
