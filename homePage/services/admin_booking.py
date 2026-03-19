# homePage/services/admin_booking.py
from datetime import datetime, timedelta
from django.db import transaction
from django.db.utils import IntegrityError
from django.utils import timezone
import secrets
from homePage.models import Appointment, Activity, Booking
import logging

logger = logging.getLogger(__name__)

def _gen_unique_mz_ref(digits=8) -> str:
    """מייצר מחרוזת ייחודית בפורמט MZ-XXXXXXXX, מנסה עד 25 פעמים מול ה-DB לפני פולבאק נדיר עם תאריך"""
    logger.debug("_gen_unique_mz_ref called with digits: %d", digits)

    for _ in range(25):
        cand = "MZ-" + "".join(secrets.choice("0123456789") for _ in range(digits))
        if not Booking.objects.filter(payment_ref=cand).exists():
            return cand
    # פולבאק נדיר – ניפול לשעון (עדיין עם MZ-)
    return "MZ-" + timezone.now().strftime("%y%m%d%H%M%S")

@transaction.atomic
def create_booking_from_slot(*, base_slot_id: int, duration_minutes: int,
                             activity_id: int | None = None,
                             participants: int = 1,
                             customer_name: str = "",
                             customer_phone: str = "",
                             customer_email: str = "",
                             mark_paid: bool = True,
                             capture_buffer: bool = True) -> Booking:
    """
    יוצרת Booking כמו באתר:
    - תופסת רצף סלוטים של 15 דק' לפי משך.
    - מסמנת is_booked=True, is_paid בהתאם, ואופציונלית תופסת 15 דק' הפסקה בסוף (>30).
    - מייצרת payment_ref ייחודי בפורמט MZ-XXXXXXXX ושומרת על ההזמנה; מעדכנת payment_reference על הסלוטים.
    """
    logger.debug("create_booking_from_slot called with base_slot_id: %d, duration_minutes: %d, activity_id: %s, participants: %d, customer_name: %s, customer_phone: %s, customer_email: %s, mark_paid: %s, capture_buffer: %s",)

    base = Appointment.objects.select_for_update().get(pk=base_slot_id)
    if base.is_booked or base.is_break:
        raise ValueError("סלוט ההתחלה תפוס או מוגדר כהפסקה")

    # קביעת פעילות
    activity = None
    if activity_id:
        activity = Activity.objects.filter(id=activity_id).first()
    if not activity:
        activity = getattr(base, "activity", None) or (base.activities.first() if hasattr(base, "activities") else None)
    if not activity:
        raise ValueError("לא נמצאה פעילות מתאימה לסלוט")

    # חשב רצף הזמנים
    slot_count = max(1, (duration_minutes + 14) // 15)
    start_dt = datetime.combine(base.date, base.time)
    times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

    # שליפת הסלוטים הנדרשים (פנויים ולא הפסקות)
    appts = list(Appointment.objects.select_for_update().filter(
        date=base.date,
        time__in=times_needed,
        is_booked=False,
        is_break=False,
    ).order_by("time"))

    if len(appts) != slot_count:
        raise ValueError("אין רצף סלוטים פנוי לכל המשך שנבחר")

    # יצירת ההזמנה
    booking = Booking.objects.create(
        activity=activity,
        customer_name=customer_name.strip(),
        customer_phone=customer_phone.strip(),
        customer_email=customer_email.strip(),
        participants=max(1, int(participants or 1)),
        total_price=None,                 # אם תרצי לחשב – הוסיפי לוגיקה כאן
        payment_method="admin",
        status="paid" if mark_paid else "pending",
        start_dt=start_dt,
        end_dt=start_dt + timedelta(minutes=duration_minutes),
        details="",                       # אופציונלי
    )

    # מספר עסקה ייחודי בפורמט MZ-XXXXXXXX
    ref = _gen_unique_mz_ref()
    try:
        booking.payment_ref = ref
        booking.save(update_fields=["payment_ref"])
    except IntegrityError:
        # במקרה נדיר של התנגשות – ננסה שוב פעם אחת
        ref = _gen_unique_mz_ref()
        booking.payment_ref = ref
        booking.save(update_fields=["payment_ref"])

    # עדכון הסלוטים של המפגש עצמו
    for a in appts:
        a.booking = booking
        a.is_booked = True
        a.is_paid = bool(mark_paid)
        a.is_break = False
        a.payment_reference = ref
        if a.activity_id != activity.id:
            a.activity = activity
        a.save(update_fields=["booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"])

        if activity and hasattr(a, "activities"):
            a.activities.add(activity)

    # הפסקת 15 דק' אחרי סוף התור אם >30 ודורשים buffer
    if capture_buffer and duration_minutes > 30:
        extra_start = start_dt + timedelta(minutes=15 * slot_count)
        extra = Appointment.objects.select_for_update().filter(
            date=base.date, time=extra_start.time(),
            is_booked=False, is_break=False,
        ).first()
        if extra:
            extra.booking = booking
            extra.is_booked = True
            extra.is_paid = False
            extra.is_break = True
            if extra.activity_id != activity.id:
                extra.activity = activity
            extra.payment_reference = ref
            extra.save(update_fields=["booking", "is_booked", "is_paid", "is_break", "activity", "payment_reference"])

    return booking
