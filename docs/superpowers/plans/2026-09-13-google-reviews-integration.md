# Google Reviews Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the site-submitted review form on `/reviews` with real reviews synced from the farm's Google Business Profile via OAuth.

**Architecture:** A new `GoogleReview` model stores reviews pulled from Google's Business Profile API. A pure-function service module (`homePage/services/google_reviews.py`) wraps the raw HTTP calls (token refresh, pagination, discovery). Two management commands sit on top of it: a one-time interactive OAuth setup command, and a recurring sync command (same shape as the existing `sync_horse_capacity`/`send_feedback_requests` commands, scheduled via Render cron). The `/reviews` view and template are rewired to read `GoogleReview` instead of `SiteReview`, and the submission form is removed.

**Tech Stack:** Django 5.2 (existing app: `homePage`), `requests` (already in requirements.txt) for HTTP calls — no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-13-google-reviews-integration-design.md`

## Global Constraints

- No new pip dependencies — use `requests` (already installed) for all HTTP calls to Google's OAuth/API endpoints.
- Never write client_id/client_secret/refresh_token to logs, stdout beyond the one-time setup command's explicit "copy this to .env" output, or into any file the assistant writes on the user's behalf. The user copies printed values into `.env` herself.
- Follow the existing `os.getenv("NAME", default)` pattern in `mesilat_zion_farm_website/settings.py` for all new settings.
- Follow the existing management-command style in `homePage/management/commands/` (`BaseCommand`, `self.stdout.write(...)` for output, no `logging` module).
- `SiteReview` model, its admin registration, and its data are NOT touched or deleted — only the `/reviews` view/template stop reading/writing it.
- Tests use Django's `TestCase`/`SimpleTestCase` per the existing convention in `homePage/tests/`, run via `python manage.py test`.
- Hebrew is the UI/help-text language throughout, matching every existing string in this codebase.

---

### Task 1: `GoogleReview` model + admin + migration

**Files:**
- Modify: `homePage/models.py` (insert after the `SiteReview` class, homePage/models.py:497)
- Modify: `homePage/admin.py` (imports at homePage/admin.py:3-8; new admin class near `SiteReviewAdmin` at homePage/admin.py:3299)
- Test: `homePage/tests/test_models.py`
- Create: `homePage/migrations/0059_googlereview.py` (auto-generated, not hand-written)

**Interfaces:**
- Produces: `GoogleReview` model with fields `google_review_id` (unique str), `reviewer_name`, `reviewer_photo_url`, `rating` (int 1-5), `comment`, `reply_comment`, `create_time` (datetime), `update_time` (datetime), `synced_at` (datetime, auto). Ordered `-create_time`. Later tasks (2, 3, 6) import this from `homePage.models`.

- [ ] **Step 1: Write the failing test**

In `homePage/tests/test_models.py`, change the models import at the top of the file from:

```python
from homePage.models import (
    Activity, Appointment, Booking,
    SiteReview, TreatmentSession, CustomSchedule,
)
```

to:

```python
from homePage.models import (
    Activity, Appointment, Booking,
    SiteReview, TreatmentSession, CustomSchedule, GoogleReview,
)
```

Then add this test class (near the other model tests, using the same `TestCase` style already in that file):

```python
class GoogleReviewDefaultsTest(TestCase):
    def test_str_and_ordering(self):
        older = GoogleReview.objects.create(
            google_review_id="accounts/1/locations/2/reviews/old",
            reviewer_name="דנה",
            rating=4,
            comment="נחמד",
            create_time=timezone.now() - timedelta(days=1),
            update_time=timezone.now() - timedelta(days=1),
        )
        newer = GoogleReview.objects.create(
            google_review_id="accounts/1/locations/2/reviews/new",
            reviewer_name="",
            rating=5,
            comment="מעולה",
            create_time=timezone.now(),
            update_time=timezone.now(),
        )
        self.assertEqual(str(newer), "אנונימי (5★)")
        self.assertEqual(list(GoogleReview.objects.all()), [newer, older])

    def test_google_review_id_is_unique(self):
        GoogleReview.objects.create(
            google_review_id="dup",
            rating=5,
            create_time=timezone.now(),
            update_time=timezone.now(),
        )
        with self.assertRaises(Exception):
            GoogleReview.objects.create(
                google_review_id="dup",
                rating=3,
                create_time=timezone.now(),
                update_time=timezone.now(),
            )
```

(`timezone` and `timedelta` are already imported at the top of `test_models.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test homePage.tests.test_models.GoogleReviewDefaultsTest -v 2`
Expected: FAIL — `ImportError: cannot import name 'GoogleReview'`

- [ ] **Step 3: Add the model**

In `homePage/models.py`, insert immediately after the `SiteReview` class (after homePage/models.py:497, before `class CancellationRequest(models.Model):`):

```python
class GoogleReview(models.Model):                             # תגובה שסונכרנה מ-Google Business Profile
    google_review_id = models.CharField("מזהה ביקורת בגוגל", max_length=255, unique=True)
    reviewer_name = models.CharField("שם המגיב", max_length=120, blank=True)
    reviewer_photo_url = models.URLField("תמונת המגיב", blank=True)
    rating = models.PositiveSmallIntegerField("דירוג", validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField("תגובה", blank=True)
    reply_comment = models.TextField("תשובת החווה", blank=True)
    create_time = models.DateTimeField("נכתב ב-")
    update_time = models.DateTimeField("עודכן ב-")
    synced_at = models.DateTimeField("סונכרן ב-", auto_now=True)

    class Meta:
        ordering = ['-create_time']
        verbose_name = "ביקורת גוגל"
        verbose_name_plural = "ביקורות גוגל"

    def __str__(self):
        who = self.reviewer_name or "אנונימי"
        return f"{who} ({self.rating}★)"
```

- [ ] **Step 4: Generate and apply the migration**

Run: `python manage.py makemigrations homePage`
Expected: creates `homePage/migrations/0059_googlereview.py` (or next free number if others were added meanwhile).

Run: `python manage.py migrate`
Expected: `Applying homePage.0059_googlereview... OK`

- [ ] **Step 5: Run test to verify it passes**

Run: `python manage.py test homePage.tests.test_models.GoogleReviewDefaultsTest -v 2`
Expected: PASS (2 tests)

- [ ] **Step 6: Register read-only in admin**

In `homePage/admin.py`, add `GoogleReview` to the models import at the top (homePage/admin.py:3-8):

```python
from .models import (
    Activity, Appointment, CustomSchedule, Booking, SiteReview, GoogleReview,
    CancellationRequest, TermsConsent, ScheduleBoard, Weekday,
    BusinessHours, ActivityRule, Instructor, TreatmentSession,
    MonthlySummary, Payment, Receipt, PhoneOnlyDate,
)
```

Add a new admin class right after `SiteReviewAdmin` (homePage/admin.py:3299-3304):

```python
@admin.register(GoogleReview)
class GoogleReviewAdmin(admin.ModelAdmin):
    list_display = ('reviewer_name', 'rating', 'create_time', 'synced_at')
    list_filter = ('rating',)
    search_fields = ('reviewer_name', 'comment')
    ordering = ('-create_time',)

    def has_add_permission(self, request):
        return False  # התוכן מגיע רק מסנכרון עם גוגל

    def has_change_permission(self, request, obj=None):
        return False
```

- [ ] **Step 7: Verify admin loads**

Run: `python manage.py check`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 8: Commit**

```bash
git add homePage/models.py homePage/admin.py homePage/migrations/0059_googlereview.py homePage/tests/test_models.py
git commit -m "Add GoogleReview model, admin registration, and migration"
```

---

### Task 2: Google Business Profile API service module

**Files:**
- Create: `homePage/services/google_reviews.py`
- Test: `homePage/tests/test_google_reviews_service.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure HTTP wrapper module, no Django models).
- Produces (used by Tasks 3 and 4): `star_rating_to_int(value: str) -> int`, `exchange_code_for_tokens(client_id, client_secret, code, redirect_uri) -> dict`, `refresh_access_token(client_id, client_secret, refresh_token) -> str`, `list_accounts(access_token) -> list[dict]`, `list_locations(access_token, account_id) -> list[dict]`, `fetch_all_reviews(access_token, account_id, location_id) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

Create `homePage/tests/test_google_reviews_service.py`:

```python
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase

from homePage.services.google_reviews import (
    star_rating_to_int,
    refresh_access_token,
    fetch_all_reviews,
)


class StarRatingMappingTest(SimpleTestCase):
    def test_known_values(self):
        self.assertEqual(star_rating_to_int("ONE"), 1)
        self.assertEqual(star_rating_to_int("FIVE"), 5)

    def test_unknown_value_defaults_to_five(self):
        self.assertEqual(star_rating_to_int("STAR_RATING_UNSPECIFIED"), 5)


class RefreshAccessTokenTest(SimpleTestCase):
    @patch("homePage.services.google_reviews.requests.post")
    def test_returns_access_token(self, mock_post):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"access_token": "tok123"}
        mock_post.return_value = resp

        token = refresh_access_token("cid", "csecret", "rtoken")

        self.assertEqual(token, "tok123")


class FetchAllReviewsTest(SimpleTestCase):
    @patch("homePage.services.google_reviews.requests.get")
    def test_follows_pagination(self, mock_get):
        page1 = MagicMock()
        page1.raise_for_status = lambda: None
        page1.json = lambda: {"reviews": [{"name": "r1"}], "nextPageToken": "p2"}

        page2 = MagicMock()
        page2.raise_for_status = lambda: None
        page2.json = lambda: {"reviews": [{"name": "r2"}]}

        mock_get.side_effect = [page1, page2]

        reviews = fetch_all_reviews("tok", "acc1", "loc1")

        self.assertEqual([r["name"] for r in reviews], ["r1", "r2"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python manage.py test homePage.tests.test_google_reviews_service -v 2`
Expected: FAIL — `ModuleNotFoundError: No module named 'homePage.services.google_reviews'`

- [ ] **Step 3: Write the service module**

Create `homePage/services/google_reviews.py`:

```python
import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_URL_TMPL = "https://mybusinessbusinessinformation.googleapis.com/v1/accounts/{account_id}/locations"
REVIEWS_URL_TMPL = "https://mybusiness.googleapis.com/v4/accounts/{account_id}/locations/{location_id}/reviews"

STAR_RATING_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def star_rating_to_int(value: str) -> int:
    """ממפה את ה-enum של גוגל (ONE..FIVE) למספר; ברירת מחדל 5 אם הערך לא מוכר."""
    return STAR_RATING_MAP.get(value, 5)


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def list_accounts(access_token: str) -> list:
    resp = requests.get(
        ACCOUNTS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("accounts", [])


def list_locations(access_token: str, account_id: str) -> list:
    url = LOCATIONS_URL_TMPL.format(account_id=account_id)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"readMask": "name,title"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("locations", [])


def fetch_all_reviews(access_token: str, account_id: str, location_id: str) -> list:
    url = REVIEWS_URL_TMPL.format(account_id=account_id, location_id=location_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    reviews = []
    page_token = None
    while True:
        params = {"pageToken": page_token} if page_token else {}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        reviews.extend(data.get("reviews", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return reviews
```

Also create `homePage/services/__init__.py` if it doesn't already exist (check first — `homePage/services/` already exists per `receipts.py`/`ntfy_gateway.py`/`slot_hold.py`, so it should already have one; no action needed if present).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python manage.py test homePage.tests.test_google_reviews_service -v 2`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add homePage/services/google_reviews.py homePage/tests/test_google_reviews_service.py
git commit -m "Add Google Business Profile API service module (OAuth + reviews fetch)"
```

---

### Task 3: `sync_google_reviews` management command

**Files:**
- Create: `homePage/management/commands/sync_google_reviews.py`
- Test: `homePage/tests/test_sync_google_reviews.py`

**Interfaces:**
- Consumes: `homePage.services.google_reviews.refresh_access_token`, `fetch_all_reviews`, `star_rating_to_int` (Task 2); `homePage.models.GoogleReview` (Task 1); settings `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`, `GOOGLE_BUSINESS_ACCOUNT_ID`, `GOOGLE_BUSINESS_LOCATION_ID` (Task 5 adds these to settings.py, but the command reads them via `getattr(settings, ..., "")` so it works before Task 5 lands too).
- Produces: `python manage.py sync_google_reviews` — upserts `GoogleReview` rows.

- [ ] **Step 1: Write the failing tests**

Create `homePage/tests/test_sync_google_reviews.py`:

```python
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from homePage.models import GoogleReview

FAKE_REVIEWS = [
    {
        "name": "accounts/1/locations/2/reviews/abc",
        "reviewer": {"displayName": "דנה", "profilePhotoUrl": ""},
        "starRating": "FIVE",
        "comment": "חוויה מדהימה",
        "createTime": "2026-01-01T10:00:00Z",
        "updateTime": "2026-01-01T10:00:00Z",
    },
]

ENV = dict(
    GOOGLE_OAUTH_CLIENT_ID="cid",
    GOOGLE_OAUTH_CLIENT_SECRET="csecret",
    GOOGLE_OAUTH_REFRESH_TOKEN="rtoken",
    GOOGLE_BUSINESS_ACCOUNT_ID="1",
    GOOGLE_BUSINESS_LOCATION_ID="2",
)


@override_settings(**ENV)
class SyncGoogleReviewsTest(TestCase):
    @patch("homePage.management.commands.sync_google_reviews.fetch_all_reviews")
    @patch("homePage.management.commands.sync_google_reviews.refresh_access_token")
    def test_creates_reviews(self, mock_refresh, mock_fetch):
        mock_refresh.return_value = "tok"
        mock_fetch.return_value = FAKE_REVIEWS

        call_command("sync_google_reviews")

        self.assertEqual(GoogleReview.objects.count(), 1)
        review = GoogleReview.objects.get()
        self.assertEqual(review.reviewer_name, "דנה")
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.comment, "חוויה מדהימה")

    @patch("homePage.management.commands.sync_google_reviews.fetch_all_reviews")
    @patch("homePage.management.commands.sync_google_reviews.refresh_access_token")
    def test_second_run_does_not_duplicate(self, mock_refresh, mock_fetch):
        mock_refresh.return_value = "tok"
        mock_fetch.return_value = FAKE_REVIEWS

        call_command("sync_google_reviews")
        call_command("sync_google_reviews")

        self.assertEqual(GoogleReview.objects.count(), 1)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="")
    def test_errors_when_config_missing(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("sync_google_reviews")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python manage.py test homePage.tests.test_sync_google_reviews -v 2`
Expected: FAIL — `ModuleNotFoundError` (command doesn't exist yet)

- [ ] **Step 3: Write the command**

Create `homePage/management/commands/sync_google_reviews.py`:

```python
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from homePage.models import GoogleReview
from homePage.services.google_reviews import (
    refresh_access_token,
    fetch_all_reviews,
    star_rating_to_int,
)


class Command(BaseCommand):
    help = "מסנכרן תגובות מ-Google Business Profile API לתוך GoogleReview"

    def handle(self, *args, **options):
        client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
        client_secret = getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")
        refresh_token = getattr(settings, "GOOGLE_OAUTH_REFRESH_TOKEN", "")
        account_id = getattr(settings, "GOOGLE_BUSINESS_ACCOUNT_ID", "")
        location_id = getattr(settings, "GOOGLE_BUSINESS_LOCATION_ID", "")

        missing = [name for name, val in [
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
            ("GOOGLE_BUSINESS_ACCOUNT_ID", account_id),
            ("GOOGLE_BUSINESS_LOCATION_ID", location_id),
        ] if not val]
        if missing:
            raise CommandError(f"חסרים משתני סביבה: {', '.join(missing)}")

        access_token = refresh_access_token(client_id, client_secret, refresh_token)
        reviews = fetch_all_reviews(access_token, account_id, location_id)

        created = updated = 0
        for r in reviews:
            reviewer = r.get("reviewer", {})
            reply = r.get("reviewReply", {})
            defaults = {
                "reviewer_name": reviewer.get("displayName", ""),
                "reviewer_photo_url": reviewer.get("profilePhotoUrl", ""),
                "rating": star_rating_to_int(r.get("starRating", "")),
                "comment": r.get("comment", ""),
                "reply_comment": reply.get("comment", ""),
                "create_time": parse_datetime(r["createTime"]),
                "update_time": parse_datetime(r["updateTime"]),
            }
            _, was_created = GoogleReview.objects.update_or_create(
                google_review_id=r["name"], defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(f"סונכרנו {len(reviews)} תגובות (חדשות: {created}, עודכנו: {updated})")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python manage.py test homePage.tests.test_sync_google_reviews -v 2`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add homePage/management/commands/sync_google_reviews.py homePage/tests/test_sync_google_reviews.py
git commit -m "Add sync_google_reviews management command"
```

---

### Task 4: `setup_google_reviews_auth` one-time OAuth command

**Files:**
- Create: `homePage/management/commands/setup_google_reviews_auth.py`
- Test: `homePage/tests/test_setup_google_reviews_auth.py`

**Interfaces:**
- Consumes: `homePage.services.google_reviews.exchange_code_for_tokens`, `list_accounts`, `list_locations` (Task 2); settings `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`.
- Produces: `python manage.py setup_google_reviews_auth` — a local, interactive, one-time command the user runs herself. Not called by any other task's code.

- [ ] **Step 1: Write the failing test**

Create `homePage/tests/test_setup_google_reviews_auth.py`:

```python
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


class SetupGoogleReviewsAuthTest(TestCase):
    @override_settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="")
    def test_errors_when_credentials_missing(self):
        with self.assertRaises(CommandError):
            call_command("setup_google_reviews_auth")
```

This is the only automated test for this command — the rest of it is an interactive local flow (opens a real browser, waits for a real redirect) that isn't meaningful to unit-test. Manual verification is covered in Step 4 below.

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test homePage.tests.test_setup_google_reviews_auth -v 2`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the command**

Create `homePage/management/commands/setup_google_reviews_auth.py`:

```python
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from homePage.services.google_reviews import (
    exchange_code_for_tokens,
    list_accounts,
    list_locations,
)

REDIRECT_URI = "http://localhost:8765/callback"
SCOPE = "https://www.googleapis.com/auth/business.manage"
AUTH_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><body>ההרשאה התקבלה, אפשר לסגור את החלון ולחזור לטרמינל.</body></html>".encode("utf-8")
        )

    def log_message(self, format, *args):
        pass  # לא להציף את הטרמינל בלוגים של השרת הזמני


class Command(BaseCommand):
    help = "הרשאה חד-פעמית מול Google (OAuth) לקבלת refresh token + מזהי חשבון/מיקום עסקי"

    def handle(self, *args, **options):
        client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
        client_secret = getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise CommandError(
                "יש להגדיר GOOGLE_OAUTH_CLIENT_ID ו-GOOGLE_OAUTH_CLIENT_SECRET ב-.env לפני הרצת הפקודה."
            )

        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"{AUTH_BASE_URL}?{urllib.parse.urlencode(params)}"

        self.stdout.write("פתחי את הכתובת הבאה בדפדפן והתחברי עם החשבון שמנהל את הפרופיל העסקי:")
        self.stdout.write(auth_url)
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        server = HTTPServer(("localhost", 8765), _CallbackHandler)
        server.auth_code = None
        self.stdout.write("ממתינה לאישור בדפדפן...")
        while server.auth_code is None:
            server.handle_request()

        code = server.auth_code
        tokens = exchange_code_for_tokens(client_id, client_secret, code, REDIRECT_URI)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise CommandError(
                "גוגל לא החזירה refresh_token. נסי שוב - ואם זה חוזר על עצמו, בטלי הרשאה קודמת "
                "ב-https://myaccount.google.com/permissions ונסי שוב."
            )

        access_token = tokens["access_token"]

        accounts = list_accounts(access_token)
        if not accounts:
            raise CommandError("לא נמצא אף חשבון עסקי מחובר למשתמש הזה.")
        if len(accounts) > 1:
            self.stdout.write("נמצאו כמה חשבונות - הפקודה בחרה את הראשון; אם זה לא הנכון, עדכני ידנית:")
            for acc in accounts:
                self.stdout.write(f"  {acc['name']} — {acc.get('accountName', '')}")
        account_id = accounts[0]["name"].split("/")[-1]

        locations = list_locations(access_token, account_id)
        if not locations:
            raise CommandError("לא נמצא אף מיקום עסקי תחת החשבון הזה.")
        if len(locations) > 1:
            self.stdout.write("נמצאו כמה מיקומים - הפקודה בחרה את הראשון; אם זה לא הנכון, עדכני ידנית:")
            for loc in locations:
                self.stdout.write(f"  {loc['name']} — {loc.get('title', '')}")
        location_id = locations[0]["name"].split("/")[-1]

        self.stdout.write("")
        self.stdout.write("הוסיפי את השורות הבאות לקובץ .env:")
        self.stdout.write(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh_token}")
        self.stdout.write(f"GOOGLE_BUSINESS_ACCOUNT_ID={account_id}")
        self.stdout.write(f"GOOGLE_BUSINESS_LOCATION_ID={location_id}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test homePage.tests.test_setup_google_reviews_auth -v 2`
Expected: PASS (1 test)

**Manual verification (the user runs this herself, not part of automated tests):**
1. Add `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` to `.env`.
2. In the Google Cloud project's OAuth client settings, add `http://localhost:8765/callback` as an authorized redirect URI.
3. Run `python manage.py setup_google_reviews_auth`, approve in the browser as the profile owner, and copy the three printed values into `.env`.

- [ ] **Step 5: Commit**

```bash
git add homePage/management/commands/setup_google_reviews_auth.py homePage/tests/test_setup_google_reviews_auth.py
git commit -m "Add one-time OAuth setup command for Google reviews"
```

---

### Task 5: Settings and README documentation

**Files:**
- Modify: `mesilat_zion_farm_website/settings.py:29` (after the existing `GOOGLE_PLACES_API_KEY` line)
- Modify: `README.md` (env var table ~line 191, Database Models table ~line 215, API Endpoints table ~line 248, Deployment checklist ~line 286, Management Commands section ~line 290-297)

**Interfaces:**
- Produces: `settings.GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`, `GOOGLE_BUSINESS_ACCOUNT_ID`, `GOOGLE_BUSINESS_LOCATION_ID` — consumed by Tasks 3 and 4 (which already work via `getattr(settings, ..., "")` even before this task, but this task makes them real Django settings with the standard `.env` loading behavior).

- [ ] **Step 1: Add settings**

In `mesilat_zion_farm_website/settings.py`, after line 29 (`GOOGLE_PLACES_API_KEY = os.getenv(...)`):

```python
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")
GOOGLE_BUSINESS_ACCOUNT_ID = os.getenv("GOOGLE_BUSINESS_ACCOUNT_ID", "")
GOOGLE_BUSINESS_LOCATION_ID = os.getenv("GOOGLE_BUSINESS_LOCATION_ID", "")
```

- [ ] **Step 2: Verify settings load**

Run: `python manage.py check`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 3: Update README env var table**

In `README.md`, after the `GOOGLE_PLACES_API_KEY` row (README.md:191):

```markdown
| `GOOGLE_OAUTH_CLIENT_ID` | OAuth client ID for Google Business Profile API | — |
| `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth client secret for Google Business Profile API | — |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Refresh token from `setup_google_reviews_auth` | — |
| `GOOGLE_BUSINESS_ACCOUNT_ID` | Google Business Profile account ID | — |
| `GOOGLE_BUSINESS_LOCATION_ID` | Google Business Profile location ID | — |
```

- [ ] **Step 4: Update Database Models table**

In `README.md`, after the `SiteReview` row (README.md:215):

```markdown
| `GoogleReview` | Reviews synced from Google Business Profile (read-only, replaces SiteReview display on /reviews) |
```

- [ ] **Step 5: Update API Endpoints table**

In `README.md`, change the `/reviews/` row (README.md:248) from:

```markdown
| `GET/POST` | `/reviews/` | View and submit customer reviews |
```

to:

```markdown
| `GET` | `/reviews/` | Display reviews synced from Google Business Profile |
```

- [ ] **Step 6: Update deployment checklist and Management Commands section**

In `README.md`, change item 7 of the deployment checklist (README.md:286) from:

```markdown
7. Schedule `send_feedback_requests` management command via cron or Render's cron job feature
```

to:

```markdown
7. Schedule `send_feedback_requests` and `sync_google_reviews` management commands via cron or Render's cron job feature
```

Replace the "Management Commands" section (README.md:290-297) with:

```markdown
## Management Commands

```bash
# Send SMS feedback requests to customers with completed bookings
python manage.py send_feedback_requests

# One-time: authorize this app against the farm's Google Business Profile
# and print the refresh token + account/location IDs to add to .env
python manage.py setup_google_reviews_auth

# Recurring: pull the latest reviews from Google Business Profile into GoogleReview
python manage.py sync_google_reviews
```

`send_feedback_requests` and `sync_google_reviews` are intended to run on a scheduled basis (e.g., daily). `setup_google_reviews_auth` is run once, locally, to obtain the refresh token.
```

- [ ] **Step 7: Commit**

```bash
git add mesilat_zion_farm_website/settings.py README.md
git commit -m "Document Google reviews env vars and management commands"
```

---

### Task 6: Rewire `/reviews` view and template to Google reviews

**Files:**
- Modify: `homePage/views/reviews.py` (imports at homePage/views/reviews.py:1-4; `site_reviews` function at homePage/views/reviews.py:96-135)
- Modify: `homePage/templates/homePage/site_reviews.html`
- Modify: `homePage/static/homePage/reviews_page.css` (append one small rule)
- Test: `homePage/tests/test_site_reviews_view.py`

**Interfaces:**
- Consumes: `homePage.models.GoogleReview` (Task 1).
- Produces: `/reviews/` (`GET` only) renders Google reviews; no longer accepts `POST`.

- [ ] **Step 1: Write the failing tests**

Create `homePage/tests/test_site_reviews_view.py`:

```python
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from homePage.models import GoogleReview


class SiteReviewsViewTest(TestCase):
    def test_shows_google_reviews_and_average(self):
        GoogleReview.objects.create(
            google_review_id="r1", reviewer_name="דנה", rating=5,
            comment="מעולה", create_time=timezone.now(), update_time=timezone.now(),
        )
        GoogleReview.objects.create(
            google_review_id="r2", reviewer_name="יוסי", rating=3,
            comment="בסדר", create_time=timezone.now(), update_time=timezone.now(),
        )

        resp = self.client.get(reverse("site_reviews"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "דנה")
        self.assertContains(resp, "יוסי")
        self.assertEqual(resp.context["rating_count"], 2)
        self.assertEqual(resp.context["rating_avg"], 4.0)

    def test_empty_state(self):
        resp = self.client.get(reverse("site_reviews"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אין עדיין ביקורות")

    def test_post_not_allowed(self):
        resp = self.client.post(reverse("site_reviews"), {})
        self.assertEqual(resp.status_code, 405)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python manage.py test homePage.tests.test_site_reviews_view -v 2`
Expected: FAIL — page currently redirects to `home` (302, not 200) and has no `GoogleReview` data.

- [ ] **Step 3: Update the view**

In `homePage/views/reviews.py`, change the imports (lines 1-4):

```python
from homePage.models import GoogleReview, Booking, CancellationRequest
from ..forms import CancelRequestForm
from django.views.decorators.http import require_http_methods
from django.db.models import Avg
```

(drop the `django_ratelimit` import and `SiteReviewForm` — no longer used in this file.)

Replace the `site_reviews` function (lines 96-135, including its `@require_http_methods` and `@ratelimit` decorators) with:

```python
@require_http_methods(["GET"])
def site_reviews(request):
    """דף ביקורות - מציג תגובות שסונכרנו מ-Google Business Profile"""
    logger.debug("site_reviews called")

    qs = GoogleReview.objects.all()  # כבר ממוין לפי -create_time דרך Meta.ordering
    paginator = Paginator(qs, 10)
    page_obj = paginator.get_page(request.GET.get('page'))

    agg = qs.aggregate(avg=Avg('rating'))
    return render(request, "homePage/site_reviews.html", {
        "reviews": page_obj.object_list,
        "page_obj": page_obj,
        "rating_avg": agg['avg'] or 0,
        "rating_count": qs.count(),
    })
```

- [ ] **Step 4: Update the template**

In `homePage/templates/homePage/site_reviews.html`:

Replace the review-card loop (lines 28-47) with:

```html
  <section class="reviews-grid">
    {% for r in reviews %}
      <article class="review-card">
        <header class="review-head">
          <strong class="review-who">{{ r.reviewer_name|default:"אנונימי" }}</strong>
          <span class="stars" aria-label="{{ r.rating }} כוכבים">
            {% for i in "12345" %}
              {% if forloop.counter <= r.rating %}★{% else %}☆{% endif %}
            {% endfor %}
          </span>
        </header>

        {% if r.comment %}
          <p class="review-comment">{{ r.comment|linebreaksbr }}</p>
        {% endif %}

        {% if r.reply_comment %}
          <p class="review-reply"><strong>תגובת החווה:</strong> {{ r.reply_comment|linebreaksbr }}</p>
        {% endif %}

        <small class="review-ts">{{ r.create_time|date:"d.m.Y H:i" }}</small>
      </article>
    {% empty %}
      <p class="no-reviews">אין עדיין ביקורות.</p>
    {% endfor %}
  </section>
```

Delete the entire `#sms_review` block (the "השאירו ביקורת" form section, originally lines 63-120) and the `{% if focus_rating_error %}...{% endif %}` script block that follows it (originally lines 123-149) — everything between the `{% if page_obj %}...{% endif %}` pagination block and the final `{% endblock %}`.

The file should end with:

```html
  {% if page_obj %}
    <div class="pagination">
      {% if page_obj.has_previous %}
        <a href="?page=1">ראשון</a>
        <a href="?page={{ page_obj.previous_page_number }}">הקודם</a>
      {% endif %}
      <span class="current">עמוד {{ page_obj.number }} מתוך {{ page_obj.paginator.num_pages }}</span>
      {% if page_obj.has_next %}
        <a href="?page={{ page_obj.next_page_number }}">הבא</a>
        <a href="?page={{ page_obj.paginator.num_pages }}">אחרון</a>
      {% endif %}
    </div>
  {% endif %}
</div>

{% endblock %}
```

- [ ] **Step 5: Add the one new CSS rule**

Append to `homePage/static/homePage/reviews_page.css`:

```css
.review-reply {
  margin: 6px 0 0;
  padding-inline-start: 10px;
  border-inline-start: 3px solid var(--brown-light, #DAB49D);
  color: #5a4636;
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python manage.py test homePage.tests.test_site_reviews_view -v 2`
Expected: PASS (3 tests)

- [ ] **Step 7: Run the full test suite**

Run: `python manage.py test homePage -v 1`
Expected: all tests pass (this catches any other test that assumed the old `/reviews/` POST behavior).

- [ ] **Step 8: Manual check in the browser**

Run the dev server (`python manage.py runserver`), create a couple of `GoogleReview` rows via the admin or the shell, and open `/reviews/` to confirm the cards render correctly and there's no leftover "השאירו ביקורת" form.

- [ ] **Step 9: Commit**

```bash
git add homePage/views/reviews.py homePage/templates/homePage/site_reviews.html homePage/static/homePage/reviews_page.css homePage/tests/test_site_reviews_view.py
git commit -m "Rewire /reviews page to display Google reviews and drop the submission form"
```
