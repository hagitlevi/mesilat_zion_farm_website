from datetime import datetime, timedelta, date, time
from homePage.models import Appointment, CustomSchedule, Activity, Booking, Payment
from django.db.models import Q
from django.utils import timezone
import secrets
from django.db import transaction, IntegrityError

def cleanup_expired_appointments(delete_booked=False):
    """
    מוחק תורים שהסתיימו (עברו בזמן), כברירת מחדל רק כאלה שלא הוזמנו.
    """
    now = timezone.localtime()
    base_q = Q(date__lt=now.date()) | Q(date=now.date(), time__lt=now.time())

    q = base_q if delete_booked else base_q & Q(is_booked=False)
    deleted, _ = Appointment.objects.filter(q).delete()
    return deleted


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

# --- עוזר כללי: יצירת משבצות רבע שעה, עם שיוך פעילות (אם יש) ---
def _create_quarter_slots(current_date, start_t: time, end_t: time, activity_name: str | None):
    if start_t is None or end_t is None:
        return
    if start_t >= end_t:
        return

    # נטען פעילות אם התבקש
    act = Activity.objects.filter(name=activity_name).first() if activity_name else None

    cur = datetime.combine(current_date, start_t)
    end_dt = datetime.combine(current_date, end_t)

    while cur < end_dt:  # שימי לב: '<' ולא '<='
        # לא ליצור על הפסקה שסומנה כ-is_break
        if Appointment.objects.filter(
            date=current_date, time=cur.time(), is_break=True
        ).exists():
            cur += timedelta(minutes=15)
            continue

        appt, _created = Appointment.objects.get_or_create(
            date=current_date,
            time=cur.time(),
            defaults={"is_booked": False, "is_break": False},
        )
        # תמיד ננסה לשייך פעילות (Idempotent ב-M2M)
        if act:
            try:
                appt.activities.add(act)
            except Exception:
                pass

        cur += timedelta(minutes=15)


# --- עוזר: קבלת "חלונות" לזמן לפי today + CustomSchedule ---
def _windows_for_date(current_date):
    """
    מחזיר רשימת חלונות [(label, start_time, end_time, activity_name), ...]
    label: "regular" / "sunrise" / "night"
    """
    weekday = current_date.weekday()  # Mon=0 ... Sun=6
    custom = CustomSchedule.objects.filter(date=current_date).first()

    # אם יש CustomSchedule שסגור – ברירת מחדל: כל היום סגור,
    # אלא אם מפעילים במפורש allow_sunrise/allow_night (כדי לפתוח רק חלון מיוחד).
    if custom and not custom.is_active:
        windows = []
        # חריגים אם ביקשת במפורש:
        if getattr(custom, "allow_sunrise", False):
            s_start = getattr(custom, "sunrise_start_time", None) or time(5, 0)
            s_end   = getattr(custom, "sunrise_end_time", None)   or time(8, 0)
            windows.append(("sunrise", s_start, s_end, "רכיבה בזריחה"))
        if getattr(custom, "allow_night", False):
            n_start = getattr(custom, "night_start_time", None) or time(20, 0)
            n_end   = getattr(custom, "night_end_time", None)   or time(23, 59)
            windows.append(("night", n_start, n_end, "רכיבת לילה"))
        return windows

    windows = []

    # --- רגיל ---
    if custom and (custom.start_time and custom.end_time):
        r_start, r_end = custom.start_time, custom.end_time
    else:
        # ברירות מחדל: ו׳ קצר, ש׳ סגור, שאר הימים 09:00–20:00
        if weekday == 4:      # Friday
            r_start, r_end = time(9, 0), time(16, 0)
        elif weekday == 5:    # Saturday
            r_start, r_end = None, None  # סגור
        else:
            r_start, r_end = time(9, 0), time(20, 0)
    if r_start and r_end:
        windows.append(("regular", r_start, r_end, None))

    # --- זריחה: א׳–ה׳ בלבד ---
    if weekday in {6, 0, 1, 2, 3}:
        if getattr(custom, "allow_sunrise", True):  # אם אין שדה – נניח מותר (כמו היום)
            s_start = getattr(custom, "sunrise_start_time", None) or time(5, 0)
            s_end   = getattr(custom, "sunrise_end_time", None)   or time(8, 0)
            windows.append(("sunrise", s_start, s_end, "רכיבה בזריחה"))

    # --- לילה: א׳–ה׳ בלבד ---
    if weekday in {6, 0, 1, 2, 3}:
        if getattr(custom, "allow_night", True):
            n_start = getattr(custom, "night_start_time", None) or time(20, 0)
            n_end   = getattr(custom, "night_end_time", None)   or time(23, 59)
            windows.append(("night", n_start, n_end, "רכיבת לילה"))

    return windows


def generate_appointments(days_ahead=7):
    start_date = timezone.localdate()

    for day_offset in range(days_ahead):
        current_date = start_date + timedelta(days=day_offset)

        # קבלי את כל החלונות ליום
        for label, start_t, end_t, act_name in _windows_for_date(current_date):
            _create_quarter_slots(current_date, start_t, end_t, act_name)

    print("✅ תורים נוצרו בהצלחה")
