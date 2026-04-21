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
  setAuthTokenGetter,
  setUnauthorizedHandler,
} from "@workspace/api-client-react";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import NotFound from "@/pages/not-found";
import Dashboard from "@/pages/dashboard";
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
    colorPrimary: "hsl(188, 86%, 53%)",
    colorForeground: "hsl(0, 0%, 95%)",
    colorMutedForeground: "hsl(240, 5%, 65%)",
    colorDanger: "hsl(0, 84%, 60%)",
    colorBackground: "hsl(240, 10%, 6%)",
    colorInput: "hsl(240, 6%, 12%)",
    colorInputForeground: "hsl(0, 0%, 95%)",
    colorNeutral: "hsl(240, 6%, 20%)",
    colorModalBackdrop: "rgba(0, 0, 0, 0.85)",
    fontFamily: "'JetBrains Mono', Menlo, Monaco, monospace",
    borderRadius: "0px",
  },
  elements: {
    rootBox: "w-full",
    cardBox:
      "bg-card border border-border rounded-none w-[440px] max-w-full overflow-hidden",
    card: "!shadow-none !border-0 !bg-transparent !rounded-none",
    footer: "!shadow-none !border-0 !bg-transparent !rounded-none",
    headerTitle: "text-foreground uppercase tracking-wider",
    headerSubtitle: "text-muted-foreground text-xs uppercase tracking-widest",
    socialButtonsBlockButton:
      "border border-border bg-card hover:bg-muted text-foreground",
    socialButtonsBlockButtonText: "text-foreground font-mono",
    formFieldLabel: "text-muted-foreground text-xs uppercase tracking-wider",
    formFieldInput:
      "bg-input border border-border text-foreground rounded-none",
    formButtonPrimary:
      "bg-primary text-primary-foreground hover:opacity-90 rounded-none uppercase tracking-wider font-bold",
    footerAction: "text-xs",
    footerActionLink: "text-primary hover:text-primary/80",
    footerActionText: "text-muted-foreground",
    dividerLine: "bg-border",
    dividerText: "text-muted-foreground text-xs uppercase tracking-widest",
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
        fallbackRedirectUrl={basePath || "/"}
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
        fallbackRedirectUrl={basePath || "/"}
      />
    </div>
  );
}

function Landing() {
  return (
    <div className="min-h-[100dvh] bg-background text-foreground font-mono flex flex-col items-center justify-center p-6 selection:bg-primary/30">
      <div className="max-w-md w-full border border-border bg-card p-8 space-y-6">
        <div className="space-y-2">
          <div className="text-xs text-muted-foreground uppercase tracking-widest">
            AXIOM_PREDICT
          </div>
          <h1 className="text-2xl font-bold uppercase tracking-tight text-primary">
            Operator Console
          </h1>
          <p className="text-sm text-muted-foreground">
            Restricted access. Sign in to view live trading telemetry, swarm
            debates, and model inventory.
          </p>
        </div>
        <SignInButton mode="modal">
          <button
            type="button"
            className="w-full bg-primary text-primary-foreground py-3 px-4 uppercase tracking-wider text-sm font-bold hover:opacity-90 transition-opacity"
          >
            Sign in
          </button>
        </SignInButton>
        <div className="text-[10px] text-muted-foreground uppercase tracking-widest border-t border-border pt-4">
          SYS_STATUS :: standby
        </div>
      </div>
    </div>
  );
}

function HomeRedirect() {
  return (
    <>
      <Show when="signed-in">
        <ProtectedDashboard />
      </Show>
      <Show when="signed-out">
        <Landing />
      </Show>
    </>
  );
}

function ProtectedDashboard() {
  return (
    <>
      <Show when="signed-in">
        <Dashboard />
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
            <Route path="/" component={HomeRedirect} />
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
