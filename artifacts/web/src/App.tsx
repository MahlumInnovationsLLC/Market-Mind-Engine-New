import { useEffect, useRef, useState } from "react";
import {
  ClerkProvider,
  SignIn,
  SignUp,
  SignInButton,
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
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import NotFound from "@/pages/not-found";
import Dashboard from "@/pages/dashboard";
import PipelinePage from "@/pages/pipeline";
import PendingApproval from "@/pages/pending-approval";
import PricingPage from "@/pages/pricing";
import BillingPage from "@/pages/billing";
import HealthPage from "@/pages/health";
import MarketingPage from "@/pages/marketing";
import SessionExpiredOverlay from "@/components/session-expired-overlay";

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
  // To update login providers, app branding, or OAuth settings use the Auth
  // pane in the workspace toolbar. More information can be found in the Replit docs.
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
  // To update login providers, app branding, or OAuth settings use the Auth
  // pane in the workspace toolbar. More information can be found in the Replit docs.
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
  // Spec: signed-out visitors get the marketing page; authenticated
  // users land on the operator console (`/app`). Anyone who explicitly
  // wants the marketing copy can still reach it via a logged-out
  // browser or by signing out.
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

function AppRoute() {
  return (
    <>
      <Show when="signed-in">
        <ApprovalGate />
      </Show>
      <Show when="signed-out">
        <Redirect to="/" />
      </Show>
    </>
  );
}

function ApprovalGate() {
  // Operator allow-list check. Resolved server-side from
  // `OPERATOR_ALLOWED_EMAILS` env var or Clerk public_metadata.approved.
  // Unapproved users see PendingApproval; the API also enforces 403.
  const { data, isLoading, isError, error } = useGetMyAccess({
    query: {
      queryKey: getGetMyAccessQueryKey(),
      retry: (failureCount, err: unknown) => {
        // 403 is a definite "not approved" — don't retry. 401 can happen
        // transiently if the Clerk token bridge hasn't attached yet, so
        // give it a couple of retries before showing an error.
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

  return <Dashboard />;
}

function ProtectedPipeline() {
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
  if (!data?.approved) return <PendingApproval email={data?.email ?? null} />;
  return <PipelinePage />;
}

function PipelineRoute() {
  return (
    <>
      <Show when="signed-in">
        <ProtectedPipeline />
      </Show>
      <Show when="signed-out">
        <Redirect to="/" />
      </Show>
    </>
  );
}


/**
 * Bridges Clerk's session token into the generated API client.  Every
 * outgoing request from `@workspace/api-client-react` will include the
 * current Clerk JWT as `Authorization: Bearer <token>`.
 */
function ClerkApiTokenBridge() {
  const { isSignedIn, getToken } = useAuth();
  useEffect(() => {
    if (isSignedIn) {
      setAuthTokenGetter(async () => (await getToken()) ?? null);
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
      // Drop every cached telemetry response so a re-authenticated user
      // never sees data from the previous session.
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
            <Route path="/app" component={AppRoute} />
            <Route path="/pipeline" component={PipelineRoute} />
            <Route path="/pricing" component={PricingPage} />
            <Route path="/billing" component={BillingPage} />
            <Route path="/health" component={HealthPage} />
            <Route path="/sign-in/*?" component={SignInPage} />
            <Route path="/sign-up/*?" component={SignUpPage} />
            <Route component={NotFound} />
          </Switch>
          {sessionExpired && (
            <SessionExpiredOverlay
              onDismiss={() => setSessionExpired(false)}
            />
          )}
          <Toaster />
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
