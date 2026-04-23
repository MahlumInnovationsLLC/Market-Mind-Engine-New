const STORAGE_KEY = "axiom:pending_checkout_price_id";

export function setPendingCheckoutPriceId(priceId: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, priceId);
  } catch {
    // ignore (private mode, quota, SSR)
  }
}

export function getPendingCheckoutPriceId(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function clearPendingCheckoutPriceId(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
