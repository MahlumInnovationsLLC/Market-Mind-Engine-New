import { useAuth } from "@clerk/expo";
import { Feather } from "@expo/vector-icons";
import { BlurView } from "expo-blur";
import { Redirect, Tabs } from "expo-router";
import React from "react";
import { Platform, StyleSheet, View, useColorScheme } from "react-native";

import { useColors } from "@/hooks/useColors";

export default function TabsLayout() {
  const { isLoaded, isSignedIn } = useAuth();
  const colors = useColors();
  const colorScheme = useColorScheme();
  const isDark = colorScheme === "dark";
  const isIOS = Platform.OS === "ios";
  const isWeb = Platform.OS === "web";

  if (!isLoaded) return null;
  if (!isSignedIn) return <Redirect href="/(auth)/sign-in" />;

  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.mutedForeground,
        tabBarLabelStyle: {
          fontSize: 10,
          letterSpacing: 1.5,
          fontFamily: "Inter_600SemiBold",
        },
        headerStyle: {
          backgroundColor: colors.background,
          borderBottomWidth: 1,
          borderBottomColor: colors.border,
        },
        headerTitleStyle: {
          color: colors.foreground,
          fontFamily: "Inter_700Bold",
          letterSpacing: 1.5,
          fontSize: 14,
        },
        headerTintColor: colors.primary,
        tabBarStyle: {
          position: "absolute",
          backgroundColor: isIOS ? "transparent" : colors.background,
          borderTopWidth: 1,
          borderTopColor: colors.border,
          elevation: 0,
          ...(isWeb ? { height: 84 } : {}),
        },
        tabBarBackground: () =>
          isIOS ? (
            <BlurView
              intensity={100}
              tint={isDark ? "dark" : "light"}
              style={StyleSheet.absoluteFill}
            />
          ) : (
            <View
              style={[
                StyleSheet.absoluteFill,
                { backgroundColor: colors.background },
              ]}
            />
          ),
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "DASHBOARD",
          tabBarLabel: "Dashboard",
          tabBarIcon: ({ color }) => (
            <Feather name="activity" size={20} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="positions"
        options={{
          title: "POSITIONS",
          tabBarLabel: "Positions",
          tabBarIcon: ({ color }) => (
            <Feather name="briefcase" size={20} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="pipeline"
        options={{
          title: "PIPELINE",
          tabBarLabel: "Pipeline",
          tabBarIcon: ({ color }) => (
            <Feather name="cpu" size={20} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="subscription"
        options={{
          title: "SUBSCRIPTION",
          tabBarLabel: "Plan",
          tabBarIcon: ({ color }) => (
            <Feather name="credit-card" size={20} color={color} />
          ),
        }}
      />
    </Tabs>
  );
}
