import { useAuth } from "@clerk/expo";
import { useQuery } from "@tanstack/react-query";
import * as WebBrowser from "expo-web-browser";
import React, { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import Purchases, { type PurchasesOffering } from "react-native-purchases";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Card, SectionHeader } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { revenueCatConfigured } from "@/lib/revenuecat";

type SubInfo = {
  user_id: string;
  email: string | null;
  tier: string;
  status: string;
  is_founder: boolean;
  stripe_subscription_id: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  interval: string | null;
};

type PriceSlot = { price_id: string | null; configured: boolean } | null;

type PricingTier = {
  id: string;
  name: string;
  monthly_usd: number;
  annual_usd: number;
  features: string[];
  monthly: PriceSlot;
  annual: PriceSlot;
};

type Pricing = { tiers: PricingTier[] };

type FlatPlan = {
  tier: string;
  interval: "month" | "year";
  price_id: string | null;
  monthly_usd: number;
  annual_usd: number;
};

function flattenPricing(p: Pricing | undefined): FlatPlan[] {
  if (!p) return [];
  const out: FlatPlan[] = [];
  for (const t of p.tiers) {
    if (t.id === "free") continue;
    if (t.monthly?.price_id) {
      out.push({
        tier: t.id,
        interval: "month",
        price_id: t.monthly.price_id,
        monthly_usd: t.monthly_usd,
        annual_usd: t.annual_usd,
      });
    }
    if (t.annual?.price_id) {
      out.push({
        tier: t.id,
        interval: "year",
        price_id: t.annual.price_id,
        monthly_usd: t.monthly_usd,
        annual_usd: t.annual_usd,
      });
    }
  }
  return out;
}

export default function SubscriptionScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const { signOut, userId } = useAuth();
  const [offering, setOffering] = useState<PurchasesOffering | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const sub = useQuery({
    queryKey: ["me", "subscription"],
    queryFn: () => apiFetch<SubInfo>("/api/me/subscription"),
  });
  const pricing = useQuery({
    queryKey: ["billing", "pricing"],
    queryFn: () => apiFetch<Pricing>("/api/billing/pricing"),
  });

  useEffect(() => {
    if (!revenueCatConfigured()) return;
    Purchases.getOfferings()
      .then((o) => setOffering(o.current ?? null))
      .catch(() => undefined);
  }, [userId]);

  const refreshing =
    (sub.isFetching && !sub.isLoading) || (pricing.isFetching && !pricing.isLoading);

  const onRefresh = () => {
    sub.refetch();
    pricing.refetch();
  };

  const openCheckout = async (priceId: string) => {
    try {
      setBusy(priceId);
      const res = await apiFetch<{ url: string }>("/api/billing/checkout-session", {
        method: "POST",
        body: { price_id: priceId },
      });
      await WebBrowser.openBrowserAsync(res.url);
    } catch (e: any) {
      Alert.alert("Checkout failed", e?.message ?? "Unknown error");
    } finally {
      setBusy(null);
    }
  };

  const openPortal = async () => {
    try {
      setBusy("portal");
      const res = await apiFetch<{ url: string }>("/api/billing/portal-session", {
        method: "POST",
      });
      await WebBrowser.openBrowserAsync(res.url);
    } catch (e: any) {
      Alert.alert("Could not open billing portal", e?.message ?? "Unknown error");
    } finally {
      setBusy(null);
    }
  };

  const purchaseRC = async (productId: string) => {
    if (!offering) return;
    const pkg = offering.availablePackages.find(
      (p) => p.product.identifier === productId,
    );
    if (!pkg) {
      Alert.alert("Plan unavailable", "This plan is not configured in RevenueCat.");
      return;
    }
    try {
      setBusy(productId);
      const { customerInfo } = await Purchases.purchasePackage(pkg);
      if (Object.keys(customerInfo.entitlements.active).length > 0) {
        sub.refetch();
      }
    } catch (e: any) {
      if (!e?.userCancelled) {
        Alert.alert("Purchase failed", e?.message ?? "Unknown error");
      }
    } finally {
      setBusy(null);
    }
  };

  if (sub.isLoading) {
    return (
      <View style={[styles.center, { backgroundColor: colors.background }]}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  const tier = sub.data?.tier ?? "free";
  const isPaid = tier === "pro" || tier === "elite" || tier === "founder";
  const plans = flattenPricing(pricing.data);
  const periodEnd = sub.data?.current_period_end
    ? new Date(sub.data.current_period_end).toLocaleDateString()
    : null;

  return (
    <ScrollView
      style={{ backgroundColor: colors.background }}
      contentContainerStyle={[styles.container, { paddingBottom: insets.bottom + 100 }]}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.primary} />
      }
    >
      <SectionHeader>CURRENT PLAN</SectionHeader>
      <Card>
        <Text style={[styles.tier, { color: colors.primary }]}>
          {tier.toUpperCase()}
          {sub.data?.is_founder ? " · FOUNDER BYPASS" : ""}
        </Text>
        <Text style={[styles.muted, { color: colors.mutedForeground }]}>
          Status: {sub.data?.status ?? "—"}
          {sub.data?.interval ? ` · ${sub.data.interval}ly` : ""}
        </Text>
        {periodEnd ? (
          <Text style={[styles.muted, { color: colors.mutedForeground }]}>
            {sub.data?.cancel_at_period_end ? "Ends" : "Renews"}: {periodEnd}
          </Text>
        ) : null}
        {sub.data?.email ? (
          <Text style={[styles.muted, { color: colors.mutedForeground }]}>
            {sub.data.email}
          </Text>
        ) : null}
        {isPaid && sub.data?.stripe_subscription_id ? (
          <Pressable
            onPress={openPortal}
            disabled={busy === "portal"}
            style={({ pressed }) => [
              styles.btnGhost,
              {
                borderColor: colors.border,
                opacity: busy === "portal" ? 0.5 : pressed ? 0.7 : 1,
              },
            ]}
          >
            <Text style={[styles.btnGhostText, { color: colors.foreground }]}>
              MANAGE SUBSCRIPTION
            </Text>
          </Pressable>
        ) : null}
      </Card>

      <SectionHeader>UPGRADE</SectionHeader>
      {plans.length === 0 ? (
        <Card>
          <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
            Pricing unavailable.
          </Text>
        </Card>
      ) : (
        plans.map((plan) => {
          const productId = `${plan.tier}_${plan.interval === "year" ? "annual" : "monthly"}`;
          const rcPkg = offering?.availablePackages.find(
            (p) => p.product.identifier === productId,
          );
          const stripeAvailable = !!plan.price_id;
          const rcAvailable = !!rcPkg;
          const isBusy = busy === plan.price_id || busy === productId;
          const stripeUsd =
            plan.interval === "year" ? plan.annual_usd : plan.monthly_usd;

          return (
            <Card key={`${plan.tier}_${plan.interval}`} style={styles.planCard}>
              <View style={styles.planHead}>
                <Text style={[styles.planTier, { color: colors.foreground }]}>
                  {plan.tier.toUpperCase()}
                </Text>
                <Text style={[styles.planInterval, { color: colors.mutedForeground }]}>
                  {plan.interval}LY
                </Text>
              </View>
              {rcPkg ? (
                <Text style={[styles.planPrice, { color: colors.primary }]}>
                  {rcPkg.product.priceString}
                  <Text style={[styles.planPer, { color: colors.mutedForeground }]}>
                    {" "}
                    / {plan.interval}
                  </Text>
                </Text>
              ) : (
                <Text style={[styles.planPrice, { color: colors.primary }]}>
                  ${stripeUsd}
                  <Text style={[styles.planPer, { color: colors.mutedForeground }]}>
                    {" "}
                    / {plan.interval}
                  </Text>
                </Text>
              )}

              {rcAvailable ? (
                <Pressable
                  onPress={() => purchaseRC(productId)}
                  disabled={isBusy}
                  style={({ pressed }) => [
                    styles.btnPrimary,
                    {
                      backgroundColor: colors.primary,
                      opacity: isBusy ? 0.5 : pressed ? 0.85 : 1,
                    },
                  ]}
                >
                  {isBusy ? (
                    <ActivityIndicator color={colors.primaryForeground} />
                  ) : (
                    <Text style={[styles.btnPrimaryText, { color: colors.primaryForeground }]}>
                      SUBSCRIBE
                    </Text>
                  )}
                </Pressable>
              ) : stripeAvailable ? (
                <Pressable
                  onPress={() => openCheckout(plan.price_id!)}
                  disabled={isBusy}
                  style={({ pressed }) => [
                    styles.btnPrimary,
                    {
                      backgroundColor: colors.primary,
                      opacity: isBusy ? 0.5 : pressed ? 0.85 : 1,
                    },
                  ]}
                >
                  {isBusy ? (
                    <ActivityIndicator color={colors.primaryForeground} />
                  ) : (
                    <Text style={[styles.btnPrimaryText, { color: colors.primaryForeground }]}>
                      CHECKOUT (WEB)
                    </Text>
                  )}
                </Pressable>
              ) : (
                <Pressable
                  disabled
                  style={[styles.btnGhost, { borderColor: colors.border, opacity: 0.5 }]}
                >
                  <Text style={[styles.btnGhostText, { color: colors.mutedForeground }]}>
                    UNAVAILABLE
                  </Text>
                </Pressable>
              )}
            </Card>
          );
        })
      )}

      <View style={{ height: 16 }} />
      <Pressable
        onPress={() => { void signOut(); }}
        style={({ pressed }) => [
          styles.btnGhost,
          { borderColor: colors.border, opacity: pressed ? 0.7 : 1 },
        ]}
      >
        <Text style={[styles.btnGhostText, { color: colors.destructive }]}>SIGN OUT</Text>
      </Pressable>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { padding: 16, gap: 8 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  tier: { fontSize: 26, fontFamily: "Inter_700Bold", letterSpacing: 1.5, marginBottom: 8 },
  muted: { fontSize: 12, fontFamily: "Inter_400Regular", marginTop: 2 },
  planCard: { gap: 12 },
  planHead: { flexDirection: "row", justifyContent: "space-between", alignItems: "baseline" },
  planTier: { fontSize: 18, fontFamily: "Inter_700Bold", letterSpacing: 1 },
  planInterval: { fontSize: 11, letterSpacing: 1.5, fontFamily: "Inter_500Medium" },
  planPrice: { fontSize: 22, fontFamily: "Inter_700Bold" },
  planPriceMuted: { fontSize: 13, fontFamily: "Inter_400Regular" },
  planPer: { fontSize: 12, fontFamily: "Inter_400Regular" },
  btnPrimary: { paddingVertical: 14, alignItems: "center" },
  btnPrimaryText: { fontSize: 12, letterSpacing: 2, fontFamily: "Inter_700Bold" },
  btnGhost: {
    paddingVertical: 14,
    alignItems: "center",
    borderWidth: 1,
    marginTop: 12,
  },
  btnGhostText: { fontSize: 12, letterSpacing: 2, fontFamily: "Inter_600SemiBold" },
});
