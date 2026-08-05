"""
בדיקות למסלול "שולם ידנית" באשף יצירת ההזמנה של האדמין (BookingAdmin.book_wizard):
תשלום מחוץ ל-PayPlus (ביט/פייבוקס/העברה בנקאית/מזומן) אמור ליצור הזמנה בסטטוס
"paid", לתפוס את הסלוטים כמשולמים, וליצור קבלה אמיתית (Receipt) - בשונה ממסלול
"ללא תשלום" שלא יוצר קבלה בכלל.
"""
import uuid
from datetime import date, time, timedelta, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from homePage.models import Activity, Appointment, Booking, Receipt

_DATE = date(2026, 9, 1)
_TIME = time(10, 0)
_BASE_DT = datetime.combine(_DATE, _TIME)


@override_settings(SEND_SMS=False, SEND_EMAIL=False)
class AdminManualPaidBookingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_superuser(
            username="staff1", email="staff1@example.com", password="pw12345!"
        )
        self.client.force_login(self.staff)

        self.activity = Activity.objects.create(
            name="רכיבה", description="", duration_minutes=30, price=Decimal("100")
        )
        self.slots = [
            Appointment.objects.create(date=_DATE, time=(_BASE_DT + timedelta(minutes=15 * i)).time())
            for i in range(2)
        ]

        self.hold_token = str(uuid.uuid4())
        now = timezone.now()
        for a in self.slots:
            a.hold_token = self.hold_token
            a.hold_until = now + timedelta(minutes=10)
            a.save(update_fields=["hold_token", "hold_until"])

        self.url = reverse("admin:homePage_appointment_book")

    def _post(self, **extra):
        data = {
            "activity_name": "רכיבה",
            "activity_id": str(self.activity.id),
            "duration_minutes": "30",
            "date": _DATE.isoformat(),
            "start_time": _TIME.strftime("%H:%M"),
            "participants": "1",
            "first_name": "דנה",
            "last_name": "כהן",
            "phone": "0501234567",
            "email": "dana@example.com",
            "hold_token": self.hold_token,
        }
        data.update(extra)
        return self.client.post(self.url, data)

    def test_paid_manually_creates_paid_booking_and_receipt(self):
        resp = self._post(payment_mode="paid", payment_method="bit")

        booking = Booking.objects.get(customer_email="dana@example.com")
        self.assertEqual(booking.status, "paid")
        self.assertEqual(booking.payment_method, "bit")

        for a in self.slots:
            a.refresh_from_db()
            self.assertTrue(a.is_booked)
            self.assertTrue(a.is_paid)
            self.assertEqual(a.booking_id, booking.id)

        receipt = Receipt.objects.get(customer_email="dana@example.com")
        self.assertEqual(receipt.payment_method, "bit")
        self.assertEqual(receipt.amount, Decimal("100"))
        self.assertFalse(receipt.is_void)
        self.assertTrue(receipt.receipt_number)

        self.assertRedirects(resp, reverse("admin:homePage_booking_change", args=[booking.id]))

    def test_paid_manually_requires_a_payment_method(self):
        resp = self._post(payment_mode="paid", payment_method="")

        self.assertFalse(Booking.objects.filter(customer_email="dana@example.com").exists())
        self.assertRedirects(resp, self.url)

    def test_skip_payment_still_creates_no_receipt(self):
        self._post(payment_mode="none")

        booking = Booking.objects.get(customer_email="dana@example.com")
        self.assertEqual(booking.status, "pending")

        for a in self.slots:
            a.refresh_from_db()
            self.assertTrue(a.is_booked)
            self.assertFalse(a.is_paid)

        self.assertFalse(Receipt.objects.filter(customer_email="dana@example.com").exists())
