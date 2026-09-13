from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


class SetupGoogleReviewsAuthTest(TestCase):
    @override_settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="")
    def test_errors_when_credentials_missing(self):
        with self.assertRaises(CommandError):
            call_command("setup_google_reviews_auth")
