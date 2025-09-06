from datetime import datetime, timedelta, date, time
from homePage.models import Appointment, CustomSchedule, Activity
from django.db.models import Q
from django.utils import timezone
import secrets
from django.db import transaction, IntegrityError
from .models import Booking, Payment

def cleanup_expired_appointments(delete_booked=False):
    """
    מוחק תורים שהסתיימו (עברו בזמן), כברירת מחדל רק כאלה שלא הוזמנו.
    """
    now = timezone.localtime()
    base_q = Q(date__lt=now.date()) | Q(date=now.date(), time__lt=now.time())

    q = base_q if delete_booked else base_q & Q(is_booked=False)
    deleted, _ = Appointment.objects.filter(q).delete()
    return deleted


def generate_appointments(days_ahead=7):

    start_date = date.today()

    for day_offset in range(days_ahead):
        current_date = start_date + timedelta(days=day_offset)
        weekday = current_date.weekday()  # Mon=0 ... Sun=6

        # --- יצירת תורים רגילים (הלוגיקה המקורית שלך) ---
        try:
            custom = CustomSchedule.objects.get(date=current_date)
            if not custom.is_active:
                continue
            regular_start_time = custom.start_time
            regular_end_time = custom.end_time
        except CustomSchedule.DoesNotExist:
            if weekday == 4:  # יום שישי
                regular_start_time = time(9, 0)
                regular_end_time = time(16, 0)
            elif weekday == 5:  # יום שבת
                continue
            else:
                regular_start_time = time(9, 0)
                regular_end_time = time(20, 0)

        current_time = datetime.combine(current_date, regular_start_time)
        end_datetime = datetime.combine(current_date, regular_end_time)

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

        # ======================= תוספת 1: "רכיבת זריחה" =======================
        # א'–ה' בלבד (בפייתון: ראשון=6, שני=0, שלישי=1, רביעי=2, חמישי=3)
        if weekday in {0, 1, 2, 3, 6}:
            try:
                sunrise_activity = Activity.objects.get(name='רכיבה בזריחה')
            except Activity.DoesNotExist:
                print("⚠ לא נמצאה פעילות זריחה – בדקו את השם/ה-type")
                sunrise_activity = None  # אם אין פעילות, ניצור בלי שיוך

            sunrise_start_time = time(5, 0)
            sunrise_end_time   = time(8, 0)

            sunrise_current = datetime.combine(current_date, sunrise_start_time)
            sunrise_end_dt  = datetime.combine(current_date, sunrise_end_time)

            while sunrise_current < sunrise_end_dt:
                # ננסה למנוע כפילות לפי date+time; אם יש לכם שדה duration_minutes בטבלת Appointment
                # ואתם רוצים לבדל לפי משך—ניתן להוסיף אותו גם פה כבדיקה נוספת.
                exists_qs = Appointment.objects.filter(
                    date=current_date,
                    time=sunrise_current.time()
                )
                if not exists_qs.exists():
                    new_appointment = Appointment(
                        date=current_date,
                        time=sunrise_current.time(),
                        # אם יש לכם שדה כזה ורוצים להגדיר 15 דק':
                        # duration_minutes=15,
                        is_booked=False,
                        is_break=False,
                    )
                    new_appointment.save()
                    # שיוך לפעילות אם קיימת מערכתית ושדה M2M activities קיים
                    try:
                        if sunrise_activity:
                            new_appointment.activities.add(sunrise_activity)
                    except Exception:
                        pass

                sunrise_current += timedelta(minutes=15)

        # ======================= תוספת/תיקון מינימלי 2: "רכיבת לילה" =======================
        # א'–ה' בלבד (תיקון: לא range(5), אלא כולל ראשון=6)
        if weekday in {0, 1, 2, 3, 6}:
            try:
                night_ride_activity = Activity.objects.get(name='רכיבת לילה')
            except Activity.DoesNotExist:
                night_ride_activity = None

            night_start_time = time(20, 0)   # 20:00
            night_end_time   = time(23, 59)  # סוף היום (לא כולל)

            night_current_time = datetime.combine(current_date, night_start_time)
            night_end_datetime = datetime.combine(current_date, night_end_time)

            while night_current_time < night_end_datetime:
                # מניעת כפילות לפי date+time (שומר על הרוח של הקוד שלך, בלי לגעת בשאר הלוגיקה)
                exists_qs = Appointment.objects.filter(
                    date=current_date,
                    time=night_current_time.time(),
                )
                if not exists_qs.exists():
                    new_appointment = Appointment(
                        date=current_date,
                        time=night_current_time.time(),
                        # אם אתם שומרים משך תור, והדרישה היא 15 דק':
                        # duration_minutes=15,
                        is_booked=False,
                        is_break=False,
                    )
                    new_appointment.save()
                    try:
                        if night_ride_activity:
                            new_appointment.activities.add(night_ride_activity)
                    except Exception:
                        pass

                night_current_time += timedelta(minutes=15)

    print("✅ תורים נוצרו בהצלחה")


def group_consecutive_hours(rows):
    """
    rows: רשימה של שורות שעות, כל שורה עם השדות:
          label (שם היום), start (datetime.time), end (datetime.time), closed (bool)
    מחזיר רשימה מקובצת: ימים עוקבים עם שעות זהות יהפכו לשורה אחת "ראשון–חמישי".
    """
    def get(r, k):
        # תומך גם במבנה dict וגם באובייקט עם מאפיינים
        return r.get(k) if isinstance(r, dict) else getattr(r, k)

    grouped = []
    i = 0
    n = len(rows)
    while i < n:
        j = i
        # אותו מצב (סגור/פתוח) ואותן שעות → ממשיכים את הקבוצה
        while (j + 1 < n and
               get(rows[j], "closed") == get(rows[j+1], "closed") and
               (get(rows[j], "closed") or
                (get(rows[j], "start") == get(rows[j+1], "start") and
                 get(rows[j], "end")   == get(rows[j+1], "end")))):
            j += 1

        first_label = get(rows[i], "label")
        last_label  = get(rows[j], "label")
        label = first_label if i == j else f"{first_label}–{last_label}"

        grouped.append({
            "label":  label,
            "start":  get(rows[i], "start"),
            "end":    get(rows[i], "end"),
            "closed": get(rows[i], "closed"),
        })
        i = j + 1

    return grouped



def generate_mz_ref(prefix="MZ", digits=8) -> str:
    """יוצר למשל MZ-34892017 (ספרות בלבד)"""
    return f"{prefix}-" + "".join(secrets.choice("0123456789") for _ in range(digits))

def assign_unique_ref(booking: Booking, payment: Payment, digits=8) -> str:
    """
    מקצה ref ייחודי לשני הגורמים יחד (booking.payment_ref + payment.charge_id),
    בתוך טרנזקציה, עם ניסיונות חוזרים במקרה התנגשות.
    """
    for _ in range(25):
        ref = generate_mz_ref(digits=digits)
        try:
            with transaction.atomic():
                # אם כבר יש — לא נוגעים
                if not booking.payment_ref:
                    booking.payment_ref = ref
                    booking.save(update_fields=["payment_ref"])
                if not payment.charge_id:
                    payment.charge_id = ref
                    payment.save(update_fields=["charge_id"])
                return ref
        except IntegrityError:
            # התנגשות ייחודיות — ננסה ref אחר
            continue
    raise RuntimeError("לא הצלחתי להקצות payment_ref ייחודי לאחר ניסיונות רבים")
