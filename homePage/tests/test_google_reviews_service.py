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
