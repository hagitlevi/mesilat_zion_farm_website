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
