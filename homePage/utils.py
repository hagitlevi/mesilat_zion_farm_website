from datetime import datetime, timedelta, date, time
from homePage.models import Appointment, CustomSchedule, Activity, Booking, Payment
from django.db.models import Q
from django.utils import timezone
import secrets
from django.db import transaction, IntegrityError
from datetime import date  # ודאי שיש ייבוא

def _get_effective_rule(g_date: date) -> CustomSchedule | None:
    # 1) כלל לועזי חד־פעמי באותו תאריך
    r = CustomSchedule.objects.filter(kind="GREGORIAN", date=g_date).first()
    if r:
        return r
    # 2) כלל לועזי שחוזר כל שנה (אותו יום/חודש)
    r = (CustomSchedule.objects
         .filter(kind="GREGORIAN", repeat_every_year=True,
                 date__month=g_date.month, date__day=g_date.day)
         .first())
    if r:
        return r
    # 3) כלל עברי שתואם (נשתמש במתודה של המודל)
    for rule in CustomSchedule.objects.filter(kind="HEBREW").iterator():
        if rule.matches_gregorian_date(g_date):
            return rule
    return None

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
    מחזיר [(label, start_time, end_time, activity_name)]
    label: "regular" / "sunrise" / "night"
    """
    weekday = current_date.weekday()  # Mon=0..Sun=6
    rule = _get_effective_rule(current_date)

    windows = []
    def add_regular(s, e):
        if s and e: windows.append(("regular", s, e, None))
    def add_sunrise(s, e):
        windows.append(("sunrise", s or time(5, 0), e or time(8, 0), "רכיבה בזריחה"))
    def add_night(s, e):
        windows.append(("night", s or time(20, 0), e or time(23, 59), "רכיבת לילה"))

    if rule:
        # יום לא פעיל: רגיל סגור; אפשר לפתוח זריחה/לילה אם הכלל מאפשר
        if not rule.is_active:
            if weekday in {6, 0, 1, 2, 3} and rule.allow_sunrise:
                add_sunrise(rule.sunrise_start_time, rule.sunrise_end_time)
            if weekday in {6, 0, 1, 2, 3} and rule.allow_night:
                add_night(rule.night_start_time, rule.night_end_time)
            return windows

        # יום פעיל: שעות רגילות לפי הכלל (או ברירת מחדל אם לא הוגדר)
        if rule.start_time and rule.end_time:
            add_regular(rule.start_time, rule.end_time)
        else:
            if weekday == 4:        # Fri
                add_regular(time(9, 0), time(16, 0))
            elif weekday == 5:      # Sat
                pass
            else:
                add_regular(time(9, 0), time(20, 0))

        # זריחה/לילה – רק אם הכלל עצמו מאפשר
        if weekday in {6, 0, 1, 2, 3} and rule.allow_sunrise:
            add_sunrise(rule.sunrise_start_time, rule.sunrise_end_time)
        if weekday in {6, 0, 1, 2, 3} and rule.allow_night:
            add_night(rule.night_start_time, rule.night_end_time)
        return windows

    # אין כלל פוגע: ברירות מחדל (כמו שהיה)
    if weekday == 4:
        add_regular(time(9, 0), time(16, 0))
    elif weekday == 5:
        pass
    else:
        add_regular(time(9, 0), time(20, 0))

    if weekday in {6, 0, 1, 2, 3}:
        add_sunrise(None, None)
        add_night(None, None)
    return windows


def generate_appointments(days_ahead=7):
    cleanup_expired_appointments(delete_booked=False)

    start_date = timezone.localdate()

    for day_offset in range(days_ahead):
        current_date = start_date + timedelta(days=day_offset)

        # קבלי את כל החלונות ליום
        for label, start_t, end_t, act_name in _windows_for_date(current_date):
            _create_quarter_slots(current_date, start_t, end_t, act_name)

    print("✅ תורים נוצרו בהצלחה")


def release_and_delete_all_bookings():
    """
    עוברת על כל ההזמנות, משחררת את הסלוטים שלהן ומוחקת אותן.
    מבוססת על release_booking_slots מ-CancellationRequestAdmin.
    """
    bookings = Booking.objects.all()
    total = bookings.count()
    print(f"נמצאו {total} הזמנות למחיקה...")

    for booking in bookings:
        with transaction.atomic():
            qs = Appointment.objects.select_for_update().filter(booking=booking)
            for a in qs:
                a.booking = None
                a.is_booked = False
                a.is_break = False
                a.is_paid = False
                a.payment_reference = ""
                try:
                    a.activity = None
                    upd = ["booking", "is_booked", "is_break", "is_paid", "payment_reference", "activity"]
                except Exception:
                    upd = ["booking", "is_booked", "is_break", "is_paid", "payment_reference"]
                a.save(update_fields=upd)
                if hasattr(a, "activities"):
                    a.activities.clear()

            booking.delete()
            print(f"  ✓ הזמנה #{booking.id} נמחקה")

    print(f"סיום — {total} הזמנות נמחקו.")