import { lazy, Suspense } from "react";
import {
  Switch,
  Route,
  Redirect,
  Router as WouterRouter,
} from "wouter";
import { hasClerkSession } from "@/lib/clerk-session";
import { Spinner } from "@/components/ui/spinner";
import { ErrorBoundary } from "@/components/error-boundary";

// MarketingPage is the only thing first-time signed-out visitors see,
// so we code-split it into its own chunk and serve it OUTSIDE the
// ClerkProvider tree below. This lets the hero / methodology /
// pricing copy paint before the Clerk SDK is ever fetched and parsed.
const MarketingPage = lazy(() => import("@/pages/marketing"));

// The full authenticated app — ClerkProvider, the React Query client,
// the AppShell, and every signed-in route — lives in its own lazy
// chunk so it doesn't block first paint of the marketing page.
const ClerkApp = lazy(() => import("@/ClerkApp"));

const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");

function MarketingFallback() {
  // Visible centered spinner shown while a lazy chunk (the marketing
  // page or the ClerkApp bundle that owns sign-in) is being fetched.
  // Previously this rendered only an `sr-only` "Loading…" string, so
  // a slow Clerk chunk on /sign-in looked like a black screen and was
  // indistinguishable from a crash.
  return (
    <div
      className="min-h-[100dvh] bg-background text-foreground flex flex-col items-center justify-center gap-3 font-mono"
      aria-busy="true"
      aria-live="polite"
    >
      <Spinner className="size-8 text-primary" />
      <span className="text-sm text-muted-foreground">Loading…</span>
    </div>
  );
}

function PublicHomeRoute() {
  // Best-effort cookie check so signed-in visitors are bounced to /app
  // immediately without waiting for the Clerk SDK to load. Signed-out
  // visitors get the marketing page rendered straight from this public
  // route tree (outside ClerkProvider) for the fastest possible paint.
  if (hasClerkSession()) {
    return <Redirect to="/app" />;
  }
  return (
    <Suspense fallback={<MarketingFallback />}>
      <MarketingPage />
    </Suspense>
  );
}

function App() {
  return (
    <WouterRouter base={basePath}>
      <Switch>
        {/* Marketing page renders without ClerkProvider so first paint
            for signed-out visitors doesn't wait on the auth SDK. */}
        <Route path="/" component={PublicHomeRoute} />
        {/* Everything else (sign-in, pricing, leaderboard, the authed
            console, etc.) is gated by Clerk and lives in a lazy chunk. */}
        <Route>
          {/* ErrorBoundary catches both a chunk-load failure and the
              "Missing VITE_CLERK_PUBLISHABLE_KEY in env" throw at the
              top of ClerkApp.tsx, so a misconfigured deploy renders a
              real error card instead of a silent black screen. */}
          <ErrorBoundary label="ClerkApp">
            <Suspense fallback={<MarketingFallback />}>
              <ClerkApp />
            </Suspense>
          </ErrorBoundary>
        </Route>
      </Switch>
    </WouterRouter>
  );
}

export default App;
