# Google Reviews Integration — Design

## Goal

Replace the site's own review-writing feature on `/reviews` with real reviews
pulled from the farm's Google Business Profile, so visitors see genuine
Google reviews instead of (or in addition to) site-submitted ones.

## Background / current state

- `SiteReview` (homePage/models.py:482) stores user-submitted reviews
  (name, rating, comment). The `/reviews` view (`site_reviews`,
  homePage/views/reviews.py:96) currently redirects straight to `home` —
  the review page is already disabled.
- The home page also has a "latest reviews" widget (home.html:165) driven by
  `SiteReview`, already hidden behind a `{% comment %}` block. **Out of
  scope** — stays hidden, untouched by this work.
- `settings.py` already has unused `GOOGLE_PLACE_ID` / `GOOGLE_PLACES_API_KEY`
  scaffolding for the (rejected) Places API approach. These stay unused;
  not removed, not wired up.
- The user owns the Google Business Profile listing and already has an
  approved Google Cloud project with Business Profile APIs (reviews scope)
  enabled, plus an OAuth client id/secret for it.
- `requests==2.32.5` and `python-dotenv` are already in requirements.txt —
  no new dependency needed for HTTP calls to Google's OAuth/API endpoints.
- Existing precedent for scheduled sync jobs: `homePage/management/commands/
  sync_horse_capacity.py` and `send_feedback_requests.py`, run via Render's
  cron job feature (see README "Management Commands" section).

## Decisions made with the user

1. API choice: **Google Business Profile API** (OAuth, client id/secret),
   not Google Places API — needed to get *all* reviews, not just up to 5.
   User confirmed the API is already enabled and access-approved in her
   Google Cloud project.
2. Display location: the existing `/reviews` page only.
3. Full replacement: the "leave a review" form and `SiteReview` display are
   both removed from `/reviews`. `SiteReview` rows stay in the database
   (not deleted, not migrated) — just no longer shown or writable there.
4. Average rating shown on `/reviews` is computed from Google reviews only.
5. Secrets discipline: client_id/client_secret/refresh_token are never
   pasted into chat and never read/written by the assistant — the user
   copies command output into `.env` herself.

## Data model

New model `GoogleReview` in `homePage/models.py`, alongside `SiteReview`:

```python
class GoogleReview(models.Model):
    google_review_id = models.CharField(max_length=255, unique=True)  # API "name" resource path
    reviewer_name = models.CharField(max_length=120, blank=True)
    reviewer_photo_url = models.URLField(blank=True)
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    reply_comment = models.TextField(blank=True)  # owner's reply, if any
    create_time = models.DateTimeField()   # from Google, when the review was written
    update_time = models.DateTimeField()   # from Google, last edit
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-create_time']
```

Registered read-only in `admin.py` (list display only, no add/edit — data
is owned by Google and overwritten on every sync).

## One-time OAuth setup

New management command `setup_google_reviews_auth`:

- Reads `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` from env
  (user adds these to `.env` herself before running it).
- Builds the Google OAuth consent URL (scope
  `https://www.googleapis.com/auth/business.manage`,
  `redirect_uri=http://localhost:8765/callback`, `access_type=offline`,
  `prompt=consent` to force a refresh token on repeat runs).
- Starts a short-lived local HTTP server on `localhost:8765`, prints the
  consent URL for the user to open in her own browser and approve as the
  profile owner, and captures the `code` query param from the redirect.
- Exchanges the code for tokens at `https://oauth2.googleapis.com/token`.
- Immediately uses the resulting access token to call
  `mybusinessaccountmanagement.googleapis.com/v1/accounts` and
  `mybusinessbusinessinformation.googleapis.com/v1/accounts/{account}/locations`
  to resolve account/location IDs (there's usually exactly one of each for
  a single-location business — if more than one comes back, print all and
  let the user pick).
- Prints `GOOGLE_OAUTH_REFRESH_TOKEN`, `GOOGLE_BUSINESS_ACCOUNT_ID`,
  `GOOGLE_BUSINESS_LOCATION_ID` to stdout for the user to paste into `.env`.
  Never writes to `.env` itself, never logs these values anywhere else.
- Run once, locally, by the user — not part of the deploy pipeline.

## Ongoing sync

New management command `sync_google_reviews` (same shape as
`sync_horse_capacity.py`):

- Reads `GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN` and
  `GOOGLE_BUSINESS_ACCOUNT_ID` / `GOOGLE_BUSINESS_LOCATION_ID` from env.
- POSTs to `https://oauth2.googleapis.com/token` with
  `grant_type=refresh_token` to get a short-lived access token.
- GETs (paginated via `pageToken`)
  `https://mybusiness.googleapis.com/v4/accounts/{account}/locations/{location}/reviews`.
- Maps Google's `starRating` enum (`ONE`..`FIVE`) to `1`-`5`.
- Upserts each review into `GoogleReview` by `google_review_id`
  (`update_or_create`). No deletion of local rows that vanish remotely —
  reviews essentially never get deleted on Google's side; not worth the
  extra API complexity to handle it.
- Logs a summary count (created/updated) via the existing `logging` pattern
  used elsewhere in the app.

Scheduled via a Render cron job, same mechanism as `send_feedback_requests`
(documented in README's "Management Commands" section) — e.g. once daily.

## View / template changes

`homePage/views/reviews.py` — `site_reviews`:
- Remove the early `return redirect('home')`.
- Remove all `SiteReviewForm` / POST handling and rate-limiting tied to
  submission (the `@ratelimit` decorator was guarding POST submission —
  drop it along with the POST branch).
- Query `GoogleReview.objects.all()` (already ordered by `-create_time`),
  paginate the same way (`Paginator`, 10/page).
- Compute `rating_avg` / `rating_count` from `GoogleReview` instead of
  `SiteReview`.

`homePage/templates/homePage/site_reviews.html`:
- Remove the `#sms_review` review-form section entirely (lines 63-120) and
  its `focus_rating_error` script block.
- Keep `.reviews-grid` / `.review-card` markup as-is, swap the loop variable
  source to `GoogleReview` fields (`reviewer_name`, `rating`, `comment`,
  `create_time`). Optionally show `reply_comment` under the comment if
  present (small addition, reuses existing card styling — no new CSS
  classes needed beyond maybe one for the reply block).
- No changes to `reviews_page.css` beyond what's needed for the (optional)
  reply block; the goal is zero unnecessary visual change per the site's
  established minimal-visual-change practice.

## Settings / config

`settings.py` additions (same `os.getenv` pattern as existing entries):

```python
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")
GOOGLE_BUSINESS_ACCOUNT_ID = os.getenv("GOOGLE_BUSINESS_ACCOUNT_ID", "")
GOOGLE_BUSINESS_LOCATION_ID = os.getenv("GOOGLE_BUSINESS_LOCATION_ID", "")
```

README updated: new env vars documented in the existing env-var table, and
`sync_google_reviews` documented in "Management Commands" alongside
`send_feedback_requests`, including the note to schedule it as a Render
cron job.

## Error handling

- `sync_google_reviews`: if the refresh-token exchange or the reviews GET
  fails (network error, revoked token, 4xx/5xx), log the error and exit
  non-zero — no partial silent failure, no fallback data. Render's cron
  job surfaces a failed run the same way it does for the existing sync
  commands.
- View: if `GoogleReview` table is empty (first deploy before the first
  sync has run), template already has an empty-state branch
  (`{% empty %}` → "אין עדיין ביקורות.") — no special-casing needed.

## Testing

- One `test_*.py` (per repo's existing `homePage/tests/` convention) for
  `sync_google_reviews`: mock the two HTTP calls (token refresh + reviews
  list) with `unittest.mock`, assert `GoogleReview` rows get created and
  that a second run with the same payload doesn't duplicate them
  (`update_or_create` idempotency).
- No test for `setup_google_reviews_auth` — it's a one-time interactive
  local tool, not part of the app's runtime behavior.

## Out of scope

- Home page reviews widget (stays hidden/commented out).
- Deleting `SiteReview` model, data, or admin registration.
- Removing the unused `GOOGLE_PLACE_ID` / `GOOGLE_PLACES_API_KEY` settings.
- Handling reviews being deleted on Google's side.
- Any UI for replying to reviews from the site (replies are read-only,
  synced from whatever the owner posts via Google directly).
