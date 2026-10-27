"""צ'קבוקס "אישור פרסום ושיווק" באשף ההזמנה של האדמין: נשמר ב-MarketingConsent, אופציונלי, ונבדק אם כבר אושר."""
from datetime import date, time, timedelta, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from homePage.models import Activity, Appointment, MarketingConsent

_DATE = date(2026, 9, 1)
_BASE_DT = datetime.combine(_DATE, time(10, 0))


@override_settings(SEND_SMS=False, SEND_EMAIL=False)
class AdminMarketingOptinTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("s1", "s1@example.com", "pw12345!"))
        self.activity = Activity.objects.create(name="רכיבה", description="", duration_minutes=30, price=Decimal("100"))
        for i in range(2):
            Appointment.objects.create(date=_DATE, time=(_BASE_DT + timedelta(minutes=15 * i)).time())
        self.url = reverse("admin:homePage_appointment_book")

    def _post(self, **extra):
        data = {
            "activity_name": "רכיבה", "activity_id": str(self.activity.id), "duration_minutes": "30",
            "date": _DATE.isoformat(), "start_time": "10:00", "participants": "1",
            "first_name": "דנה", "last_name": "כהן", "phone": "0501234567", "email": "Dana@example.com",
            "payment_mode": "none",
        }
        data.update(extra)
        return self.client.post(self.url, data)

    def test_checked_saves_consent(self):
        self._post(marketing_optin="1")
        c = MarketingConsent.objects.get()
        self.assertEqual(c.subject_id, "0501234567")
        self.assertEqual(c.customer_email, "dana@example.com")

    def test_unchecked_saves_nothing_and_booking_still_works(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MarketingConsent.objects.exists())

    def test_ajax_exists(self):
        q = lambda p: self.client.get(self.url, {"ajax": "marketing_exists", "phone": p}).json()["exists"]
        self.assertFalse(q("050-123-4567"))
        self._post(marketing_optin="1")
        self.assertTrue(q("050-123-4567"))
