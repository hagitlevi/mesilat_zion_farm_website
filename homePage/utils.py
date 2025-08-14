from datetime import datetime, timedelta, date, time
from homePage.models import Appointment, CustomSchedule
from django.db.models import Q
from django.utils import timezone

def cleanup_expired_appointments(delete_booked=False):
    """
    מוחק תורים שהסתיימו (עברו בזמן), כברירת מחדל רק כאלה שלא הוזמנו.
    אם delete_booked=True — ימחק גם מוזמנים (בדרך כלל לא כדאי).
    """
    now = timezone.localtime()
    base_q = Q(date__lt=now.date()) | Q(date=now.date(), time__lt=now.time())

    q = base_q if delete_booked else base_q & Q(is_booked=False)
    deleted, _ = Appointment.objects.filter(q).delete()
    return deleted

def generate_appointments(days_ahead=7):
    cleanup_expired_appointments(delete_booked=False)

    start_date = date.today()

    for day_offset in range(days_ahead):
        current_date = start_date + timedelta(days=day_offset)
        weekday = current_date.weekday()

        # שעות מיוחדות
        try:
            custom = CustomSchedule.objects.get(date=current_date)
            if not custom.is_active:
                continue
            start_time = custom.start_time
            end_time = custom.end_time
        except CustomSchedule.DoesNotExist:
            if weekday == 4:    #יום שישי
                start_time = time(9, 0)
                end_time = time(16, 0)
            elif weekday == 5:  #יום שבת
                continue
            else:
                start_time = time(9, 0)
                end_time = time(20, 0)

        current_time = datetime.combine(current_date, start_time)
        end_datetime = datetime.combine(current_date, end_time) - timedelta(minutes=30)

        while current_time <= end_datetime:
            if not Appointment.objects.filter(
                date=current_date,
                time=current_time.time()
            ).exists():
                Appointment.objects.create(
                    date=current_date,
                    time=current_time.time(),
                    is_booked=False,
                    is_break=False
                )
            current_time += timedelta(minutes=15)

    print("✅ תורים נוצרו בהצלחה")
