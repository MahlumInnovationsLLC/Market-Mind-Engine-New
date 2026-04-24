/**
 * Global test setup. Silences a few noisy RN warnings that fire under
 * jsdom-style environments and provides defaults for browser-y globals
 * the component code touches at import time.
 */
process.env.EXPO_PUBLIC_DOMAIN = process.env.EXPO_PUBLIC_DOMAIN || "test.local";
