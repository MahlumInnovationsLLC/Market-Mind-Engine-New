/**
 * Lightweight timezone helpers for the web. Mirrors the mobile
 * `artifacts/mobile/lib/timezones.ts` implementation so the quiet-hours
 * surfaces — the Profile page hint and the notification history badges
 * — stay consistent across platforms (task #629).
 */

export function offsetForZone(zone: string, now: Date = new Date()): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      timeZoneName: "shortOffset",
    }).formatToParts(now);
    const off = parts.find((p) => p.type === "timeZoneName")?.value ?? "";
    if (off === "GMT") return "GMT+0";
    return off;
  } catch {
    return "";
  }
}

/**
 * True if ``when`` falls inside the user's quiet-hours window
 * (``startHour``..``endHour`` interpreted in ``zone``).
 *
 * Mirrors the server-side ``is_in_quiet_hours`` semantics in
 * ``api-server/app/notifications/push.py`` so the web feed annotates
 * the same set of pushes the backend would have suppressed:
 *   - all three inputs must be present and valid
 *   - equal ``startHour`` and ``endHour`` mean "no window" (zero-length
 *     range, not 24h)
 *   - overnight windows wrap past midnight, e.g. 22 -> 7 covers
 *     [22, 24) ∪ [0, 7)
 *   - any zone the runtime can't resolve returns ``false`` instead of
 *     throwing.
 */
export function isInQuietHours(
  startHour: number | null | undefined,
  endHour: number | null | undefined,
  zone: string | null | undefined,
  when: Date = new Date(),
): boolean {
  if (
    startHour === null ||
    startHour === undefined ||
    endHour === null ||
    endHour === undefined ||
    !zone
  ) {
    return false;
  }
  if (!Number.isInteger(startHour) || !Number.isInteger(endHour)) return false;
  if (startHour < 0 || startHour > 23 || endHour < 0 || endHour > 23) {
    return false;
  }
  if (startHour === endHour) return false;
  let localHour: number;
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      hour: "2-digit",
      hourCycle: "h23",
    }).formatToParts(when);
    const hourStr = parts.find((p) => p.type === "hour")?.value ?? "";
    localHour = Number.parseInt(hourStr, 10);
    if (!Number.isFinite(localHour)) return false;
  } catch {
    return false;
  }
  if (startHour < endHour) {
    return localHour >= startHour && localHour < endHour;
  }
  return localHour >= startHour || localHour < endHour;
}
