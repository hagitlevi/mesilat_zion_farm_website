"""
בדיקת רגרסיה לבאג בחישוב "שעות זמינות" בעת שינוי שעה להזמנה קיימת באדמין
(BookingAdminForm.__init__): כשמשך הפעילות>30 דק' (דורש 15 דק' הפסקה),
find_free_start_times כבר מוסיפה את ההפסקה בעצמה - העברת משך שכבר "מנופח"
בהפסקה גרמה לה לדרוש הפסקה כפולה (90 דק' פנויות במקום 75), ולכן שעות תקינות
נעלמו מרשימת הבחירה.
"""
from datetime import date, time, timedelta, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from homePage.models import Activity, Appointment, Booking

_DAY = date(2026, 9, 10)


class BookingRescheduleAvailableTimesTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_superuser(
            username="staff2", email="staff2@example.com", password="pw12345!"
        )
        self.client.force_login(self.staff)

        # פעילות של שעה (60 דק') - דורשת הפסקה של 15 דק' אחריה
        self.activity = Activity.objects.create(
            name="רכיבה", description="", duration_minutes=60, price=Decimal("100")
        )

        # ההזמנה הקיימת: 10:00-11:00
        start_dt = datetime.combine(_DAY, time(10, 0))
        self.booking = Booking.objects.create(
            activity=self.activity,
            customer_name="לקוחה",
            customer_phone="0501234567",
            customer_email="c@example.com",
            participants=1,
            status="paid",
            start_dt=start_dt,
            end_dt=start_dt + timedelta(minutes=60),
        )
        for i, t in enumerate([time(10, 0), time(10, 15), time(10, 30), time(10, 45)]):
            Appointment.objects.create(
                date=_DAY, time=t, is_booked=True, is_break=False, booking=self.booking
            )
        Appointment.objects.create(
            date=_DAY, time=time(11, 0), is_booked=True, is_break=True, booking=self.booking
        )

        # חלון פנוי נפרד ומאוחר יותר ביום: בדיוק 75 דק' פנויות (14:00-15:15),
        # בלי סלוט נוסף אחרי 15:00 - כדי לבודד את הבאג (14:00 היה נעלם רק אם
        # המערכת דרשה הפסקה כפולה - 90 דק' במקום 75).
        for t in (time(14, 0), time(14, 15), time(14, 30), time(14, 45), time(15, 0)):
            Appointment.objects.create(date=_DAY, time=t, is_booked=False, is_break=False)

    def test_valid_75_minute_opening_is_offered_when_rescheduling(self):
        resp = self.client.get(
            reverse("admin:homePage_booking_change", args=[self.booking.id])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "14:00")