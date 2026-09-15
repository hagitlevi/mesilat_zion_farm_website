from datetime import timedelta, time

from django.test import TestCase
from django.utils import timezone

from homePage.admin import _generate_slots_from_form
from homePage.models import Activity, Appointment


def _next_weekday(start_date, weekday):
    """weekday: Monday=0 ... Sunday=6 (python's date.weekday())."""
    days_ahead = (weekday - start_date.weekday()) % 7 or 7
    return start_date + timedelta(days=days_ahead)


class GenerateSunriseNightSlotsInFullModeTests(TestCase):
    """
    Regression test: selecting the sunrise/night activities on the admin
    "יצירת תורים" (full-day mode) page must create slots even though those
    windows fall outside BusinessHours.
    """

    def setUp(self):
        self.sunrise = Activity.objects.create(name="רכיבה בזריחה", description="", duration_minutes=60)
        self.night = Activity.objects.create(name="רכיבת לילה", description="", duration_minutes=60)
        # Monday has no BusinessHours configured in this test DB, and is a
        # weekday for which _windows_for_date opens sunrise/night by default.
        self.monday = _next_weekday(timezone.localdate(), 0)

    def test_full_mode_creates_sunrise_slots_with_no_business_hours(self):
        totals, err = _generate_slots_from_form({
            "date": self.monday,
            "mode": "full",
            "activities": Activity.objects.filter(pk=self.sunrise.pk),
        })

        self.assertIsNone(err)
        self.assertGreater(totals["created"], 0)
        slot = Appointment.objects.get(date=self.monday, time=time(5, 0))
        self.assertIn(self.sunrise, slot.activities.all())

    def test_full_mode_creates_night_slots_with_no_business_hours(self):
        totals, err = _generate_slots_from_form({
            "date": self.monday,
            "mode": "full",
            "activities": Activity.objects.filter(pk=self.night.pk),
        })

        self.assertIsNone(err)
        self.assertGreater(totals["created"], 0)
        slot = Appointment.objects.get(date=self.monday, time=time(20, 0))
        self.assertIn(self.night, slot.activities.all())
