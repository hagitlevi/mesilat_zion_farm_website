from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticViewSitemap(Sitemap):
  """דפי המידע הציבוריים של האתר — לא כולל טפסי הזמנה/תשלום ונקודות API שאין טעם לאנדקס אותן."""
  changefreq = "weekly"

  def items(self):
    return [
        "home",
        "riding_lessons",
        "night_riding",
        "couple_riding",
        "sunrise_riding",
        "group_riding",
        "carriage_trip",
        "photographs",
        "children_riding",
        "gallery",
        "site_reviews",
        "terms",
        "privacy",
    ]

  def location(self, item):
    return reverse(item)

  def priority(self, item):
    return 1.0 if item == "home" else 0.7
