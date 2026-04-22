import { useEffect, useRef, useState } from "react";
import {
  ClerkProvider,
  SignIn,
  SignUp,
  Show,
  useAuth,
  useClerk,
} from "@clerk/react";
import { shadcn } from "@clerk/themes";
import {
  Switch,
  Route,
  Redirect,
  Router as WouterRouter,
  useLocation,
} from "wouter";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import {
  getGetMyAccessQueryKey,
  setAuthTokenGetter,
  setUnauthorizedHandler,
  useGetMyAccess,
} from "@workspace/api-client-react";
import { TooltipProvider } from "@/components/ui/tooltip";
import NotFound from "@/pages/not-found";
import Dashboard from "@/pages/dashboard";
import PipelinePage from "@/pages/pipeline";
import PendingApproval from "@/pages/pending-approval";
import PricingPage from "@/pages/pricing";
import BillingPage from "@/pages/billing";
import HealthPage from "@/pages/health";
import MarketingPage from "@/pages/marketing";
import AdminPage from "@/pages/admin";
import VerticalsPage from "@/pages/verticals";
import SettingsNotificationsPage from "@/pages/settings-notifications";
import TradeDetailPage from "@/pages/trade-detail";
import TradingPage from "@/pages/trading";
import PositionsPage from "@/pages/positions";
import IntelligencePage from "@/pages/intelligence";
import SystemPage from "@/pages/system";
import ProfilePage from "@/pages/profile";
import SessionExpiredOverlay from "@/components/session-expired-overlay";
import GlobalSearch from "@/components/global-search";
import { AppShell } from "@/components/shell/app-shell";

const clerkPubKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY;
// Empty in dev, auto-set in prod by the platform.
const clerkProxyUrl = import.meta.env.VITE_CLERK_PROXY_URL;

const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");

if (!clerkPubKey) {
  throw new Error("Missing VITE_CLERK_PUBLISHABLE_KEY in env");
}

function stripBase(path: string): string {
  return basePath && path.startsWith(basePath)
    ? path.slice(basePath.length) || "/"
    : path;
}

const queryClient = new QueryClient();

const clerkAppearance = {
  theme: shadcn,
  cssLayerName: "clerk",
  options: {
    logoPlacement: "inside" as const,
    logoLinkUrl: basePath || "/",
    logoImageUrl:
      typeof window !== "undefined"
        ? `${window.location.origin}${basePath}/logo.svg`
        : undefined,
    socialButtonsPlacement: "top" as const,
  },
  variables: {
    colorPrimary: "hsl(189, 94%, 55%)",
    colorForeground: "hsl(210, 40%, 98%)",
    colorMutedForeground: "hsl(215, 20%, 70%)",
    colorDanger: "hsl(0, 84%, 60%)",
    colorBackground: "hsl(222, 35%, 7%)",
    colorInput: "hsl(220, 25%, 12%)",
    colorInputForeground: "hsl(210, 40%, 98%)",
    colorNeutral: "hsl(220, 25%, 18%)",
    colorModalBackdrop: "rgba(0, 0, 0, 0.85)",
    fontFamily: "'Inter', system-ui, -apple-system, sans-serif",
    borderRadius: "0.75rem",
  },
  elements: {
    rootBox: "w-full",
    cardBox:
      "bg-card border border-border rounded-2xl w-[440px] max-w-full overflow-hidden",
    card: "!shadow-none !border-0 !bg-transparent !rounded-2xl",
    footer: "!shadow-none !border-0 !bg-transparent !rounded-2xl",
    headerTitle: "text-foreground font-semibold tracking-tight",
    headerSubtitle: "text-muted-foreground text-sm",
    socialButtonsBlockButton:
      "border border-border bg-card hover:bg-muted text-foreground rounded-xl",
    socialButtonsBlockButtonText: "text-foreground font-medium",
    formFieldLabel: "text-muted-foreground text-sm font-medium",
    formFieldInput:
      "bg-input border border-border text-foreground rounded-xl",
    formButtonPrimary:
      "bg-primary text-primary-foreground hover:opacity-90 rounded-full font-semibold",
    footerAction: "text-sm",
    footerActionLink: "text-primary hover:text-primary/80",
    footerActionText: "text-muted-foreground",
    dividerLine: "bg-border",
    dividerText: "text-muted-foreground text-xs",
    identityPreviewEditButton: "text-primary",
    formFieldSuccessText: "text-success",
    alert: "border border-destructive text-destructive",
    alertText: "text-destructive",
    otpCodeFieldInput: "bg-input border border-border text-foreground",
    formFieldRow: "space-y-1",
    main: "space-y-4",
    logoBox: "justify-center mb-2",
    logoImage: "h-10 w-10",
  },
};

function SignInPage() {
  return (
    <div className="flex min-h-[100dvh] items-center justify-center bg-background px-4 font-mono">
      <SignIn
        routing="path"
        path={`${basePath}/sign-in`}
        signUpUrl={`${basePath}/sign-up`}
        fallbackRedirectUrl={`${basePath}/app`}
      />
    </div>
  );
}

function SignUpPage() {
  return (
    <div className="flex min-h-[100dvh] items-center justify-center bg-background px-4 font-mono">
      <SignUp
        routing="path"
        path={`${basePath}/sign-up`}
        signInUrl={`${basePath}/sign-in`}
        fallbackRedirectUrl={`${basePath}/app`}
      />
    </div>
  );
}

function HomeRoute() {
  return (
    <>
      <Show when="signed-in">
        <Redirect to="/app" />
      </Show>
      <Show when="signed-out">
        <MarketingPage />
      </Show>
    </>
  );
}

function AdminForbidden({ email }: { email: string | null }) {
  return (
    <div className="min-h-[100dvh] bg-background text-foreground font-mono flex items-center justify-center p-6">
      <div className="max-w-md text-center space-y-4 border border-destructive/40 bg-destructive/5 rounded-md p-8">
        <div className="text-destructive text-xs uppercase tracking-widest">
          HTTP 403 :: Forbidden
        </div>
        <div className="text-lg font-semibold">Admin access required</div>
        <p className="text-sm text-muted-foreground">
          Your account{email ? ` (${email})` : ""} is approved as an operator
          but is not an admin. Ask an existing admin to promote you from the
          admin console.
        </p>
        <a
          href="/"
          className="inline-block text-xs uppercase tracking-widest text-primary underline-offset-4 hover:underline"
        >
          ← back to dashboard
        </a>
      </div>
    </div>
  );
}

/**
 * Authenticated shell + approval gate consolidated into one component
 * so the access query is fired once per session and the chrome (sidebar,
 * header, banners) is mounted exactly once for the whole signed-in
 * surface area. Wouter unmounts route children on navigation, but the
 * shell itself persists so sidebar collapse state is preserved.
 */
function AuthedShell() {
  const { data, isLoading, isError, error } = useGetMyAccess({
    query: {
      queryKey: getGetMyAccessQueryKey(),
      retry: (failureCount, err: unknown) => {
        const status = (err as { status?: number } | null)?.status;
        if (status === 403) return false;
        return failureCount < 3;
      },
      staleTime: 30_000,
    },
  });

  if (isLoading) {
    return (
      <div className="min-h-[100dvh] bg-background text-muted-foreground font-mono flex items-center justify-center text-xs uppercase tracking-widest">
        AUTH :: verifying access…
      </div>
    );
  }

  if (isError) {
    const status = (error as { status?: number } | null)?.status;
    if (status === 403) return <PendingApproval />;
    return (
      <div className="min-h-[100dvh] bg-background text-destructive font-mono flex items-center justify-center text-xs uppercase tracking-widest p-6 text-center">
        AUTH :: unable to verify access (HTTP {status ?? "?"})
      </div>
    );
  }

  if (!data?.approved) {
    return <PendingApproval email={data?.email ?? null} />;
  }

  const isAdmin = !!data.is_admin;
  const operatorEmail = data.email ?? null;

  return (
    <AppShell isAdmin={isAdmin} operatorEmail={operatorEmail}>
      <Switch>
        <Route path="/app" component={Dashboard} />
        <Route path="/pipeline" component={PipelinePage} />
        <Route path="/verticals" component={VerticalsPage} />
        <Route path="/trading" component={TradingPage} />
        <Route path="/positions" component={PositionsPage} />
        <Route path="/intelligence" component={IntelligencePage} />
        <Route path="/system" component={SystemPage} />
        <Route path="/profile" component={ProfilePage} />
        <Route path="/trade/:ref" component={TradeDetailPage} />
        <Route path="/admin">
          {isAdmin ? <AdminPage /> : <AdminForbidden email={operatorEmail} />}
        </Route>
        <Route path="/billing/:rest*">
          <Redirect to="/profile" />
        </Route>
        <Route component={NotFound} />
      </Switch>
    </AppShell>
  );
}

function AuthedShellRoute() {
  // Catch-all for any path not matched by the public-route Switch above.
  // Signed-in users get the shell (which has its own inner NotFound for
  // unknown sub-paths); signed-out users get the public NotFound page so
  // unknown URLs don't silently bounce to `/`.
  return (
    <>
      <Show when="signed-in">
        <AuthedShell />
      </Show>
      <Show when="signed-out">
        <NotFound />
      </Show>
    </>
  );
}

function ClerkApiTokenBridge() {
  const { isSignedIn, getToken } = useAuth();
  useEffect(() => {
    if (isSignedIn) {
      setAuthTokenGetter(async (opts) => {
        const token = await getToken(
          opts?.forceRefresh ? { skipCache: true } : undefined,
        );
        return token ?? null;
      });
    } else {
      setAuthTokenGetter(null);
    }
    return () => {
      setAuthTokenGetter(null);
    };
  }, [isSignedIn, getToken]);
  return null;
}

function ClerkQueryClientCacheInvalidator() {
  const { addListener } = useClerk();
  const queryClient = useQueryClient();
  const prevUserIdRef = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    const unsubscribe = addListener(({ user }) => {
      const userId = user?.id ?? null;
      if (
        prevUserIdRef.current !== undefined &&
        prevUserIdRef.current !== userId
      ) {
        queryClient.clear();
      }
      prevUserIdRef.current = userId;
    });
    return unsubscribe;
  }, [addListener, queryClient]);

  return null;
}

function SessionExpiredBridge({
  onExpired,
}: {
  onExpired: () => void;
}) {
  const queryClient = useQueryClient();
  const onExpiredRef = useRef(onExpired);

  useEffect(() => {
    onExpiredRef.current = onExpired;
  }, [onExpired]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      queryClient.cancelQueries();
      queryClient.clear();
      onExpiredRef.current();
    });
    return () => {
      setUnauthorizedHandler(null);
    };
  }, [queryClient]);

  return null;
}

function ClerkProviderWithRoutes() {
  const [, setLocation] = useLocation();
  const [sessionExpired, setSessionExpired] = useState(false);

  return (
    <ClerkProvider
      publishableKey={clerkPubKey}
      proxyUrl={clerkProxyUrl}
      appearance={clerkAppearance}
      localization={{
        signIn: {
          start: {
            title: "Operator sign-in",
            subtitle: "Authenticate to access live telemetry",
          },
        },
        signUp: {
          start: {
            title: "Request operator access",
            subtitle: "Create an account",
          },
        },
      }}
      routerPush={(to) => setLocation(stripBase(to))}
      routerReplace={(to) => setLocation(stripBase(to), { replace: true })}
    >
      <QueryClientProvider client={queryClient}>
        <ClerkApiTokenBridge />
        <ClerkQueryClientCacheInvalidator />
        <SessionExpiredBridge onExpired={() => setSessionExpired(true)} />
        <TooltipProvider>
          <Switch>
            <Route path="/" component={HomeRoute} />
            <Route path="/pricing" component={PricingPage} />
            <Route path="/billing">
              {/* /billing now lives inside the authed /profile page.
                  Signed-in visitors are redirected straight to /profile;
                  signed-out visitors are sent through the Clerk sign-in
                  page (with /profile as the post-login destination) so
                  they don't fall through to the public NotFound. */}
              <Show when="signed-in">
                <Redirect to="/profile" />
              </Show>
              <Show when="signed-out">
                <Redirect to="/sign-in?redirect_url=/profile" />
              </Show>
            </Route>
            <Route path="/health" component={HealthPage} />
            <Route path="/settings/notifications" component={SettingsNotificationsPage} />
            <Route path="/sign-in/:rest*" component={SignInPage} />
            <Route path="/sign-up/:rest*" component={SignUpPage} />
            {/* Catch-all: every other path goes through the
                authenticated shell, which itself handles
                approval-gating and 404s. */}
            <Route component={AuthedShellRoute} />
          </Switch>
          <Show when="signed-in">
            <GlobalSearch />
          </Show>
          {sessionExpired && (
            <SessionExpiredOverlay
              onDismiss={() => setSessionExpired(false)}
            />
          )}
        </TooltipProvider>
      </QueryClientProvider>
    </ClerkProvider>
  );
}

function App() {
  return (
    <WouterRouter base={basePath}>
      <ClerkProviderWithRoutes />
    </WouterRouter>
  );
}

export default App;
