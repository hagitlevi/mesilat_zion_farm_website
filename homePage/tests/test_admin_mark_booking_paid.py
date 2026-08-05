"""
בדיקות ל"סימון הזמנה כשולם" (BookingAdmin.mark_paid_view): הזמנה שנוצרה כ"ללא תשלום"
ושולם עליה בפועל בדיעבד - מסמנים כשולם, מנפיקים קבלה, ושולחים ללקוח רק את הקבלה
במייל, בלי לשלוח שוב SMS/מייל עם פרטי ההזמנה.
"""
from datetime import date, time, timedelta, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from homePage.models import Activity, Appointment, Booking, Receipt

_DATE = date(2026, 9, 1)
_TIME = time(10, 0)
_BASE_DT = datetime.combine(_DATE, _TIME)


@override_settings(SEND_SMS=False, SEND_EMAIL=True)
class MarkBookingPaidTests(TestCase):
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
        self.slots = [
            Appointment.objects.create(
                date=_DATE, time=(_BASE_DT + timedelta(minutes=15 * i)).time(),
                is_booked=True, is_paid=False, booking=self.booking,
            )
            for i in range(2)
        ]
        self.url = reverse("admin:homePage_booking_mark_paid", args=[self.booking.id])

    def test_mark_paid_creates_receipt_and_sends_only_receipt_email(self):
        # render_receipt_pdf תלוי ב-WeasyPrint (Pango/Cairo) שלא מותקן בסביבת הפיתוח
        # של Windows - עוקפים אותו כדי לבדוק את שליחת המייל עצמה בלי תלות בזה.
        with patch("homePage.admin.send_sms_via_ntfy") as mock_sms, \
             patch("homePage.services.receipts.render_receipt_pdf", return_value=b"%PDF-fake"):
            resp = self.client.post(self.url, {"payment_method": "bit"})

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "paid")
        self.assertEqual(self.booking.payment_method, "bit")

        for a in self.slots:
            a.refresh_from_db()
            self.assertTrue(a.is_paid)

        receipt = Receipt.objects.get(customer_email="dana@example.com")
        self.assertEqual(receipt.payment_method, "bit")
        self.assertEqual(receipt.amount, Decimal("100"))

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(receipt.receipt_number, mail.outbox[0].subject)
        mock_sms.assert_not_called()

        self.assertRedirects(resp, reverse("admin:homePage_booking_change", args=[self.booking.id]))

    def test_mark_paid_requires_a_payment_method(self):
        resp = self.client.post(self.url, {"payment_method": ""})

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")
        self.assertFalse(Receipt.objects.filter(customer_email="dana@example.com").exists())
        self.assertRedirects(resp, self.url)

    def test_already_paid_booking_is_left_alone(self):
        self.booking.status = "paid"
        self.booking.save(update_fields=["status"])

        resp = self.client.post(self.url, {"payment_method": "bit"})

        self.assertFalse(Receipt.objects.filter(customer_email="dana@example.com").exists())
        self.assertRedirects(resp, reverse("admin:homePage_booking_change", args=[self.booking.id]))
