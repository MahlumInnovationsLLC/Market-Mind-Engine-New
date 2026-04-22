import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import { Platform } from "react-native";
import Constants from "expo-constants";

import { apiFetch } from "./api";

let _registeredToken: string | null = null;

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

async function ensureAndroidChannels(): Promise<void> {
  if (Platform.OS !== "android") return;
  await Notifications.setNotificationChannelAsync("trades", {
    name: "Live trades",
    importance: Notifications.AndroidImportance.HIGH,
    sound: "default",
    vibrationPattern: [0, 250, 250, 250],
  });
  await Notifications.setNotificationChannelAsync("halts", {
    name: "Trading halts",
    importance: Notifications.AndroidImportance.MAX,
    sound: "default",
    vibrationPattern: [0, 500, 250, 500],
  });
}

async function getExpoPushToken(): Promise<string | null> {
  if (!Device.isDevice) return null;
  if (Platform.OS === "web") return null;
  try {
    const existing = await Notifications.getPermissionsAsync();
    let status = existing.status;
    if (status !== "granted") {
      const req = await Notifications.requestPermissionsAsync();
      status = req.status;
    }
    if (status !== "granted") return null;
    const projectId =
      (Constants.expoConfig?.extra as { eas?: { projectId?: string } })?.eas?.projectId ??
      (Constants.easConfig as { projectId?: string } | undefined)?.projectId;
    const token = await Notifications.getExpoPushTokenAsync(
      projectId ? { projectId } : undefined,
    );
    return token.data ?? null;
  } catch (err) {
    console.warn("expo push token fetch failed", err);
    return null;
  }
}

export async function registerPushTokenForSignedInUser(): Promise<void> {
  try {
    await ensureAndroidChannels();
    const token = await getExpoPushToken();
    if (!token) return;
    if (token === _registeredToken) return;
    await apiFetch("/api/me/push-token", {
      method: "POST",
      body: { expo_token: token, platform: Platform.OS },
    });
    _registeredToken = token;
  } catch (err) {
    console.warn("push token register failed", err);
  }
}

export async function unregisterPushTokenForSignedOutUser(): Promise<void> {
  if (!_registeredToken) return;
  const token = _registeredToken;
  _registeredToken = null;
  try {
    await apiFetch("/api/me/push-token", {
      method: "DELETE",
      body: { expo_token: token },
    });
  } catch {
    /* best effort — sign-out should not fail on this */
  }
}

export type NotificationDeepLink = "/positions" | "/pipeline" | string;

export function deepLinkFromNotificationData(
  data: Record<string, unknown> | null | undefined,
): NotificationDeepLink | null {
  if (!data) return null;
  const link = data["deep_link"];
  if (typeof link === "string" && link.startsWith("/")) return link;
  const type = data["type"];
  if (type === "live_trade") return "/positions";
  if (type === "halt") return "/pipeline";
  return null;
}
