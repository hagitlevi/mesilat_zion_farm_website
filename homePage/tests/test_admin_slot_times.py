from datetime import date, datetime, timedelta

from django.test import TestCase
from django.utils import timezone
from zoneinfo import ZoneInfo

from homePage.admin import find_free_start_times
from homePage.models import Activity, Appointment

_TZ = ZoneInfo("Asia/Jerusalem")


class FindFreeStartTimesExcludesPastTimesTests(TestCase):
    def setUp(self):
        self.activity = Activity.objects.create(name="רכיבה", description="", duration_minutes=30)
        self.now_local = timezone.now().astimezone(_TZ)
        self.today = self.now_local.date()

    def _slot(self, dt_local):
        return Appointment.objects.create(date=dt_local.date(), time=dt_local.time())

    def test_excludes_todays_slot_in_the_past(self):
        past = self._slot(self.now_local - timedelta(hours=2))
        future = self._slot(self.now_local + timedelta(hours=2))

        times = find_free_start_times(self.today, 15, self.activity.name)

        self.assertNotIn(past.time.strftime("%H:%M"), times)
        self.assertIn(future.time.strftime("%H:%M"), times)

    def test_does_not_filter_future_days_by_time_of_day(self):
        tomorrow = self.today + timedelta(days=1)
        # מעגלים דרך תאריך פיקטיבי כדי ש"5 שעות אחורה" תזיז רק את שעת היום
        # ולא תגלוש ל-'תמול' כשהריצה קרובה לחצות (למשל 00:07 - 5h = היום הקודם).
        early_time = (datetime.combine(date(2000, 1, 1), self.now_local.time()) - timedelta(hours=5)).time()
        early_slot = self._slot(datetime.combine(tomorrow, early_time).replace(tzinfo=_TZ))

        times = find_free_start_times(tomorrow, 15, self.activity.name)

        self.assertIn(early_slot.time.strftime("%H:%M"), times)
