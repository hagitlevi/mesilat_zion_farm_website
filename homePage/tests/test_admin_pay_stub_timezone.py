"""
בדיקת רגרסיה לבאג אזור-זמן ב-admin_pay_stub (מסלול "תשלום להזמנה קיימת" -
mode == "booking", כשמסמנים ידנית שהזמנה קיימת (שנוצרה קודם בלי תשלום) שולמה):

ה-Booking נשלף מה-DB (Booking.objects.filter(pk=...).first()), כך ש-start_dt/end_dt
שלו הם aware ב-UTC. בלי המרה לשעון ישראל, המערכת הייתה מחשבת יום/שעה שגויים
(בהפרש של שעון ישראל מ-UTC) ומחפשת/תופסת סלוטים לפי הזמן השגוי - ואם סלוטים
כאלה במקרה קיימים ופנויים (למשל בגלל רכיבות זריחה מוקדמות), היא תופסת אותם
בטעות במקום הסלוטים האמיתיים של ההזמנה, ושולחת ללקוח מייל/SMS עם שעה שגויה.
"""
from datetime import date, time, timedelta, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from homePage.models import Activity, Appointment, Booking

_DATE = date(2026, 9, 1)
_TIME = time(10, 0)  # 10:00 שעון ישראל (קיץ, UTC+3) = 07:00 UTC
_BASE_DT = datetime.combine(_DATE, _TIME)


@override_settings(SEND_SMS=False, SEND_EMAIL=True)
class AdminPayStubTimezoneTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_superuser(
            username="staff4", email="staff4@example.com", password="pw12345!"
        )
        self.client.force_login(self.staff)

        self.activity = Activity.objects.create(
            name="רכיבה", description="", duration_minutes=60, price=Decimal("100")
        )
        self.booking = Booking.objects.create(
            activity=self.activity,
            customer_name="דנה כהן",
            customer_phone="0501234567",
            customer_email="dana@example.com",
            participants=1,
            total_price=Decimal("100"),
            payment_method="",
            payment_ref="",
            status="pending",
            start_dt=_BASE_DT,
            end_dt=_BASE_DT + timedelta(minutes=60),
        )

        # הסלוטים האמיתיים של ההזמנה (10:00 שעון ישראל) - עדיין לא נתפסו.
        self.real_times = [time(10, 0), time(10, 15), time(10, 30), time(10, 45)]
        for t in self.real_times:
            Appointment.objects.create(date=_DATE, time=t, is_booked=False, is_break=False)
        self.real_buffer_time = time(11, 0)
        Appointment.objects.create(date=_DATE, time=self.real_buffer_time, is_booked=False, is_break=False)

        # "פיתיון": סלוטים פנויים בזמן שהיה מתקבל בטעות אם לא ממירים UTC->שעון ישראל
        # (10:00 - 3 שעות = 07:00). אם התיקון עובד, הם צריכים להישאר פנויים לגמרי.
        self.decoy_times = [time(7, 0), time(7, 15), time(7, 30), time(7, 45)]
        for t in self.decoy_times:
            Appointment.objects.create(date=_DATE, time=t, is_booked=False, is_break=False)
        Appointment.objects.create(date=_DATE, time=time(8, 0), is_booked=False, is_break=False)

        self.url = reverse("admin:homePage_admin_pay_stub")

    def test_marking_existing_booking_paid_captures_the_real_local_time_slots(self):
        resp = self.client.post(self.url, {"id": str(self.booking.id), "action": "mark_paid"})

        self.assertRedirects(resp, reverse("admin:homePage_booking_change", args=[self.booking.id]))

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "paid")

        for t in self.real_times:
            appt = Appointment.objects.get(date=_DATE, time=t)
            self.assertTrue(appt.is_booked, f"slot {t} should be booked")
            self.assertTrue(appt.is_paid)
            self.assertEqual(appt.booking_id, self.booking.id)

        buffer_appt = Appointment.objects.get(date=_DATE, time=self.real_buffer_time)
        self.assertTrue(buffer_appt.is_booked)
        self.assertTrue(buffer_appt.is_break)
        self.assertEqual(buffer_appt.booking_id, self.booking.id)

        # הפיתיונות (07:00 וכו') לא נגעו בהם בכלל
        for t in self.decoy_times + [time(8, 0)]:
            decoy = Appointment.objects.get(date=_DATE, time=t)
            self.assertFalse(decoy.is_booked, f"decoy slot {t} must stay free")
            self.assertIsNone(decoy.booking_id)

    def test_confirmation_email_shows_correct_local_time(self):
        self.client.post(self.url, {"id": str(self.booking.id), "action": "mark_paid"})

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("10:00", body)
        self.assertNotIn("07:00", body)
