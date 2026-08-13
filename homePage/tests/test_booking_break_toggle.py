"""
בדיקות למתג booking_break_enabled ב-SiteSettings: שליטה על תפיסת הפסקת 15 דק'
אוטומטית אחרי הזמנות ארוכות מ-30 דקות, במסלול ה-finalize האמיתי של הזמנות
אונליין (_capture_slots_and_break, הנקרא מ-_finalize_booking_after_payment
בתשלומי PayPlus).
"""
from datetime import date, time, timedelta, datetime

from django.test import TestCase
from django.utils import timezone

from homePage.models import Activity, Appointment, Booking, SiteSettings
from homePage.services.booking_service import _capture_slots_and_break

_DATE = date(2026, 9, 10)
_TIME = time(10, 0)
_BASE_DT = datetime.combine(_DATE, _TIME)
_BREAK_TIME = time(10, 45)


def _slots(count=5):
    return [
        Appointment.objects.create(
            date=_DATE,
            time=(_BASE_DT + timedelta(minutes=15 * i)).time(),
        )
        for i in range(count)
    ]


def _booking(activity):
    now = timezone.now()
    return Booking.objects.create(
        activity=activity,
        start_dt=now,
        end_dt=now + timedelta(minutes=45),
        status="pending",
    )


class BookingBreakToggleTests(TestCase):
    def setUp(self):
        self.activity = Activity.objects.create(
            name="רכיבה", description="", duration_minutes=45
        )
        self.slots = _slots(count=5)
        self.base_appt = self.slots[0]
        self.booking = _booking(self.activity)

    def _set_break_enabled(self, value):
        obj = SiteSettings.load()
        obj.booking_break_enabled = value
        obj.save(update_fields=["booking_break_enabled"])

    def test_break_captured_when_enabled(self):
        self._set_break_enabled(True)

        _capture_slots_and_break(
            appt=self.base_appt, duration_minutes=45, booking=self.booking, activity=self.activity,
        )

        brk = Appointment.objects.get(date=_DATE, time=_BREAK_TIME)
        self.assertTrue(brk.is_break)
        self.assertTrue(brk.is_booked)
        self.assertFalse(brk.is_paid)
        self.assertEqual(brk.booking_id, self.booking.id)

    def test_no_break_captured_when_disabled(self):
        self._set_break_enabled(False)

        _capture_slots_and_break(
            appt=self.base_appt, duration_minutes=45, booking=self.booking, activity=self.activity,
        )

        brk = Appointment.objects.get(date=_DATE, time=_BREAK_TIME)
        self.assertFalse(brk.is_break)
        self.assertFalse(brk.is_booked)
        self.assertIsNone(brk.booking_id)
