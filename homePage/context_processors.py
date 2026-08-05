from homePage.models import SiteSettings


def site_settings(request):
    return {"online_booking_enabled": SiteSettings.load().online_booking_enabled}
