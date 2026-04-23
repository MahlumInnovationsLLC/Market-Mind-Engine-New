# RevenueCat sandbox QA runbook

Genuine end-to-end tests of the RevenueCat purchase, restore and
receipt-validation flows can only be performed on real iOS and Android
devices behind TestFlight + Google Play Internal Testing. They cannot
run from inside Replit because:

* the App Store / Play Store sandboxes only honour StoreKit / Billing
  Library calls coming from a signed, store-distributed build;
* purchase + restore require a sandbox tester / license tester that is
  signed into the device's Apple ID / Google account;
* webhook deliveries originate from RevenueCat's edge and need a
  publicly reachable api-server URL.

The webhook / entitlement-plumbing path is covered automatically by
`artifacts/api-server/tests/test_rc_billing.py`. **Everything below is
the manual checklist that must be run after every release that touches
billing, the mobile entitlement code, or the RevenueCat dashboard
configuration.**

---

## 0. Prerequisites

Done once per environment:

1. **App Store Connect**
   - A sandbox tester created in *Users and Access → Sandbox Testers*
     with an email that does **not** match a real Apple ID.
   - A TestFlight build uploaded with the `pro_monthly`, `pro_annual`,
     `elite_monthly`, `elite_annual` non-consumable subscription
     products approved as "Ready to Submit".
2. **Google Play Console**
   - An Internal Testing track with the build signed by the upload key.
   - A license tester added under *Setup → License testing*.
   - The four subscription products (`pro_monthly`, `pro_annual`,
     `elite_monthly`, `elite_annual`) created and Active.
3. **RevenueCat dashboard**
   - Both `pro` and `elite` *entitlements* exist.
   - Each store product is attached to the corresponding entitlement
     (e.g. `pro_monthly` → `pro`, `elite_annual` → `elite`).
   - Apple App Store and Google Play app configurations point at the
     correct bundle id / package name and have valid in-app purchase
     keys uploaded.
   - The webhook is configured at `https://<api-server-host>/api/billing/revenuecat-webhook`
     with the `Authorization` header value matching the `REVENUECAT_WEBHOOK_AUTH_HEADER`
     env var on the api-server.
4. **Mobile env**
   - `EXPO_PUBLIC_REVENUECAT_APPLE_KEY` and
     `EXPO_PUBLIC_REVENUECAT_GOOGLE_KEY` set in the Expo build profile.
5. **Founder allow-list**
   - The Clerk user being used for the test is **not** in
     `STRIPE_FOUNDER_USERS` or `OPERATOR_ALLOWED_EMAILS` (otherwise
     the founder bypass masks the purchase result).

---

## 1. iOS sandbox purchase (TestFlight)

1. Install the latest TestFlight build on a physical iOS device.
2. *Settings → App Store → Sandbox Account* — sign in with the sandbox
   tester from step 0.1.
3. Open the app, sign in to a Clerk account whose tier is currently
   `free` (verify on the *Subscription* tab — should read `FREE`).
4. Tap **Subscribe** under the *Pro · monthly* card.
5. Complete the StoreKit prompt with the sandbox password.
6. **Expect within ~10 seconds:**
   - The subscription screen refreshes and shows `PRO`, status `active`.
   - The *Manage Subscription* button is **not** shown (the stored
     subscription id is `rc:…` so the Stripe portal link is hidden).
7. **Server-side check** (api-server logs):
   ```
   [rc_billing] event INITIAL_PURCHASE for app_user_id=user_…
   ```
   And:
   ```sql
   SELECT tier, status, stripe_subscription_id
   FROM subscriptions WHERE user_id = '<clerk_user_id>';
   -- → tier=pro, status=active, stripe_subscription_id LIKE 'rc:%'
   ```

## 2. Android sandbox purchase (Internal Testing)

Same as §1, but with the Internal Testing build and a license tester
account signed into the Play Store on the test device. The webhook
event `store` field should be `PLAY_STORE`.

## 3. Restore-purchases (sign-out / sign-in)

This is the path the deferred E2E test originally targeted.

1. Starting from a device that already completed §1 (Clerk user A is
   `pro`).
2. On the *Subscription* tab, tap **Sign out**.
3. Sign in as a **different** Clerk user B that is currently `free`.
4. **Expect:**
   - Within ~10 seconds the screen renders `PRO` for user B (RC fires
     a `TRANSFER` event tagging user B as the new `app_user_id`; the
     api-server upserts the entitlement onto B's row).
   - The api-server log shows a `TRANSFER` webhook delivery.
5. Sign back in as user A and confirm A's tier:
   - On iOS: A typically goes to `free` because the App Store now
     associates the receipt with B. A will see *RESTORE PURCHASES* if
     they re-purchase or restore from Settings.
   - On Android: same semantics via Play Billing.
6. **Negative test:** sign in as a third user C with no purchase
   history and confirm tier remains `free` and no RC webhook fires.

## 4. Renewal in sandbox

Sandbox subscriptions auto-renew on accelerated timelines (5 min for a
1-month product on iOS sandbox; similar on Play). Leave the device
signed in and verify after one renewal:

* api-server log shows `RENEWAL`.
* `current_period_end` in the `subscriptions` row advances.
* Tier stays `pro`.

## 5. Cancellation + grace period

1. iOS: *Settings → Apple ID → Subscriptions → Axiom* → Cancel.
   Android: Play Store → *Manage subscriptions* → Cancel.
2. **Expect:**
   - Webhook `CANCELLATION` arrives.
   - `subscriptions.cancel_at_period_end = true`, `tier` remains `pro`,
     `status` remains `active` until the period end.
3. Wait for sandbox expiration (a few minutes on iOS):
   - Webhook `EXPIRATION` arrives.
   - `tier` flips to `free`, `status` to `inactive`.
   - The mobile UI re-renders the upgrade CTAs.

## 6. Billing issue (grace period)

Hard to force in sandbox; if reproducible via expired sandbox payment
method, verify:
* Webhook `BILLING_ISSUE` arrives.
* `status` becomes `past_due`, `tier` stays `pro` (paid features stay
  unlocked during grace).

## 7. Idempotent re-delivery

Use the RevenueCat dashboard's *Resend* button on a recent event:
* The api-server returns `{"status": "duplicate", "event_id": …}`.
* No additional rows are inserted — verify by snapshotting the row's
  `updated_at` before and after.

---

## Sign-off

After all sections pass on both iOS and Android, attach screenshots of
the *Subscription* screen in `pro`, `cancel_at_period_end=true`, and
`free` (post-expiration) states to the release ticket and mark this
runbook as complete for the release.
