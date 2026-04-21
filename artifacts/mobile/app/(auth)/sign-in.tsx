import { useSignIn } from "@clerk/expo";
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

export default function SignInScreen() {
  const { signIn, errors, fetchStatus } = useSignIn();
  const router = useRouter();
  const colors = useColors();
  const insets = useSafeAreaInsets();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const submit = async () => {
    const { error } = await signIn.password({ emailAddress: email, password });
    if (error) return;
    if (signIn.status === "complete") {
      await signIn.finalize({
        navigate: ({ decorateUrl }) => {
          const url = decorateUrl("/(tabs)");
          router.replace(url as Href);
        },
      });
    }
  };

  const loading = fetchStatus === "fetching";

  return (
    <View style={[styles.container, { backgroundColor: colors.background, paddingTop: insets.top + 40 }]}>
      <View style={styles.head}>
        <Text style={[styles.eyebrow, { color: colors.mutedForeground }]}>AXIOM_PREDICT</Text>
        <Text style={[styles.title, { color: colors.primary }]}>OPERATOR SIGN-IN</Text>
        <Text style={[styles.sub, { color: colors.mutedForeground }]}>
          Authenticate to access live telemetry
        </Text>
      </View>

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
        {errors.fields.identifier && (
          <Text style={[styles.error, { color: colors.destructive }]}>
            {errors.fields.identifier.message}
          </Text>
        )}

        <Text style={[styles.label, { color: colors.mutedForeground, marginTop: 16 }]}>PASSWORD</Text>
        <TextInput
          style={[styles.input, { color: colors.foreground, backgroundColor: colors.input, borderColor: colors.border }]}
          secureTextEntry
          placeholder="••••••••"
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
            <Text style={[styles.buttonText, { color: colors.primaryForeground }]}>SIGN IN</Text>
          )}
        </Pressable>

        <View style={styles.linkRow}>
          <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
            Need access?{" "}
          </Text>
          <Link href="/(auth)/sign-up">
            <Text style={{ color: colors.primary, fontSize: 12, fontWeight: "600" }}>
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
