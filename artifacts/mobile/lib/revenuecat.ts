import { Platform } from "react-native";
import Purchases, { LOG_LEVEL } from "react-native-purchases";

const APPLE_KEY = process.env.EXPO_PUBLIC_REVENUECAT_APPLE_KEY;
const GOOGLE_KEY = process.env.EXPO_PUBLIC_REVENUECAT_GOOGLE_KEY;
const WEB_KEY = process.env.EXPO_PUBLIC_REVENUECAT_WEB_KEY;

let _configured = false;
let _identifiedAs: string | null = null;

export function revenueCatConfigured(): boolean {
  return _configured;
}

export async function ensureRevenueCat(userId: string | null): Promise<void> {
  const key =
    Platform.OS === "ios"
      ? APPLE_KEY
      : Platform.OS === "android"
        ? GOOGLE_KEY
        : WEB_KEY;
  if (!key) {
    return;
  }
  try {
    if (!_configured) {
      Purchases.setLogLevel(LOG_LEVEL.WARN);
      Purchases.configure({ apiKey: key, appUserID: userId ?? undefined });
      _configured = true;
      _identifiedAs = userId;
      return;
    }
    if (userId && userId !== _identifiedAs) {
      await Purchases.logIn(userId);
      _identifiedAs = userId;
    } else if (!userId && _identifiedAs) {
      await Purchases.logOut();
      _identifiedAs = null;
    }
  } catch (e) {
    console.warn("[revenuecat] init failed", e);
  }
}
