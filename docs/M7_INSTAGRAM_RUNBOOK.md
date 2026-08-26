# M7 — Instagram Graph runbook (CUT-61)

Code-side M7 (CUT-56…60) is done and offline-tested. This doc covers the part that
isn't code: getting real Instagram publishing credentials. **Owner action required —
start this early, it's the weeks-long step.**

## 1. Meta app + Instagram Graph product

1. Create (or reuse) an app at [developers.facebook.com](https://developers.facebook.com/apps).
2. Add the **Instagram Graph API** product to the app.
3. Convert the target Instagram account to a **Business** or **Creator** account
   (Instagram app → Settings → Account type).
4. Link that IG account to a **Facebook Page** you admin (required by Graph even
   though publishing itself is IG-only).

## 2. Permissions + App Review

Scopes needed for single-image feed publishing:

- `instagram_basic`
- `instagram_content_publish`
- `pages_show_list`
- `pages_read_engagement`

These require **Business Verification** (confirms who owns the app) and **App
Review** (Meta reviews the actual use case) before they work outside the app's own
Meta-account testers. In the App Review submission:

- Describe the use case as: an owner-approved, human-in-the-loop content pipeline
  that publishes single-image feed posts to the business's own linked Instagram
  account (no third-party accounts, no scraping).
- Attach a short screen recording of the Telegram approve → publish flow if asked
  for a demo.

While waiting on review, the app's Meta-account testers (added under App Roles) can
already publish for QA — enough to run the M7 verification steps below.

## 3. Long-lived access token

1. Generate a **short-lived** user token via the Graph API Explorer with the scopes
   above, for a user with admin access to the linked Page.
2. Exchange it for a **long-lived token** (~60 days):
   ```
   GET https://graph.facebook.com/{graph_version}/oauth/access_token
     ?grant_type=fb_exchange_token
     &client_id=<app_id>
     &client_secret=<app_secret>
     &fb_exchange_token=<short_lived_token>
   ```
3. Set it as `INSTAGRAM_ACCESS_TOKEN` in Railway (`railway variables --set`).
4. **Manual refresh procedure** (automated refresh is a follow-up, not M7): before
   the ~60-day expiry, repeat the exchange call using the current long-lived token
   as `fb_exchange_token` (this resets its 60-day clock), then update the Railway
   variable and redeploy.

## 4. Find the IG user id

```
GET https://graph.facebook.com/{graph_version}/me/accounts?access_token=<token>
# -> Page id, then:
GET https://graph.facebook.com/{graph_version}/{page_id}?fields=instagram_business_account&access_token=<token>
# -> {"instagram_business_account": {"id": "<INSTAGRAM_IG_USER_ID>"}}
```

## 5. Deploy + prod smoke (against a **test** IG account first)

1. Set `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_IG_USER_ID`, `PUBLIC_BASE_URL` in Railway.
2. `railway up` — confirm `alembic upgrade head` applies migration `0004`.
3. Approve a post via Telegram; at the next posting slot, confirm on the test IG
   account: image + bilingual caption + disclaimer live, and the `posts` row goes
   `approved → publishing → published` with `ig_media_id` set.
4. Kill the Railway process mid-publish (or redeploy right after `/dev/postnow` on a
   test post) and confirm on restart: no double-post, and `recover_orphaned` logs
   show the correct outcome (see `app/pipeline/publish.py::_recover_one`).
5. Only after a clean test-account run, point `INSTAGRAM_IG_USER_ID` at the real
   production account.
