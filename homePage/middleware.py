from django.shortcuts import render
from django.utils import timezone

from .models import SiteSettings
from .services.shabbat_zmanim import get_closure_status

# נתיבים שתמיד עוברים, גם כשהאתר "סגור" לשבת/חג:
# - /mzf-admin/ - כדי שאפשר יהיה לכבות את המתג/לטפל אם משהו לא כשורה.
# - pay/webhook/ - קריאות חוזרות (retries) מ-PayPlus חייבות להתקבל תמיד.
# - shabbat-preview/ - כדי שאפשר יהיה לבדוק את מראה עמוד הסגירה בכל שעה.
_ALWAYS_ALLOWED_PREFIXES = ("/mzf-admin/",)
_ALWAYS_ALLOWED_PATHS = {"/pay/webhook/", "/shabbat-preview/"}


class ShabbatClosureMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_allowed_path(request.path):
            return self.get_response(request)

        if not SiteSettings.load().shabbat_closure_enabled:
            return self.get_response(request)

        blocked, reopen_at = get_closure_status()
        if not blocked:
            return self.get_response(request)

        response = render(request, "homePage/shabbat_closed.html", status=503)
        if reopen_at is not None:
            seconds_left = int((reopen_at - timezone.now()).total_seconds())
            response["Retry-After"] = str(max(seconds_left, 0))
        return response

    @staticmethod
    def _is_allowed_path(path):
        if path in _ALWAYS_ALLOWED_PATHS:
            return True
        return any(path.startswith(prefix) for prefix in _ALWAYS_ALLOWED_PREFIXES)
