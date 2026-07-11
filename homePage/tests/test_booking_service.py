"""
בדיקות פונקציות עסק ב-booking_service.py:
• detect_season       — קביעת עונה לפי DST בישראל
• _pick_booking_status — בחירת סטטוס ראשון תקף
• build_business_hours_rows — בניית שורות שעות לתצוגה
• get_rules_for        — כללי זמינות לפעילות ויום
"""
from datetime import date, time, timedelta

from django.test import TestCase

from homePage.models import (
    Activity, Booking, Season, Weekday, BusinessHours,
)
from homePage.services.booking_service import (
    detect_season,
    _pick_booking_status,
    build_business_hours_rows,
    get_rules_for,
)


def make_activity(**kw):
    return Activity.objects.create(**{"name": "בדיקה", "description": "", "duration_minutes": 30, **kw})


# ─── detect_season ─────────────────────────────────────────────────

class DetectSeasonTest(TestCase):
    """
    ישראל עוברת לשעון קיץ (DST) בסוף מרץ ובחזרה בסוף אוקטובר.
    ינואר ודצמבר → חורף; יולי → קיץ.
    """

    def test_january_is_winter(self):
        self.assertEqual(detect_season(date(2025, 1, 15)), Season.WINTER)

    def test_july_is_summer(self):
        self.assertEqual(detect_season(date(2025, 7, 15)), Season.SUMMER)

    def test_december_is_winter(self):
        self.assertEqual(detect_season(date(2025, 12, 15)), Season.WINTER)

    def test_april_mid_is_summer(self):
        """אחרי מעבר השעון (כ-28 מרץ) — אפריל אמצע חייב להיות קיץ."""
        self.assertEqual(detect_season(date(2025, 4, 15)), Season.SUMMER)

    def test_november_is_winter(self):
        """נובמבר — אחרי שחזרנו לשעון חורף."""
        self.assertEqual(detect_season(date(2025, 11, 15)), Season.WINTER)


# ─── _pick_booking_status ──────────────────────────────────────────

class PickBookingStatusTest(TestCase):
    """
    _pick_booking_status(Model, *candidates) מחזיר את הסטטוס הראשון
    שמופיע ב-STATUS_CHOICES של המודל.
    Booking.STATUS_CHOICES = ('pending', 'paid', 'failed', 'refunded').
    """

    def test_first_valid_is_returned(self):
        result = _pick_booking_status(Booking, "pending", "confirmed")
        self.assertEqual(result, "pending")

    def test_skips_invalid_and_picks_next_valid(self):
        """'confirmed' לא קיים ב-choices — מדלג ל-'paid'."""
        result = _pick_booking_status(Booking, "confirmed", "paid")
        self.assertEqual(result, "paid")

    def test_returns_first_candidate_when_none_match(self):
        """כשאף מועמד לא תקף — מחזיר את הראשון (fallback)."""
        result = _pick_booking_status(Booking, "confirmed")
        self.assertEqual(result, "confirmed")

    def test_all_valid_choices_are_accepted(self):
        for status in ("pending", "paid", "failed", "refunded"):
            result = _pick_booking_status(Booking, status)
            self.assertEqual(result, status)


# ─── build_business_hours_rows ─────────────────────────────────────

class BuildBusinessHoursRowsTest(TestCase):
    """build_business_hours_rows() בונה רשימה של 7 ימים עם מבנה אחיד."""

    def test_returns_exactly_seven_rows(self):
        rows = build_business_hours_rows(season="summer")
        self.assertEqual(len(rows), 7)

    def test_each_row_has_required_keys(self):
        for row in build_business_hours_rows(season="winter"):
            for key in ("label", "closed", "start", "end"):
                self.assertIn(key, row)

    def test_no_db_data_all_days_closed(self):
        """כשאין שעות עבודה ב-DB — כל 7 הימים מוצגים כסגורים."""
        rows = build_business_hours_rows(season="summer")
        self.assertTrue(all(r["closed"] for r in rows))

    def test_day_with_hours_is_not_closed(self):
        """לאחר הוספת שעות לאחד הימים — הוא חייב להופיע כפתוח."""
        # יום שלישי = weekday code 1
        wd, _ = Weekday.objects.get_or_create(code=1, defaults={"name": "שלישי"})
        bh = BusinessHours.objects.create(
            season="summer", start_time=time(8, 0), end_time=time(18, 0)
        )
        bh.days.add(wd)
        rows = build_business_hours_rows(season="summer")
        tuesday_row = next(r for r in rows if r["start"] == time(8, 0))
        self.assertFalse(tuesday_row["closed"])


# ─── get_rules_for ─────────────────────────────────────────────────

class GetRulesForTest(TestCase):
    """
    get_rules_for(activity, date) מחזיר (assigned_only, cutoff_min, win_start, win_end).
    date(2025, 7, 15) = יום שלישי קיץ (weekday=1).
    """

    def test_no_rules_returns_all_defaults(self):
        """ללא כללים ב-DB → ברירות מחדל: לא מוגבל, ללא חלון זמן."""
        assigned, cutoff, win_start, win_end = get_rules_for(make_activity(), date(2025, 7, 15))
        self.assertFalse(assigned)
        self.assertEqual(cutoff, 0)
        self.assertIsNone(win_start)
        self.assertIsNone(win_end)

    def test_business_hours_define_window(self):
        """BusinessHours ליום שלישי קיץ → win_start/win_end לפי השעות שהוגדרו."""
        activity = make_activity()
        wd, _ = Weekday.objects.get_or_create(code=1, defaults={"name": "שלישי"})
        bh = BusinessHours.objects.create(
            season="summer", start_time=time(8, 0), end_time=time(18, 0)
        )
        bh.days.add(wd)

        _, _, win_start, win_end = get_rules_for(activity, date(2025, 7, 15))
        self.assertIsNotNone(win_start)
        self.assertIsNotNone(win_end)
        self.assertEqual(win_start.time(), time(8, 0))
        self.assertEqual(win_end.time(), time(18, 0))

    def test_different_day_does_not_use_wrong_hours(self):
        """
        BusinessHours ביום שלישי (code=1) — ביום שני (date(2025, 7, 14), code=0)
        אין כלל, לכן win_start חייב להיות None.
        """
        activity = make_activity()
        wd, _ = Weekday.objects.get_or_create(code=1, defaults={"name": "שלישי"})
        bh = BusinessHours.objects.create(
            season="summer", start_time=time(8, 0), end_time=time(18, 0)
        )
        bh.days.add(wd)

        _, _, win_start, _ = get_rules_for(activity, date(2025, 7, 14))  # שני
        self.assertIsNone(win_start)
