/**
 * Global test setup. Silences a few noisy RN warnings that fire under
 * jsdom-style environments and provides defaults for browser-y globals
 * the component code touches at import time.
 */
process.env.EXPO_PUBLIC_DOMAIN = process.env.EXPO_PUBLIC_DOMAIN || "test.local";

// AsyncStorage's runtime needs the host app's native module which jest
// can't link to. The package ships an in-memory mock that satisfies the
// API surface; wiring it here lets any component that touches storage
// (e.g. the quiet-hours device-tz banner in `NotificationPrefs`) render
// under the test renderer without blowing up at import time.
jest.mock(
  "@react-native-async-storage/async-storage",
  () =>
    require("@react-native-async-storage/async-storage/jest/async-storage-mock"),
);
