import { useSignUp } from "@clerk/expo";
import { Link, useRouter, type Href } from "expo-router";
import React, { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useColors } from "@/hooks/useColors";

export default function SignUpScreen() {
  const { isLoaded, signUp, setActive } = useSignUp();
  const router = useRouter();
  const colors = useColors();
  const insets = useSafeAreaInsets();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [pendingVerification, setPendingVerification] = useState(false);

  const submit = async () => {
    if (!isLoaded || !signUp) return;
    setLoading(true);
    setError("");
    try {
      await signUp.create({ emailAddress: email, password });
      await signUp.prepareEmailAddressVerification({ strategy: "email_code" });
      setPendingVerification(true);
    } catch (err: unknown) {
      const clerkErr = err as { errors?: { longMessage?: string; message?: string }[] };
      setError(
        clerkErr.errors?.[0]?.longMessage ??
          clerkErr.errors?.[0]?.message ??
          "Sign up failed.",
      );
    } finally {
      setLoading(false);
    }
  };

  const verify = async () => {
    if (!isLoaded || !signUp) return;
    setLoading(true);
    setError("");
    try {
      const result = await signUp.attemptEmailAddressVerification({ code });
      if (result.status === "complete") {
        await setActive({ session: result.createdSessionId });
        router.replace("/(tabs)" as Href);
      }
    } catch (err: unknown) {
      const clerkErr = err as { errors?: { longMessage?: string; message?: string }[] };
      setError(
        clerkErr.errors?.[0]?.longMessage ??
          clerkErr.errors?.[0]?.message ??
          "Verification failed.",
      );
    } finally {
      setLoading(false);
    }
  };

  const resendCode = async () => {
    if (!isLoaded || !signUp) return;
    try {
      await signUp.prepareEmailAddressVerification({ strategy: "email_code" });
    } catch {
      /* ignore */
    }
  };

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
          {pendingVerification ? "VERIFY EMAIL" : "REQUEST ACCESS"}
        </Text>
        <Text style={[styles.sub, { color: colors.mutedForeground }]}>
          {pendingVerification
            ? "Enter the 6-digit code sent to your email"
            : "Operator approval required after sign-up"}
        </Text>
      </View>

      {pendingVerification ? (
        <View style={styles.form}>
          <Text style={[styles.label, { color: colors.mutedForeground }]}>
            VERIFICATION CODE
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
            keyboardType="number-pad"
            placeholder="000000"
            placeholderTextColor={colors.mutedForeground}
            value={code}
            onChangeText={setCode}
          />

          {!!error && (
            <Text style={[styles.error, { color: colors.destructive }]}>
              {error}
            </Text>
          )}

          <Pressable
            onPress={verify}
            disabled={!code || loading}
            style={({ pressed }) => [
              styles.button,
              {
                backgroundColor: colors.primary,
                opacity: !code || loading ? 0.5 : pressed ? 0.85 : 1,
              },
            ]}
          >
            {loading ? (
              <ActivityIndicator color={colors.primaryForeground} />
            ) : (
              <Text
                style={[styles.buttonText, { color: colors.primaryForeground }]}
              >
                VERIFY
              </Text>
            )}
          </Pressable>

          <Pressable onPress={resendCode} style={styles.linkRow}>
            <Text style={{ color: colors.primary, fontSize: 12 }}>
              Resend code
            </Text>
          </Pressable>
        </View>
      ) : (
        <View style={styles.form}>
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
            placeholder="At least 8 characters"
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
              <Text
                style={[styles.buttonText, { color: colors.primaryForeground }]}
              >
                CREATE ACCOUNT
              </Text>
            )}
          </Pressable>

          <View style={styles.linkRow}>
            <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
              Already approved?{" "}
            </Text>
            <Link href="/(auth)/sign-in">
              <Text
                style={{
                  color: colors.primary,
                  fontSize: 12,
                  fontWeight: "600",
                }}
              >
                Sign in
              </Text>
            </Link>
          </View>

          <View nativeID="clerk-captcha" />
        </View>
      )}
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
});
