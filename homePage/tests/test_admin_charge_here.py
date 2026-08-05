"""
בדיקות ל"הקלידי כרטיס כאן" (action=charge_here) ב-admin_pay_stub: המנהלת מקלידה
בעצמה את פרטי הכרטיס עבור לקוח. אמור להעביר ישירות (redirect) לדף הסליקה של
PayPlus - בלי דף "ממתינים" ביניים ובלי לפתוח כרטיסייה נוספת - כי pay_return כבר
יודעת (לפי raw_metadata.source) להחזיר את המנהלת לעמוד ההזמנה באדמין בסוף.
"""
from datetime import date, time, timedelta, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from homePage.models import Activity, Appointment, Booking, Payment

_DATE = date(2026, 9, 1)
_TIME = time(10, 0)
_BASE_DT = datetime.combine(_DATE, _TIME)

_FAKE_LINK = "https://payplus.example/pay/abc123"


@override_settings(PAYPLUS_API_KEY="dummy", PAYPLUS_PAYMENT_PAGE_UID="dummy")
class AdminChargeHereTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_superuser(
            username="staff1", email="staff1@example.com", password="pw12345!"
        )
        self.client.force_login(self.staff)

        self.activity = Activity.objects.create(
            name="רכיבה", description="", duration_minutes=30, price=Decimal("100")
        )
        self.booking = Booking.objects.create(
            activity=self.activity,
            customer_name="דנה כהן",
            customer_phone="0501234567",
            customer_email="dana@example.com",
            participants=1,
            total_price=Decimal("100"),
            payment_method="admin",
            payment_ref="MZ-TEST-1",
            status="pending",
            start_dt=_BASE_DT,
            end_dt=_BASE_DT + timedelta(minutes=30),
        )
        Appointment.objects.create(
            date=_DATE, time=_TIME, is_booked=True, is_paid=False, booking=self.booking,
        )
        self.url = reverse("admin:homePage_admin_pay_stub")

    def test_charge_here_redirects_straight_to_payplus_link(self):
        with patch("homePage.admin._create_payplus_payment_link", return_value=_FAKE_LINK) as mock_link:
            resp = self.client.post(self.url, {"id": str(self.booking.id), "action": "charge_here"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, _FAKE_LINK)
        mock_link.assert_called_once()

        payment = Payment.objects.get(booking=self.booking)
        self.assertEqual(payment.provider, "payplus")
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.raw_metadata.get("source"), "admin_pay_stub_booking")
