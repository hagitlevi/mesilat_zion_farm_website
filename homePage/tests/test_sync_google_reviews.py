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
        "reviewReply": {"comment": "תודה רבה!"},
        "createTime": "2026-01-01T10:00:00Z",
        "updateTime": "2026-01-01T10:00:00Z",
    },
]

FAKE_REVIEWS_UPDATED = [
    {
        "name": "accounts/1/locations/2/reviews/abc",
        "reviewer": {"displayName": "דנה", "profilePhotoUrl": ""},
        "starRating": "THREE",
        "comment": "בעצם לא כזה מדהים",
        "reviewReply": {"comment": "תודה רבה!"},
        "createTime": "2026-01-01T10:00:00Z",
        "updateTime": "2026-01-02T10:00:00Z",
    },
]

FAKE_REVIEWS_UNKNOWN_RATING = [
    {
        "name": "accounts/1/locations/2/reviews/xyz",
        "reviewer": {"displayName": "יוסי", "profilePhotoUrl": ""},
        "starRating": "STAR_RATING_UNSPECIFIED",
        "comment": "ללא דירוג ברור",
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
        self.assertEqual(review.reply_comment, "תודה רבה!")

    @patch("homePage.management.commands.sync_google_reviews.fetch_all_reviews")
    @patch("homePage.management.commands.sync_google_reviews.refresh_access_token")
    def test_second_run_does_not_duplicate(self, mock_refresh, mock_fetch):
        mock_refresh.return_value = "tok"
        mock_fetch.return_value = FAKE_REVIEWS

        call_command("sync_google_reviews")
        call_command("sync_google_reviews")

        self.assertEqual(GoogleReview.objects.count(), 1)

    @patch("homePage.management.commands.sync_google_reviews.fetch_all_reviews")
    @patch("homePage.management.commands.sync_google_reviews.refresh_access_token")
    def test_second_run_updates_changed_fields(self, mock_refresh, mock_fetch):
        mock_refresh.return_value = "tok"
        mock_fetch.return_value = FAKE_REVIEWS

        call_command("sync_google_reviews")

        mock_fetch.return_value = FAKE_REVIEWS_UPDATED
        call_command("sync_google_reviews")

        self.assertEqual(GoogleReview.objects.count(), 1)
        review = GoogleReview.objects.get()
        self.assertEqual(review.comment, "בעצם לא כזה מדהים")
        self.assertEqual(review.rating, 3)

    @patch("homePage.management.commands.sync_google_reviews.fetch_all_reviews")
    @patch("homePage.management.commands.sync_google_reviews.refresh_access_token")
    def test_skips_review_with_unresolved_rating(self, mock_refresh, mock_fetch):
        mock_refresh.return_value = "tok"
        mock_fetch.return_value = FAKE_REVIEWS_UNKNOWN_RATING

        call_command("sync_google_reviews")

        self.assertEqual(GoogleReview.objects.count(), 0)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="")
    def test_errors_when_config_missing(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("sync_google_reviews")
