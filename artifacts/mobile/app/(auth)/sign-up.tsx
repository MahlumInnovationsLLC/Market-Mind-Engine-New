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
  const { signUp, errors, fetchStatus } = useSignUp();
  const router = useRouter();
  const colors = useColors();
  const insets = useSafeAreaInsets();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");

  const submit = async () => {
    const { error } = await signUp.password({ emailAddress: email, password });
    if (error) return;
    await signUp.verifications.sendEmailCode();
  };

  const verify = async () => {
    await signUp.verifications.verifyEmailCode({ code });
    if (signUp.status === "complete") {
      await signUp.finalize({
        navigate: ({ decorateUrl }) => {
          const url = decorateUrl("/(tabs)");
          router.replace(url as Href);
        },
      });
    }
  };

  const loading = fetchStatus === "fetching";

  const verifyMode =
    signUp.status === "missing_requirements" &&
    signUp.unverifiedFields?.includes("email_address") &&
    signUp.missingFields.length === 0;

  return (
    <View style={[styles.container, { backgroundColor: colors.background, paddingTop: insets.top + 40 }]}>
      <View style={styles.head}>
        <Text style={[styles.eyebrow, { color: colors.mutedForeground }]}>AXIOM_PREDICT</Text>
        <Text style={[styles.title, { color: colors.primary }]}>
          {verifyMode ? "VERIFY EMAIL" : "REQUEST ACCESS"}
        </Text>
        <Text style={[styles.sub, { color: colors.mutedForeground }]}>
          {verifyMode
            ? "Enter the 6-digit code sent to your email"
            : "Operator approval required after sign-up"}
        </Text>
      </View>

      {verifyMode ? (
        <View style={styles.form}>
          <Text style={[styles.label, { color: colors.mutedForeground }]}>VERIFICATION CODE</Text>
          <TextInput
            style={[styles.input, { color: colors.foreground, backgroundColor: colors.input, borderColor: colors.border }]}
            keyboardType="number-pad"
            placeholder="000000"
            placeholderTextColor={colors.mutedForeground}
            value={code}
            onChangeText={setCode}
          />
          {errors.fields.code && (
            <Text style={[styles.error, { color: colors.destructive }]}>
              {errors.fields.code.message}
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
              <Text style={[styles.buttonText, { color: colors.primaryForeground }]}>VERIFY</Text>
            )}
          </Pressable>
          <Pressable onPress={() => signUp.verifications.sendEmailCode()} style={styles.linkRow}>
            <Text style={{ color: colors.primary, fontSize: 12 }}>Resend code</Text>
          </Pressable>
        </View>
      ) : (
        <View style={styles.form}>
          <Text style={[styles.label, { color: colors.mutedForeground }]}>EMAIL</Text>
          <TextInput
            style={[styles.input, { color: colors.foreground, backgroundColor: colors.input, borderColor: colors.border }]}
            autoCapitalize="none"
            autoComplete="email"
            keyboardType="email-address"
            placeholder="you@operator.com"
            placeholderTextColor={colors.mutedForeground}
            value={email}
            onChangeText={setEmail}
          />
          {errors.fields.emailAddress && (
            <Text style={[styles.error, { color: colors.destructive }]}>
              {errors.fields.emailAddress.message}
            </Text>
          )}

          <Text style={[styles.label, { color: colors.mutedForeground, marginTop: 16 }]}>PASSWORD</Text>
          <TextInput
            style={[styles.input, { color: colors.foreground, backgroundColor: colors.input, borderColor: colors.border }]}
            secureTextEntry
            placeholder="At least 8 characters"
            placeholderTextColor={colors.mutedForeground}
            value={password}
            onChangeText={setPassword}
          />
          {errors.fields.password && (
            <Text style={[styles.error, { color: colors.destructive }]}>
              {errors.fields.password.message}
            </Text>
          )}

          <Pressable
            onPress={submit}
            disabled={!email || !password || loading}
            style={({ pressed }) => [
              styles.button,
              {
                backgroundColor: colors.primary,
                opacity: !email || !password || loading ? 0.5 : pressed ? 0.85 : 1,
              },
            ]}
          >
            {loading ? (
              <ActivityIndicator color={colors.primaryForeground} />
            ) : (
              <Text style={[styles.buttonText, { color: colors.primaryForeground }]}>CREATE ACCOUNT</Text>
            )}
          </Pressable>

          <View style={styles.linkRow}>
            <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>Already approved? </Text>
            <Link href="/(auth)/sign-in">
              <Text style={{ color: colors.primary, fontSize: 12, fontWeight: "600" }}>Sign in</Text>
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
    marginTop: 6,
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
