export type TimeZoneOption = {
  id: string;
  city: string;
  region: string;
};

export const TIMEZONES: TimeZoneOption[] = [
  { id: "UTC", city: "UTC", region: "UTC" },

  { id: "Africa/Cairo", city: "Cairo", region: "Africa" },
  { id: "Africa/Casablanca", city: "Casablanca", region: "Africa" },
  { id: "Africa/Johannesburg", city: "Johannesburg", region: "Africa" },
  { id: "Africa/Lagos", city: "Lagos", region: "Africa" },
  { id: "Africa/Nairobi", city: "Nairobi", region: "Africa" },
  { id: "Africa/Tunis", city: "Tunis", region: "Africa" },

  { id: "America/Anchorage", city: "Anchorage", region: "Americas" },
  { id: "America/Argentina/Buenos_Aires", city: "Buenos Aires", region: "Americas" },
  { id: "America/Bogota", city: "Bogotá", region: "Americas" },
  { id: "America/Caracas", city: "Caracas", region: "Americas" },
  { id: "America/Chicago", city: "Chicago", region: "Americas" },
  { id: "America/Denver", city: "Denver", region: "Americas" },
  { id: "America/Edmonton", city: "Edmonton", region: "Americas" },
  { id: "America/Halifax", city: "Halifax", region: "Americas" },
  { id: "America/Havana", city: "Havana", region: "Americas" },
  { id: "America/Indiana/Indianapolis", city: "Indianapolis", region: "Americas" },
  { id: "America/Juneau", city: "Juneau", region: "Americas" },
  { id: "America/Lima", city: "Lima", region: "Americas" },
  { id: "America/Los_Angeles", city: "Los Angeles", region: "Americas" },
  { id: "America/Mexico_City", city: "Mexico City", region: "Americas" },
  { id: "America/Montevideo", city: "Montevideo", region: "Americas" },
  { id: "America/New_York", city: "New York", region: "Americas" },
  { id: "America/Panama", city: "Panama", region: "Americas" },
  { id: "America/Phoenix", city: "Phoenix", region: "Americas" },
  { id: "America/Puerto_Rico", city: "San Juan", region: "Americas" },
  { id: "America/Santiago", city: "Santiago", region: "Americas" },
  { id: "America/Sao_Paulo", city: "São Paulo", region: "Americas" },
  { id: "America/St_Johns", city: "St. John's", region: "Americas" },
  { id: "America/Tegucigalpa", city: "Tegucigalpa", region: "Americas" },
  { id: "America/Toronto", city: "Toronto", region: "Americas" },
  { id: "America/Vancouver", city: "Vancouver", region: "Americas" },
  { id: "America/Winnipeg", city: "Winnipeg", region: "Americas" },

  { id: "Asia/Almaty", city: "Almaty", region: "Asia" },
  { id: "Asia/Amman", city: "Amman", region: "Asia" },
  { id: "Asia/Bangkok", city: "Bangkok", region: "Asia" },
  { id: "Asia/Beirut", city: "Beirut", region: "Asia" },
  { id: "Asia/Dhaka", city: "Dhaka", region: "Asia" },
  { id: "Asia/Dubai", city: "Dubai", region: "Asia" },
  { id: "Asia/Ho_Chi_Minh", city: "Ho Chi Minh City", region: "Asia" },
  { id: "Asia/Hong_Kong", city: "Hong Kong", region: "Asia" },
  { id: "Asia/Jakarta", city: "Jakarta", region: "Asia" },
  { id: "Asia/Jerusalem", city: "Jerusalem", region: "Asia" },
  { id: "Asia/Karachi", city: "Karachi", region: "Asia" },
  { id: "Asia/Kolkata", city: "Kolkata", region: "Asia" },
  { id: "Asia/Kuala_Lumpur", city: "Kuala Lumpur", region: "Asia" },
  { id: "Asia/Kuwait", city: "Kuwait", region: "Asia" },
  { id: "Asia/Manila", city: "Manila", region: "Asia" },
  { id: "Asia/Riyadh", city: "Riyadh", region: "Asia" },
  { id: "Asia/Seoul", city: "Seoul", region: "Asia" },
  { id: "Asia/Shanghai", city: "Shanghai", region: "Asia" },
  { id: "Asia/Singapore", city: "Singapore", region: "Asia" },
  { id: "Asia/Taipei", city: "Taipei", region: "Asia" },
  { id: "Asia/Tashkent", city: "Tashkent", region: "Asia" },
  { id: "Asia/Tehran", city: "Tehran", region: "Asia" },
  { id: "Asia/Tokyo", city: "Tokyo", region: "Asia" },
  { id: "Asia/Yangon", city: "Yangon", region: "Asia" },

  { id: "Atlantic/Azores", city: "Azores", region: "Atlantic" },
  { id: "Atlantic/Cape_Verde", city: "Cape Verde", region: "Atlantic" },
  { id: "Atlantic/Reykjavik", city: "Reykjavík", region: "Atlantic" },

  { id: "Australia/Adelaide", city: "Adelaide", region: "Australia" },
  { id: "Australia/Brisbane", city: "Brisbane", region: "Australia" },
  { id: "Australia/Darwin", city: "Darwin", region: "Australia" },
  { id: "Australia/Hobart", city: "Hobart", region: "Australia" },
  { id: "Australia/Melbourne", city: "Melbourne", region: "Australia" },
  { id: "Australia/Perth", city: "Perth", region: "Australia" },
  { id: "Australia/Sydney", city: "Sydney", region: "Australia" },

  { id: "Europe/Amsterdam", city: "Amsterdam", region: "Europe" },
  { id: "Europe/Athens", city: "Athens", region: "Europe" },
  { id: "Europe/Belgrade", city: "Belgrade", region: "Europe" },
  { id: "Europe/Berlin", city: "Berlin", region: "Europe" },
  { id: "Europe/Brussels", city: "Brussels", region: "Europe" },
  { id: "Europe/Bucharest", city: "Bucharest", region: "Europe" },
  { id: "Europe/Budapest", city: "Budapest", region: "Europe" },
  { id: "Europe/Copenhagen", city: "Copenhagen", region: "Europe" },
  { id: "Europe/Dublin", city: "Dublin", region: "Europe" },
  { id: "Europe/Helsinki", city: "Helsinki", region: "Europe" },
  { id: "Europe/Istanbul", city: "Istanbul", region: "Europe" },
  { id: "Europe/Kyiv", city: "Kyiv", region: "Europe" },
  { id: "Europe/Lisbon", city: "Lisbon", region: "Europe" },
  { id: "Europe/London", city: "London", region: "Europe" },
  { id: "Europe/Madrid", city: "Madrid", region: "Europe" },
  { id: "Europe/Moscow", city: "Moscow", region: "Europe" },
  { id: "Europe/Oslo", city: "Oslo", region: "Europe" },
  { id: "Europe/Paris", city: "Paris", region: "Europe" },
  { id: "Europe/Prague", city: "Prague", region: "Europe" },
  { id: "Europe/Rome", city: "Rome", region: "Europe" },
  { id: "Europe/Sofia", city: "Sofia", region: "Europe" },
  { id: "Europe/Stockholm", city: "Stockholm", region: "Europe" },
  { id: "Europe/Vienna", city: "Vienna", region: "Europe" },
  { id: "Europe/Warsaw", city: "Warsaw", region: "Europe" },
  { id: "Europe/Zurich", city: "Zürich", region: "Europe" },

  { id: "Pacific/Auckland", city: "Auckland", region: "Pacific" },
  { id: "Pacific/Fiji", city: "Fiji", region: "Pacific" },
  { id: "Pacific/Guam", city: "Guam", region: "Pacific" },
  { id: "Pacific/Honolulu", city: "Honolulu", region: "Pacific" },
  { id: "Pacific/Pago_Pago", city: "Pago Pago", region: "Pacific" },
  { id: "Pacific/Port_Moresby", city: "Port Moresby", region: "Pacific" },
  { id: "Pacific/Tahiti", city: "Tahiti", region: "Pacific" },
];

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

export function findTimezone(id: string | null | undefined): TimeZoneOption | null {
  if (!id) return null;
  return TIMEZONES.find((t) => t.id === id) ?? null;
}

export function searchTimezones(
  query: string,
  zones: TimeZoneOption[] = TIMEZONES,
): TimeZoneOption[] {
  const q = query.trim().toLowerCase().replace(/_/g, " ");
  if (!q) return zones;
  return zones.filter((z) => {
    const hay =
      `${z.id} ${z.city} ${z.region}`.toLowerCase().replace(/_/g, " ");
    return hay.includes(q);
  });
}
