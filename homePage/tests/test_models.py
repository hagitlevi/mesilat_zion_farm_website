"""
בדיקות מודלים — לוגיקה פנימית: ברירות מחדל, ולידציה, signals.

כל TestCase רץ עם DB ריק ומתאפס אחרי כל בדיקה (Django כורך כל test בטרנזקציה).
"""
from datetime import date, time, timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError

from homePage.models import (
    Activity, Appointment, Booking,
    SiteReview, TreatmentSession, CustomSchedule,
)


# ─── פונקציות עזר — יוצרות אובייקטים עם ברירות מחדל הגיוניות ───

def make_activity(**kw):
    return Activity.objects.create(**{"name": "פעילות בדיקה", "description": "", "duration_minutes": 30, **kw})


def make_booking(activity, **kw):
    now = timezone.now()
    return Booking.objects.create(**{
        "activity": activity,
        "start_dt": now,
        "end_dt": now + timedelta(hours=1),
        **kw,
    })


def make_appointment(t=time(10, 0), d=date(2025, 7, 15), **kw):
    return Appointment.objects.create(date=d, time=t, **kw)


# ─── Booking ───────────────────────────────────────────────────────

class BookingDefaultsTest(TestCase):
    """ברירות מחדל וייצוג טקסטואלי של הזמנה."""

    def setUp(self):
        self.activity = make_activity()

    def test_default_status_is_pending(self):
        """כשיוצרים הזמנה ללא status — הסטטוס חייב להיות 'pending'."""
        booking = make_booking(self.activity)
        self.assertEqual(booking.status, "pending")

    def test_str_contains_customer_name(self):
        """ה-__str__ חייב לכלול את שם הלקוח."""
        booking = make_booking(self.activity, customer_name="ישראל ישראלי")
        self.assertIn("ישראל ישראלי", str(booking))

    def test_str_contains_activity_name(self):
        """ה-__str__ חייב לכלול את שם הפעילות."""
        booking = make_booking(self.activity)
        self.assertIn(self.activity.name, str(booking))

    def test_payment_ref_unique_constraint(self):
        """שני bookings עם אותו payment_ref — שגיאת DB."""
        make_booking(self.activity, payment_ref="REF-001")
        with self.assertRaises(Exception):
            make_booking(self.activity, payment_ref="REF-001")


# ─── Activity ──────────────────────────────────────────────────────

class ActivityDefaultsTest(TestCase):
    """ברירות מחדל ו-__str__ של פעילות."""

    def test_default_activity_type_is_none(self):
        self.assertEqual(make_activity().activity_type, "none")

    def test_default_duration_minutes_is_30(self):
        self.assertEqual(make_activity().duration_minutes, 30)

    def test_str_returns_name(self):
        self.assertEqual(str(make_activity(name="רכיבה בסיסית")), "רכיבה בסיסית")


# ─── Appointment — is_held_active() ───────────────────────────────

class AppointmentHoldTest(TestCase):
    """בדיקות מתודת is_held_active() — מחזירה True רק אם הסלוט תפוס ולא פג תוקפו."""

    def test_future_hold_is_active(self):
        appt = make_appointment(
            hold_until=timezone.now() + timedelta(minutes=10),
            hold_token=uuid.uuid4(),
        )
        self.assertTrue(appt.is_held_active())

    def test_expired_hold_is_inactive(self):
        appt = make_appointment(
            hold_until=timezone.now() - timedelta(minutes=1),
            hold_token=uuid.uuid4(),
        )
        self.assertFalse(appt.is_held_active())

    def test_no_hold_is_inactive(self):
        self.assertFalse(make_appointment().is_held_active())


# ─── SiteReview ────────────────────────────────────────────────────

class SiteReviewTest(TestCase):
    """בדיקות ולידציה וייצוג של ביקורת."""

    def test_str_shows_name_and_stars(self):
        r = SiteReview.objects.create(name="דוד", rating=5)
        self.assertIn("דוד", str(r))
        self.assertIn("5", str(r))

    def test_empty_name_shows_anonymous(self):
        r = SiteReview.objects.create(name="", rating=3)
        self.assertIn("אנונימי", str(r))

    def test_rating_zero_is_invalid(self):
        """דירוג 0 מתחת לגבול המינימום (1)."""
        with self.assertRaises(ValidationError):
            SiteReview(rating=0).full_clean()

    def test_rating_six_is_invalid(self):
        """דירוג 6 מעל הגבול המקסימום (5)."""
        with self.assertRaises(ValidationError):
            SiteReview(rating=6).full_clean()

    def test_ratings_1_through_5_are_valid(self):
        for r in range(1, 6):
            SiteReview(name="", rating=r).full_clean()  # לא אמור לזרוק


# ─── TreatmentSession.clean() ──────────────────────────────────────

class TreatmentSessionCleanTest(TestCase):
    """
    TreatmentSession.clean() אוכף:
    • שעת סיום > שעת התחלה
    • שתי השעות בין 09:00 ל-20:00
    """

    def _s(self, start, end):
        return TreatmentSession(
            date=date(2025, 7, 15),
            start_time=start,
            end_time=end,
            customer_full_name="לקוח",
            customer_phone="0500000000",
        )

    def test_end_before_start_raises(self):
        with self.assertRaises(ValidationError):
            self._s(time(12, 0), time(10, 0)).clean()

    def test_equal_times_raises(self):
        with self.assertRaises(ValidationError):
            self._s(time(10, 0), time(10, 0)).clean()

    def test_start_before_09_raises(self):
        with self.assertRaises(ValidationError):
            self._s(time(8, 59), time(11, 0)).clean()

    def test_end_after_20_raises(self):
        with self.assertRaises(ValidationError):
            self._s(time(10, 0), time(20, 1)).clean()

    def test_boundary_start_09_is_valid(self):
        self._s(time(9, 0), time(11, 0)).clean()

    def test_boundary_end_20_is_valid(self):
        self._s(time(18, 0), time(20, 0)).clean()


# ─── CustomSchedule.clean() ────────────────────────────────────────

class CustomScheduleCleanTest(TestCase):
    """CustomSchedule.clean() דורש date עבור GREGORIAN, ו-h_month+h_day עבור HEBREW."""

    def test_gregorian_without_date_raises(self):
        cs = CustomSchedule(kind="GREGORIAN")
        with self.assertRaises(ValidationError) as ctx:
            cs.clean()
        self.assertIn("date", ctx.exception.message_dict)

    def test_hebrew_without_month_raises(self):
        cs = CustomSchedule(kind="HEBREW", h_day=15)
        with self.assertRaises(ValidationError) as ctx:
            cs.clean()
        self.assertIn("h_month", ctx.exception.message_dict)

    def test_hebrew_without_day_raises(self):
        cs = CustomSchedule(kind="HEBREW", h_month="NISAN")
        with self.assertRaises(ValidationError) as ctx:
            cs.clean()
        self.assertIn("h_day", ctx.exception.message_dict)

    def test_gregorian_with_date_passes(self):
        CustomSchedule(kind="GREGORIAN", date=date(2025, 7, 15)).clean()

    def test_hebrew_with_both_fields_passes(self):
        CustomSchedule(kind="HEBREW", h_month="NISAN", h_day=15).clean()


# ─── pre_delete signal: release_on_booking_delete ──────────────────

class BookingDeleteSignalTest(TestCase):
    """
    כשמוחקים Booking — ה-pre_delete signal חייב לשחרר את כל הסלוטים המשויכים:
    is_booked=False, booking=None, customer_name=''.
    """

    def test_deleting_booking_releases_appointment(self):
        activity = make_activity()
        booking = make_booking(activity)
        appt = Appointment.objects.create(
            date=date(2025, 7, 15),
            time=time(10, 0),
            booking=booking,
            is_booked=True,
            is_paid=True,
            customer_name="לקוח בדיקה",
        )

        booking.delete()

        appt.refresh_from_db()
        self.assertFalse(appt.is_booked)
        self.assertFalse(appt.is_paid)
        self.assertIsNone(appt.booking)
        self.assertEqual(appt.customer_name, "")
