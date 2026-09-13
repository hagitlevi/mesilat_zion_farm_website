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

        created = updated = skipped = 0
        for r in reviews:
            rating = star_rating_to_int(r.get("starRating", ""))
            if rating is None:
                skipped += 1
                continue

            reviewer = r.get("reviewer", {})
            reply = r.get("reviewReply", {})
            defaults = {
                "reviewer_name": reviewer.get("displayName", ""),
                "reviewer_photo_url": reviewer.get("profilePhotoUrl", ""),
                "rating": rating,
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

        self.stdout.write(
            f"סונכרנו {len(reviews)} תגובות (חדשות: {created}, עודכנו: {updated}, דולגו: {skipped})"
        )
