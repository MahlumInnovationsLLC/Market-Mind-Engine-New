import { useSSO, useSignIn } from "@clerk/expo";
import * as AuthSession from "expo-auth-session";
import { Link, useRouter, type Href } from "expo-router";
import * as WebBrowser from "expo-web-browser";
import React, { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useColors } from "@/hooks/useColors";

// Required for the OAuth flow to complete cleanly when the user
// returns from the in-app browser. Safe to call at module scope.
WebBrowser.maybeCompleteAuthSession();

// Warm the in-app browser on Android so the Google consent screen
// opens promptly when the operator taps "Continue with Google".
function useWarmUpBrowser() {
  useEffect(() => {
    if (Platform.OS !== "android") return;
    void WebBrowser.warmUpAsync();
    return () => {
      void WebBrowser.coolDownAsync();
    };
  }, []);
}

export default function SignInScreen() {
  useWarmUpBrowser();
  const { isLoaded, signIn, setActive } = useSignIn();
  const { startSSOFlow } = useSSO();
  const router = useRouter();
  const colors = useColors();
  const insets = useSafeAreaInsets();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [ssoLoading, setSsoLoading] = useState(false);

  const submit = async () => {
    if (!isLoaded || !signIn) return;
    setLoading(true);
    setError("");
    try {
      const result = await signIn.create({ identifier: email, password });
      if (result.status === "complete") {
        await setActive({ session: result.createdSessionId });
        router.replace("/(tabs)" as Href);
      }
    } catch (err: unknown) {
      const clerkErr = err as { errors?: { longMessage?: string; message?: string }[] };
      setError(
        clerkErr.errors?.[0]?.longMessage ??
          clerkErr.errors?.[0]?.message ??
          "Sign in failed. Check your credentials.",
      );
    } finally {
      setLoading(false);
    }
  };

  // Google SSO via Clerk's native OAuth flow. The operator's web
  // account is Google-OAuth-only (no password set), so the
  // email/password form alone cannot sign them in on mobile —
  // they need the same "Continue with Google" path the web app
  // uses through Clerk's prebuilt <SignIn> component.
  const signInWithGoogle = useCallback(async () => {
    if (ssoLoading) return;
    setError("");
    setSsoLoading(true);
    try {
      const { createdSessionId, setActive: ssoSetActive } = await startSSOFlow({
        strategy: "oauth_google",
        redirectUrl: AuthSession.makeRedirectUri({ scheme: "axiommobile" }),
      });
      if (createdSessionId && ssoSetActive) {
        await ssoSetActive({ session: createdSessionId });
        router.replace("/(tabs)" as Href);
      }
    } catch (err: unknown) {
      const clerkErr = err as { errors?: { longMessage?: string; message?: string }[] };
      setError(
        clerkErr.errors?.[0]?.longMessage ??
          clerkErr.errors?.[0]?.message ??
          "Google sign-in failed. Try again.",
      );
    } finally {
      setSsoLoading(false);
    }
  }, [router, ssoLoading, startSSOFlow]);

  return (
    <View
      style={[
        styles.container,
        { backgroundColor: colors.background, paddingTop: insets.top + 40 },
      ]}
    >
      <View style={styles.head}>
        <Text style={[styles.eyebrow, { color: colors.mutedForeground }]}>
          AXIOM_PREDICT
        </Text>
        <Text style={[styles.title, { color: colors.primary }]}>
          OPERATOR SIGN-IN
        </Text>
        <Text style={[styles.sub, { color: colors.mutedForeground }]}>
          Authenticate to access live telemetry
        </Text>
      </View>

      <View style={styles.form}>
        <Pressable
          onPress={signInWithGoogle}
          disabled={ssoLoading || !isLoaded}
          style={({ pressed }) => [
            styles.ssoButton,
            {
              backgroundColor: colors.input,
              borderColor: colors.border,
              opacity: ssoLoading || !isLoaded ? 0.5 : pressed ? 0.85 : 1,
            },
          ]}
        >
          {ssoLoading ? (
            <ActivityIndicator color={colors.foreground} />
          ) : (
            <Text style={[styles.ssoButtonText, { color: colors.foreground }]}>
              CONTINUE WITH GOOGLE
            </Text>
          )}
        </Pressable>

        <View style={styles.dividerRow}>
          <View style={[styles.dividerLine, { backgroundColor: colors.border }]} />
          <Text style={[styles.dividerText, { color: colors.mutedForeground }]}>
            OR
          </Text>
          <View style={[styles.dividerLine, { backgroundColor: colors.border }]} />
        </View>

        <Text style={[styles.label, { color: colors.mutedForeground }]}>
          EMAIL
        </Text>
        <TextInput
          style={[
            styles.input,
            {
              color: colors.foreground,
              backgroundColor: colors.input,
              borderColor: colors.border,
            },
          ]}
          autoCapitalize="none"
          autoComplete="email"
          keyboardType="email-address"
          placeholder="you@operator.com"
          placeholderTextColor={colors.mutedForeground}
          value={email}
          onChangeText={setEmail}
        />

        <Text
          style={[
            styles.label,
            { color: colors.mutedForeground, marginTop: 16 },
          ]}
        >
          PASSWORD
        </Text>
        <TextInput
          style={[
            styles.input,
            {
              color: colors.foreground,
              backgroundColor: colors.input,
              borderColor: colors.border,
            },
          ]}
          secureTextEntry
          placeholder="••••••••"
          placeholderTextColor={colors.mutedForeground}
          value={password}
          onChangeText={setPassword}
        />

        {!!error && (
          <Text style={[styles.error, { color: colors.destructive }]}>
            {error}
          </Text>
        )}

        <Pressable
          onPress={submit}
          disabled={!email || !password || loading || !isLoaded}
          style={({ pressed }) => [
            styles.button,
            {
              backgroundColor: colors.primary,
              opacity:
                !email || !password || loading || !isLoaded
                  ? 0.5
                  : pressed
                    ? 0.85
                    : 1,
            },
          ]}
        >
          {loading ? (
            <ActivityIndicator color={colors.primaryForeground} />
          ) : (
            <Text style={[styles.buttonText, { color: colors.primaryForeground }]}>
              SIGN IN
            </Text>
          )}
        </Pressable>

        <View style={styles.linkRow}>
          <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
            Need access?{" "}
          </Text>
          <Link href="/(auth)/sign-up">
            <Text
              style={{ color: colors.primary, fontSize: 12, fontWeight: "600" }}
            >
              Request operator account
            </Text>
          </Link>
        </View>
      </View>

      <Text style={[styles.footer, { color: colors.mutedForeground }]}>
        SYS_STATUS :: standby
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, paddingHorizontal: 24 },
  head: { marginBottom: 32 },
  eyebrow: {
    fontSize: 11,
    letterSpacing: 3,
    fontFamily: "Inter_500Medium",
    marginBottom: 8,
  },
  title: {
    fontSize: 22,
    fontFamily: "Inter_700Bold",
    letterSpacing: 1.5,
  },
  sub: {
    fontSize: 13,
    marginTop: 6,
    fontFamily: "Inter_400Regular",
  },
  form: { gap: 4 },
  ssoButton: {
    paddingVertical: 16,
    alignItems: "center",
    borderWidth: 1,
    marginBottom: 20,
  },
  ssoButtonText: {
    fontSize: 13,
    letterSpacing: 2,
    fontFamily: "Inter_700Bold",
  },
  dividerRow: {
    flexDirection: "row",
    alignItems: "center",
    marginBottom: 20,
    gap: 12,
  },
  dividerLine: { flex: 1, height: StyleSheet.hairlineWidth },
  dividerText: {
    fontSize: 10,
    letterSpacing: 2,
    fontFamily: "Inter_500Medium",
  },
  label: {
    fontSize: 10,
    letterSpacing: 2,
    fontFamily: "Inter_500Medium",
    marginBottom: 6,
  },
  input: {
    borderWidth: 1,
    paddingHorizontal: 12,
    paddingVertical: 14,
    fontSize: 15,
    fontFamily: "Inter_400Regular",
  },
  error: {
    fontSize: 12,
    marginTop: 8,
    fontFamily: "Inter_400Regular",
  },
  button: {
    paddingVertical: 16,
    alignItems: "center",
    marginTop: 24,
  },
  buttonText: {
    fontSize: 13,
    letterSpacing: 2,
    fontFamily: "Inter_700Bold",
  },
  linkRow: {
    flexDirection: "row",
    justifyContent: "center",
    marginTop: 20,
  },
  footer: {
    position: "absolute",
    bottom: 32,
    left: 0,
    right: 0,
    textAlign: "center",
    fontSize: 10,
    letterSpacing: 2,
  },
});
