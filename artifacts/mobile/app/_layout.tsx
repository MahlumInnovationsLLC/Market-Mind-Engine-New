import {
  Inter_400Regular,
  Inter_500Medium,
  Inter_600SemiBold,
  Inter_700Bold,
  useFonts,
} from "@expo-google-fonts/inter";
import { ClerkLoaded, ClerkProvider, useAuth } from "@clerk/expo";
import { tokenCache } from "@clerk/expo/token-cache";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as Notifications from "expo-notifications";
import { Slot, useRouter } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import React, { useEffect, useRef } from "react";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { KeyboardProvider } from "react-native-keyboard-controller";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { ErrorBoundary } from "@/components/ErrorBoundary";
import { setTokenGetter } from "@/lib/api";
import {
  deepLinkFromNotificationData,
  registerPushTokenForSignedInUser,
  unregisterPushTokenForSignedOutUser,
} from "@/lib/push";
import { ensureRevenueCat } from "@/lib/revenuecat";

SplashScreen.preventAutoHideAsync();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1 },
  },
});

const publishableKey = process.env.EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY ?? "";
const proxyUrl = process.env.EXPO_PUBLIC_CLERK_PROXY_URL || undefined;

function ClerkBridges() {
  const { isSignedIn, getToken, userId } = useAuth();
  const router = useRouter();
  const responseSubRef = useRef<Notifications.EventSubscription | null>(null);

  useEffect(() => {
    setTokenGetter(async () => (await getToken()) ?? null);
    ensureRevenueCat(userId ?? null).catch(() => undefined);
    if (isSignedIn) {
      registerPushTokenForSignedInUser().catch(() => undefined);
    } else {
      unregisterPushTokenForSignedOutUser().catch(() => undefined);
    }
    return () => setTokenGetter(null);
  }, [isSignedIn, getToken, userId]);

  useEffect(() => {
    responseSubRef.current?.remove();
    const sub = Notifications.addNotificationResponseReceivedListener(
      (response) => {
        try {
          const data = response.notification.request.content
            .data as Record<string, unknown> | undefined;
          const link = deepLinkFromNotificationData(data ?? null);
          if (link) router.push(link as never);
        } catch {
          /* ignore */
        }
      },
    );
    responseSubRef.current = sub;
    return () => sub.remove();
  }, [router]);

  return null;
}

export default function RootLayout() {
  const [fontsLoaded, fontError] = useFonts({
    Inter_400Regular,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_700Bold,
  });

  useEffect(() => {
    if (fontsLoaded || fontError) {
      SplashScreen.hideAsync();
    }
  }, [fontsLoaded, fontError]);

  if (!fontsLoaded && !fontError) return null;

  if (!publishableKey) {
    return null;
  }

  return (
    <SafeAreaProvider>
      <ErrorBoundary>
        <ClerkProvider
          publishableKey={publishableKey}
          tokenCache={tokenCache}
          proxyUrl={proxyUrl}
        >
          <ClerkLoaded>
            <QueryClientProvider client={queryClient}>
              <GestureHandlerRootView style={{ flex: 1 }}>
                <KeyboardProvider>
                  <ClerkBridges />
                  <Slot />
                </KeyboardProvider>
              </GestureHandlerRootView>
            </QueryClientProvider>
          </ClerkLoaded>
        </ClerkProvider>
      </ErrorBoundary>
    </SafeAreaProvider>
  );
}
