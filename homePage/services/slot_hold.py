from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
from django.db import transaction
from django.utils import timezone

from homePage.models import Appointment

@dataclass
class HoldResult:
    ok: bool
    reason: str = ""
    token: str | None = None

def release_expired_holds():
    now = timezone.now()
    Appointment.objects.filter(hold_until__isnull=False, hold_until__lte=now).update(
        hold_until=None, hold_token=None, hold_by=None
    )

def release_hold(token: str):
    if not token:
        return
    Appointment.objects.filter(hold_token=token).update(
        hold_until=None, hold_token=None, hold_by=None
    )

def try_hold_chain(
    *,
    token: str,
    user=None,
    date,
    start_dt,
    minutes_total_for_hold: int,
    include_buffer_if_gt30: bool = True,
    ttl_minutes: int = 15,
) -> HoldResult:
    """
    תופס זמנית רצף סלוטים ל־15 דקות.
    minutes_total_for_hold = משך הפעילות + (15 אם רוצים “הפסקה” כמו אצלך)
    """
    if not token:
        return HoldResult(False, "missing_token")

    release_expired_holds()
    now = timezone.now()
    until = now + timedelta(minutes=ttl_minutes)

    slot_cnt = max(1, (int(minutes_total_for_hold) + 14) // 15)
    times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_cnt)]

    with transaction.atomic():
        # לוקחים נעילה על הסלוטים הרלוונטיים
        qs = (Appointment.objects
              .select_for_update()
              .filter(date=date, time__in=times_needed)
              .order_by("time"))

        appts = list(qs)
        if len(appts) != slot_cnt:
            return HoldResult(False, "missing_slots")  # אין רצף

        # בדיקת תפוס/הפסקה/הולד של מישהו אחר
        for a in appts:
            if a.is_booked or a.is_break:
                return HoldResult(False, "already_booked_or_break")
            if a.hold_until and a.hold_until > now and str(a.hold_token) != str(token):
                return HoldResult(False, "held_by_other")

        # כאן תופסים
        Appointment.objects.filter(id__in=[a.id for a in appts]).update(
            hold_until=until,
            hold_token=token,
            hold_by=user if user and getattr(user, "is_authenticated", False) else None
        )

    return HoldResult(True, token=token)

def finalize_hold_to_paid_booking(
    *,
    token: str,
    booking,
    activity,
    date,
    start_dt,
    minutes_real: int,
    capture_buffer_if_gt30: bool = True,
):
    """
    ממיר HOLD → תפיסה אמיתית (booked/paid) באטומיות.
    """
    if not token:
        raise ValueError("missing_token")

    release_expired_holds()
    now = timezone.now()

    minutes_total = minutes_real + (15 if capture_buffer_if_gt30 and minutes_real > 30 else 0)
    slot_cnt = max(1, (int(minutes_total) + 14) // 15)
    times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_cnt)]

    with transaction.atomic():
        appts = list(
            Appointment.objects.select_for_update()
            .filter(date=date, time__in=times_needed)
            .order_by("time")
        )
        if len(appts) != slot_cnt:
            raise ValueError("missing_slots")

        # חייב להיות מוחזק ע״י הטוקן ובתוקף (או פנוי לגמרי — אם תרצי לאפשר fallback)
        for a in appts:
            if a.is_booked:
                raise ValueError("already_booked")
            if a.hold_until and a.hold_until > now:
                if str(a.hold_token) != str(token):
                    raise ValueError("held_by_other")
            else:
                # לא מוחזק בתוקף (פג/לא הוחזק) — אם את רוצה “חייב hold” אז תזרקי שגיאה
                raise ValueError("hold_expired")

        # תופסים באמת
        for a in appts:
            a.booking = booking
            a.is_booked = True
            a.is_paid = True
            a.is_break = False
            a.payment_reference = booking.payment_ref
            if a.activity_id != activity.id:
                a.activity = activity
            a.hold_until = None
            a.hold_token = None
            a.hold_by = None
            a.save(update_fields=[
                "booking","is_booked","is_paid","is_break","payment_reference","activity",
                "hold_until","hold_token","hold_by"
            ])
            if hasattr(a, "activities"):
                a.activities.add(activity)