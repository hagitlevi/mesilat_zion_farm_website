from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from datetime import datetime, time as dtime, timedelta
from homePage.models import ActivityRule, BusinessHours, Season, Activity, Appointment, Booking, Payment
import logging

logger = logging.getLogger(__name__)

VARIANT_TO_TYPE = {
    "day":     "couple",
    "sunrise": "couple_sunrise",
    "night":   "couple_night",
    "picnic":  "couple_picnic",
}

def _capture_slots_and_break(appt, duration_minutes, booking, activity=None, payment_ref=None):
  """
  תופסת רצף סלוטים של 15 דק' לפי משך, ומוסיפה הפסקה של 15 דק' אם צריך.
  מסמנת את הסלוטים כ-is_booked=True, is_paid=True (לא להפסקה), is_break=False.
  מחזירה (times_captured, extra_break) כאשר extra_break הוא ה-Appointment של ההפסקה אם נתפס.
  """
  logger.debug("_capture_slots_and_break called with appt id: %s, duration_minutes: %s, booking id: %s, activity id: %s",)

  if duration_minutes is None:
    duration_minutes = 60

  slot_count = max(1, (int(duration_minutes) + 14) // 15)
  base_dt = datetime.combine(appt.date, appt.time)
  times_needed = [(base_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

  # שליפת כל הסלוטים הרצופים ונעילה
  chain_qs = (Appointment.objects
              .select_for_update()
              .filter(date=appt.date,
                      time__in=times_needed,
                      is_break=False))
  # אם כבר נתפסו עבור אותו Booking — נאפשר לעבור, אחרת נדרוש פנוי
  chain = list(chain_qs)
  if len(chain) != slot_count or any(a.is_booked and a.booking_id != getattr(booking, 'id', None) for a in chain):
    raise ValueError("חסרים סלוטים פנויים לרצף שביקשת.")

  # סימון סלוטי המפגש
  for a in chain:
    a.booking = booking
    a.is_paid = True
    a.is_booked = True
    a.is_break = False
    if payment_ref and hasattr(a, "payment_reference"):
      a.payment_reference = payment_ref
    if activity and getattr(a, "activity_id", None) != activity.id and hasattr(a, "activity_id"):
      a.activity = activity
    a.save(update_fields=[f for f in ["booking", "is_paid", "is_booked", "is_break", "payment_reference", "activity"]
                          if hasattr(a, f)])

    # אם יש לך M2M של slots על Booking — נחבר
    if hasattr(booking, "slots"):
      try:
        booking.slots.add(a)
      except Exception:
        pass

    # ואם יש M2M activities על Appointment — לשיוך
    if activity and hasattr(a, "activities"):
      try:
        a.activities.add(activity)
      except Exception:
        pass

  # תפיסת ההפסקה של 15 דק' אם משך>30
  extra_appt = None
  if int(duration_minutes) > 30:
    extra_start_dt = base_dt + timedelta(minutes=15 * slot_count)
    extra = (Appointment.objects
             .select_for_update()
             .filter(date=appt.date,
                     time=extra_start_dt.time(),
                     is_booked=False,
                     is_break=False)
             .first())
    if extra:
      extra.booking = booking
      extra.is_paid = False
      extra.is_booked = True
      extra.is_break = True
      if activity and getattr(extra, "activity_id", None) != activity.id and hasattr(extra, "activity_id"):
        extra.activity = activity
      extra.save(update_fields=[f for f in ["booking", "is_paid", "is_booked", "is_break", "activity"]
                                if hasattr(extra, f)])
      extra_appt = extra
      # לחיבור ל-M2M אם קיים
      if hasattr(booking, "slots"):
        try:
          booking.slots.add(extra)
        except Exception:
          pass

  return times_needed, extra_appt

def _finalize_booking_after_payment(payment, pending_extra=None):
    """
    סוגרת תור, מוצאת/יוצרת Booking, מקשרת אותו ל-Payment, ותופסת את כל הסלוטים לפי המשך.
    אידמפוטנטית ככל האפשר.
    """
    logger.debug("_finalize_booking_after_payment called with payment id: %s", getattr(payment, "id", None))

    with transaction.atomic():
      appt = None
      if getattr(payment, "appointment_id", None):
        appt = Appointment.objects.select_for_update().filter(id=payment.appointment_id).first()

      # איתור/יצירת Booking כפי שהיה
      if getattr(payment, "booking_id", None):
        booking = payment.booking
      else:
        booking = None
        try:
          booking = Booking.objects.select_for_update().filter(payment=payment).first()
        except Exception:
          pass
        if not booking and appt is not None:
          try:
            booking = Booking.objects.select_for_update().filter(slots=appt).first()
          except Exception:
            pass

      if not booking:

        activity = _resolve_activity_for_booking(payment, appt)

        if appt is not None:
          start_dt = getattr(appt, "start_dt", None) or datetime.combine(appt.date, appt.time)
          if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        else:
          raise ValueError("Payment בלי appointment_id: אי אפשר לייצר Booking בלי זמנים.")

        minutes = (getattr(payment, "duration_minutes", None)
                   or getattr(appt, "duration_minutes", None)
                   or getattr(activity, "duration_minutes", None)
                   or 60)
        end_dt = start_dt + timedelta(minutes=minutes)

        total_price = (Decimal(payment.amount_agorot) / Decimal("100")) if getattr(payment, "amount_agorot",
                                                                                   None) is not None else None

        extra = pending_extra or {}

        activity_type = (extra.get("activity_type") or "").strip().lower()
        wine = (extra.get("wine") or "").strip().lower()

        details_parts = []

        if activity.name == "צילומים" and activity_type:
          color_map = {
            "white": "סוס לבן",
            "brown": "סוס חום",
          }
          details_parts.append(f"צבע סוס: {color_map.get(activity_type, activity_type)}")

        if wine in {"white", "red", "none"}:
          wine_map = {
            "white": "יין לבן",
            "red": "יין אדום",
            "none": "בלי יין",
          }
          details_parts.append(f"יין: {wine_map[wine]}")

        details_txt = " | ".join(details_parts)

        booking = Booking.objects.create(
          activity=activity,
          start_dt=start_dt,
          end_dt=end_dt,
          participants=getattr(payment, "participants", 1),
          customer_name=getattr(payment, "customer_name", ""),
          customer_phone=getattr(payment, "phone", ""),
          customer_email=getattr(payment, "email", ""),
          status="confirmed",
          total_price=total_price,
          details=details_txt,
        )

        if hasattr(payment, "booking_id"):
          payment.booking = booking
          payment.save(update_fields=["booking"])

      # --- כאן תופסים את כל הסלוטים לפי המשך הנכון ---
      if appt is not None:
        minutes = (getattr(payment, "duration_minutes", None)
                   or getattr(appt, "duration_minutes", None)
                   or getattr(booking.activity, "duration_minutes", None)
                   or 60)
        try:
          _capture_slots_and_break(
            appt=appt,
            duration_minutes=minutes,
            booking=booking,
            activity=getattr(booking, "activity", None),
            payment_ref=getattr(payment, "charge_id", None) or None
          )
        except ValueError:
          # אם אין רצף פנוי — לפחות נסמן את בסיס התור כתפוס (שמירה על ההתנהגות הישנה)
          if hasattr(appt, "is_booked") and not appt.is_booked:
            appt.is_booked = True
            appt.save(update_fields=["is_booked"])

        # עדכון end_dt ב-Booking אם לא תאם למשך בפועל
        start_dt = getattr(booking, "start_dt", None)
        if start_dt:
          desired_end = start_dt + timedelta(minutes=minutes)
          if getattr(booking, "end_dt", None) != desired_end:
            try:
              booking.end_dt = desired_end
              booking.save(update_fields=["end_dt"])
            except Exception:
              pass

      return booking

def _capture_trailing_quarter_slot_as_break(base_appt, slot_count, field_names, activity_obj):
    """
    מנסה לתפוס סלוט נוסף של 15 דק' מיד אחרי סוף התור ולסמן אותו כהפסקה.
    מחזיר את הרשומה שתפס (Appointment) או None אם לא נתפס.
    - מסמן: is_booked=True, is_paid=False, is_break=True (+ us_break=True אם יש)
    - מקשר פעילות אם יש activity או activities.
    """
    logger.debug("_capture_trailing_quarter_slot_as_break called with base_appt id: %s, slot_count: %s, field_names: %s", getattr(base_appt, "id", None), slot_count, field_names)

    extra_start_dt = datetime.combine(base_appt.date, base_appt.time) + timedelta(minutes=15 * slot_count)
    extra_time = extra_start_dt.time()

    qs = Appointment.objects.select_for_update().filter(date=base_appt.date, time=extra_time)

    # אל תתפוס הפסקות קיימות מכל סוג
    if "is_break" in field_names:
        qs = qs.filter(is_break=False)
    if "us_break" in field_names:
        qs = qs.filter(us_break=False)

    extra = qs.first()
    if not extra or getattr(extra, "is_booked", False):
        return None

    update_fields = []

    # תפיסת הסלוט כהפסקה (לא משולם)
    if "is_booked" in field_names:
        extra.is_booked = True
        update_fields.append("is_booked")
    if "is_paid" in field_names:
        extra.is_paid = False
        update_fields.append("is_paid")

    if "is_break" in field_names:
        extra.is_break = True
        update_fields.append("is_break")
    if "us_break" in field_names:
        extra.us_break = True
        update_fields.append("us_break")

    # פעילות (FK)
    if activity_obj and "activity" in field_names:
        extra.activity = activity_obj
        update_fields.append("activity")

    extra.save(update_fields=update_fields)

    # פעילות (M2M)
    if activity_obj and hasattr(extra, "activities"):
        extra.activities.add(activity_obj)

    return extra

def _resolve_activity_for_booking(payment: Payment, appt: Appointment | None):
  """מנסה לקבוע את ה-Activity המתאים להזמנה לפי סדר עדיפויות:"""
  logger.debug("_resolve_activity_for_booking called with payment id: %s, appt id: %s", getattr(payment, "id", None), getattr(appt, "id", None))

  # 1) הכי טוב: מהתור (אם קיים ועליו FK לפעילות)
  if appt is not None:
    a = getattr(appt, "activity", None)
    if a:
      return a
    # לפעמים יש activity_id על ה-Appointment
    aid = getattr(appt, "activity_id", None)
    if aid:
      a = Activity.objects.filter(id=aid).first()
      if a:
        return a

  # 2) פולבאק: מתוך ה-Payment (יש לך IntegerField בשם activity_id)
  if getattr(payment, "activity_id", None):
    a = Activity.objects.filter(id=payment.activity_id).first()
    if a:
      return a

  raise ValueError("אין דרך לקבוע Activity להזמנה (Appointment בלי פעילות ו-Payment.activity_id ריק).")

def _pick_booking_status(booking_model, *candidates):
    """
    מחזיר סטטוס ראשון מתוך candidates שקיים בבחירות של המודל (אם יש choices),
    אחרת מחזיר את הראשון.
    """
    logger.debug("_pick_booking_status called with candidates: %s", candidates)

    try:
      choices = {c[0] for c in booking_model._meta.get_field("status").choices or []}
    except Exception:
      choices = set()
    for c in candidates:
      if not choices or c in choices:
        return c
    return candidates[0]

def build_business_hours_rows(season=None):
  """מחזיר רשימה מוכנה לתצוגה: [{label, closed, start, end}, ...] לפי ה-BusinessHours מהאדמין."""
  logger.debug("build_business_hours_rows called with season: %s", season)

  if season is None:
    # קובע עונה לפי שינוי השעון בישראל (Asia/Jerusalem) בזמן המקומי הנוכחי
    now_local = timezone.localtime()
    season = "summer" if (now_local.dst() or timedelta(0)) != timedelta(0) else "winter"

  # Monday=0 ... Sunday=6; נציג לפי ראשון..שבת
  day_map = {0: None, 1: None, 2: None, 3: None, 4: None, 5: None, 6: None}

  qs = BusinessHours.objects.filter(season=season).prefetch_related("days")
  for bh in qs:
    for wd in bh.days.all():  # wd.code: 0..6
      cur = day_map[wd.code]
      if cur is None:
        day_map[wd.code] = (bh.start_time, bh.end_time)
      else:
        s0, e0 = cur
        day_map[wd.code] = (min(s0, bh.start_time), max(e0, bh.end_time))

  heb = {6: "ראשון", 0: "שני", 1: "שלישי", 2: "רביעי", 3: "חמישי", 4: "שישי", 5: "שבת"}
  order = [6, 0, 1, 2, 3, 4, 5]

  rows = []
  for code in order:
    rng = day_map[code]
    if rng is None:
      rows.append({"label": heb[code], "closed": True, "start": None, "end": None})
    else:
      rows.append({"label": heb[code], "closed": False, "start": rng[0], "end": rng[1]})
  return rows

def detect_season(d=None):
    """
    ▼ גרסה זמנית תואמת-ישראל (DST):
    - אם d=None → מחליטה לפי עכשיו
    - אם d הוא date → מחליטה לפי 12:00 באותו יום (נמנע מנפילה על רגע המעבר)
    """
    logger.debug("detect_season called with date: %s", d)

    tz = ZoneInfo("Asia/Jerusalem")
    if d is None:
      local = timezone.localtime()
      is_summer = (local.dst() or timedelta(0)) != timedelta(0)
    else:
      noon_local = datetime.combine(d, dtime(12, 0)).replace(tzinfo=tz)
      is_summer = (noon_local.dst() or timedelta(0)) != timedelta(0)

    return Season.SUMMER if is_summer else Season.WINTER

def get_rules_for(activity, d):
  """
  מחזיר כללים ליום נתון ופעילות נתונה:
    assigned_only: האם להציג רק תורים שמשויכים לפעילות הזו
    cutoff_minutes: מינימום דקות מראש להזמנה
    win_start_dt: תחילת חלון (datetime) אם מוגדר
    win_end_dt:   סוף חלון (datetime) אם מוגדר
  סדר עדיפויות: ActivityRule (ספציפי לפעילות) → BusinessHours (כללי) → בלי כללים.
  """
  logger.debug("get_rules_for called with activity id: %s, date: %s", getattr(activity, "id", None), d)

  weekday = d.weekday()  # Monday=0 ... Sunday=6

  # === שינוי קטן: קובע עונה לפי מעבר השעון בישראל (DST) ===
  # משתמשים ב-12:00 באותו יום כדי לא ליפול בדיוק על שעת המעבר.
  tz = ZoneInfo("Asia/Jerusalem")
  noon_local = datetime.combine(d, dtime(12, 0)).replace(tzinfo=tz)
  season = "summer" if (noon_local.dst() or timedelta(0)) != timedelta(0) else "winter"

  # 1) כלל ספציפי לפעילות (אם קיים ליום+עונה)
  arule = (ActivityRule.objects
           .filter(activity=activity, season=season, days__code=weekday)
           .first())

  if arule:
    assigned_only = bool(arule.assigned_only)
    cutoff_minutes = int(arule.booking_cutoff_minutes or 0)
    win_start_dt = datetime.combine(d, arule.start_time) if arule.start_time else None

    if arule.end_time:
      # תמיכה ב"סוף היום" (24:00) באמצעות סימון end_is_midnight_next_day
      if getattr(arule, "end_is_midnight_next_day", False):
        win_end_dt = datetime.combine(d, dtime(0, 0)) + timedelta(days=1)  # 00:00 שלמחרת
      else:
        win_end_dt = datetime.combine(d, arule.end_time)
    else:
      win_end_dt = None

    return assigned_only, cutoff_minutes, win_start_dt, win_end_dt

  # 2) fallback לשעות כלליות של העסק (לפי עונה+יום)
  bh = (BusinessHours.objects
        .filter(season=season, days__code=weekday)
        .first())

  if bh:
    win_start_dt = datetime.combine(d, bh.start_time)
    win_end_dt = datetime.combine(d, bh.end_time)
    # ברירת מחדל עבור שעות כלליות: לא מחייב שיוך, בלי cutoff
    return False, 0, win_start_dt, win_end_dt

  # 3) אין כלל – בלי חלון ובלי מגבלות
  return False, 0, None, None

def _durations_for_variant(variant: str):
    """
    משכים לטאב (יום/זריחה/לילה) מתוך 'רכיבה זוגית', עם פולבקים חכמים למקרה של כתיב/רווחים.
    """
    logger.debug("_durations_for_variant called with variant: %s", variant)

    def distinct_minutes(qs):
      return sorted(set(qs.values_list("duration_minutes", flat=True)))

    if variant == "day":
      qs = Activity.objects.filter(name="רכיבה זוגית", activity_type__iexact=VARIANT_TO_TYPE["day"])
      mins = distinct_minutes(qs)
      if mins: return mins
      qs = (Activity.objects.filter(name="רכיבה זוגית", activity_type__icontains="יום")
            .exclude(activity_type__icontains="לילה")
            .exclude(activity_type__icontains="זריחה"))
      mins = distinct_minutes(qs)
      return mins or [30, 45, 60, 90, 120]

    if variant == "picnic":
      qs = Activity.objects.filter(
        name="רכיבה זוגית",
        activity_type__iexact=VARIANT_TO_TYPE["picnic"]
      )
      mins = distinct_minutes(qs)
      if mins:
        return mins

      # fallback חכם אם כתבו קצת אחרת
      qs = Activity.objects.filter(name="רכיבה זוגית", activity_type__icontains="פיקניק")
      mins = distinct_minutes(qs)
      return mins or [120]

    if variant == "sunrise":
      qs = Activity.objects.filter(name="רכיבה זוגית", activity_type__iexact=VARIANT_TO_TYPE["sunrise"])
      mins = distinct_minutes(qs)
      if mins: return mins
      qs = Activity.objects.filter(name="רכיבה זוגית", activity_type__icontains="זריחה")
      mins = distinct_minutes(qs)
      return mins or [90]

    if variant == "night":
      qs = Activity.objects.filter(name="רכיבה זוגית", activity_type__iexact=VARIANT_TO_TYPE["night"])
      mins = distinct_minutes(qs)
      if mins: return mins
      qs = Activity.objects.filter(name="רכיבה זוגית", activity_type__icontains="לילה")
      mins = distinct_minutes(qs)
      return mins or [90]

    return []

