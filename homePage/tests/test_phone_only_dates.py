"""
בדיקות לתכונת "תאריכים להזמנות טלפוניות בלבד" (PhoneOnlyDate):
בתאריך כזה, לקוחות באתר לא אמורים לראות תורים פנויים (ורואים הודעה חלופית),
ולא יכולים לתפוס/להזמין תור גם דרך קריאה ישירה ל-API - אבל האדמין לא מושפע
(לא נוגעים בקוד יצירת ההזמנה/הזמינות של האדמין).
"""
from datetime import date, time, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from homePage.admin import PhoneOnlyDateForm
from homePage.models import Activity, Appointment, PhoneOnlyDate, is_phone_only_date

_DATE = date(2026, 9, 1)      # תאריך חד-פעמי לבדיקה
_TIME = time(10, 0)


class PhoneOnlyDateModelTests(TestCase):
    def test_one_time_matches_only_exact_date(self):
        rule = PhoneOnlyDate.objects.create(date=_DATE, repeat_every_year=False)
        self.assertTrue(rule.matches(_DATE))
        self.assertFalse(rule.matches(date(2027, 9, 1)))
        self.assertFalse(rule.matches(date(2026, 9, 2)))

    def test_recurring_matches_same_month_day_any_year(self):
        rule = PhoneOnlyDate.objects.create(date=date(2020, 4, 15), repeat_every_year=True)
        self.assertTrue(rule.matches(date(2026, 4, 15)))
        self.assertTrue(rule.matches(date(2031, 4, 15)))
        self.assertFalse(rule.matches(date(2026, 4, 16)))

    def test_inactive_rule_is_ignored(self):
        PhoneOnlyDate.objects.create(date=_DATE, repeat_every_year=False, is_active=False)
        self.assertFalse(is_phone_only_date(_DATE))

    def test_one_time_range_matches_all_days_inclusive(self):
        rule = PhoneOnlyDate.objects.create(
            date=date(2026, 8, 10), end_date=date(2026, 8, 16), repeat_every_year=False
        )
        self.assertTrue(rule.matches(date(2026, 8, 10)))
        self.assertTrue(rule.matches(date(2026, 8, 13)))
        self.assertTrue(rule.matches(date(2026, 8, 16)))
        self.assertFalse(rule.matches(date(2026, 8, 9)))
        self.assertFalse(rule.matches(date(2026, 8, 17)))

    def test_recurring_range_matches_every_year(self):
        rule = PhoneOnlyDate.objects.create(
            date=date(2020, 4, 10), end_date=date(2020, 4, 16), repeat_every_year=True
        )
        self.assertTrue(rule.matches(date(2026, 4, 13)))
        self.assertTrue(rule.matches(date(2031, 4, 16)))
        self.assertFalse(rule.matches(date(2026, 4, 17)))

    def test_recurring_range_wraps_around_year_end(self):
        rule = PhoneOnlyDate.objects.create(
            date=date(2020, 12, 28), end_date=date(2020, 1, 3), repeat_every_year=True
        )
        self.assertTrue(rule.matches(date(2026, 12, 30)))
        self.assertTrue(rule.matches(date(2027, 1, 2)))
        self.assertFalse(rule.matches(date(2026, 6, 15)))

    def test_one_time_range_end_before_start_is_invalid(self):
        rule = PhoneOnlyDate(
            date=date(2026, 8, 16), end_date=date(2026, 8, 10), repeat_every_year=False
        )
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_hebrew_date_matches_correct_gregorian_date_each_year(self):
        # א' תשרי (ראש השנה) - חל ב-2026-09-12 וב-2027-10-02
        rule = PhoneOnlyDate.objects.create(kind="HEBREW", h_month="TISHREI", h_day=1)
        self.assertTrue(rule.matches(date(2026, 9, 12)))
        self.assertTrue(rule.matches(date(2027, 10, 2)))
        self.assertFalse(rule.matches(date(2026, 9, 11)))

    def test_hebrew_date_range(self):
        # א'-ב' תשרי (ראש השנה, יומיים) - 2026: 12-13.9
        rule = PhoneOnlyDate.objects.create(
            kind="HEBREW", h_month="TISHREI", h_day=1, h_end_month="TISHREI", h_end_day=2
        )
        self.assertTrue(rule.matches(date(2026, 9, 12)))
        self.assertTrue(rule.matches(date(2026, 9, 13)))
        self.assertFalse(rule.matches(date(2026, 9, 11)))
        self.assertFalse(rule.matches(date(2026, 9, 14)))

    def test_hebrew_date_requires_month_and_day(self):
        rule = PhoneOnlyDate(kind="HEBREW")
        with self.assertRaises(ValidationError):
            rule.full_clean()


class PhoneOnlyDateFormTests(TestCase):
    def test_hebrew_day_accepts_hebrew_letters(self):
        form = PhoneOnlyDateForm(data={
            "kind": "HEBREW", "h_month": "TISHREI", "h_day": "כ״ה",
            "adar_policy": "AUTO_ADAR2", "is_active": "on",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["h_day"], 25)

    def test_hebrew_day_rejects_out_of_range_number(self):
        form = PhoneOnlyDateForm(data={
            "kind": "HEBREW", "h_month": "TISHREI", "h_day": "40",
            "adar_policy": "AUTO_ADAR2", "is_active": "on",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("h_day", form.errors)


class PhoneOnlyDateCustomerFlowTests(TestCase):
    def setUp(self):
        self.activity = Activity.objects.create(
            name="רכיבה", description="", duration_minutes=30, price=100
        )
        for d in (_DATE, _DATE + timedelta(days=1)):
            for t in (time(10, 0), time(10, 15)):
                Appointment.objects.create(date=d, time=t)

        self.url = reverse("available_appointment", kwargs={"activity_id": self.activity.id})

    def test_normal_date_shows_slots(self):
        other_date = _DATE + timedelta(days=1)
        resp = self.client.get(self.url, {"date": other_date.isoformat()})
        self.assertContains(resp, "10:00")
        self.assertNotContains(resp, "ההזמנות לתאריך זה מתבצעות טלפונית בלבד")

    def test_phone_only_date_hides_slots_and_shows_message(self):
        PhoneOnlyDate.objects.create(date=_DATE, repeat_every_year=False)

        resp = self.client.get(self.url, {"date": _DATE.isoformat()})
        self.assertContains(resp, "ההזמנות לתאריך זה מתבצעות טלפונית בלבד")
        self.assertNotContains(resp, "10:00")

    def test_recurring_phone_only_date_applies_every_year(self):
        PhoneOnlyDate.objects.create(
            date=date(2020, _DATE.month, _DATE.day), repeat_every_year=True
        )

        resp = self.client.get(self.url, {"date": _DATE.isoformat()})
        self.assertContains(resp, "ההזמנות לתאריך זה מתבצעות טלפונית בלבד")

    def test_snapshot_returns_no_slots_on_phone_only_date(self):
        PhoneOnlyDate.objects.create(date=_DATE, repeat_every_year=False)

        resp = self.client.get(
            reverse("appointments_snapshot"),
            {"activity_id": self.activity.id, "date": _DATE.isoformat()},
        )
        self.assertEqual(resp.json()["slots"], [])

    def test_range_hides_slots_across_all_days_in_range(self):
        PhoneOnlyDate.objects.create(
            date=_DATE, end_date=_DATE + timedelta(days=1), repeat_every_year=False
        )

        for d in (_DATE, _DATE + timedelta(days=1)):
            resp = self.client.get(self.url, {"date": d.isoformat()})
            self.assertContains(resp, "ההזמנות לתאריך זה מתבצעות טלפונית בלבד")

    def test_hold_appointment_blocked_on_phone_only_date(self):
        PhoneOnlyDate.objects.create(date=_DATE, repeat_every_year=False)
        appt = Appointment.objects.get(date=_DATE, time=_TIME)

        resp = self.client.post(
            reverse("hold_appointment"),
            {
                "appointment_id": appt.id,
                "duration_minutes": "30",
                "activity_id": self.activity.id,
                "date": _DATE.isoformat(),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["ok"])

        appt.refresh_from_db()
        self.assertFalse(appt.is_booked)
        self.assertIsNone(appt.hold_token)