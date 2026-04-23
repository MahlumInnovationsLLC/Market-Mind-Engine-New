import { lazy, Suspense } from "react";
import {
  Switch,
  Route,
  Redirect,
  Router as WouterRouter,
} from "wouter";
import { hasClerkSession } from "@/lib/clerk-session";

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
  // Tiny, dependency-free skeleton that paints instantly while the
  // marketing chunk is fetched. Keeps the screen from flashing blank.
  return (
    <div
      className="min-h-[100dvh] bg-background text-foreground flex items-center justify-center font-mono"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">Loading…</span>
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
          <Suspense fallback={<MarketingFallback />}>
            <ClerkApp />
          </Suspense>
        </Route>
      </Switch>
    </WouterRouter>
  );
}

export default App;
