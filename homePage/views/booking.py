import logging

from homePage.models import Activity, Appointment
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q, Max
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.db import transaction
from django.contrib import messages
from django.utils import timezone
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from uuid import uuid4
from ..services.booking_service import detect_season, get_rules_for, _durations_for_variant
from ..views.consent import _has_consent_by_phone, _save_consent_by_phone
from homePage.services.ntfy_gateway import normalize_phone_il
from homePage.models import Season, MarketingConsent
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

VARIANT_TO_TYPE = {
    "day":     "couple",
    "sunrise": "couple_sunrise",
    "night":   "couple_night",
    "picnic":  "couple_picnic",
}
VARIANT_TO_TARGET_ACTIVITY_NAME = {
    "day":     None,
    "picnic":  None,
    "sunrise": "רכיבה בזריחה",
    "night":   "רכיבת לילה",
}

def confirm_booking(request):
  """טיפול ב-POST מהטופס, כולל בדיקת הסכמה לפי טלפון (ללא קוקיז) לפני המשך ליצירת דף תשלום"""
  logger.debug("confirm_booking called with method: %s", request.method)

  if request.method != "POST":
    return HttpResponseBadRequest("Invalid method")

  # שדות מהטופס:
  appointment_id = request.POST.get("appointment_id")
  activity_id = request.POST.get("activity_id")
  duration_minutes = request.POST.get("duration_minutes")
  first_name = request.POST.get("first_name")
  last_name = request.POST.get("last_name")
  phone_raw = request.POST.get("phone")
  email = request.POST.get("email", "")
  participants = request.POST.get("participants")
  activity_type = request.POST.get("activity_type")
  wine = request.POST.get("wine")

  # --- שלב 5: אכיפת הסכמה לפי טלפון (ללא קוקיז) ---
  full_name = " ".join(x for x in [(first_name or "").strip(), (last_name or "").strip()] if x)
  phone_norm = normalize_phone_il(phone_raw)
  if not _has_consent_by_phone(phone_norm):
    # אם אין רישום הסכמה לגרסאות הנוכחיות – חייב לבוא accept_terms מהטופס
    if not request.POST.get("accept_terms"):
      # מחזירים את המשתמש לדף המילוי עם פרמטר שגיאה + כל מה שצריך ב-GET
      qs = urlencode({
        "appointment_id": appointment_id or "",
        "activity_id": activity_id or "",
        "duration_minutes": duration_minutes or "",
        "participants": participants or "",
        "activity_type": activity_type or "",
        "wine": wine or "",
        "consent_error": "1",
      })
      return redirect(f"{reverse('booking_form')}?{qs}")

    else:
      # יש הסכמה חדשה – נשמור אותה ב-DB (כאן עדיין אין Booking; מעביר None)
      _save_consent_by_phone(request, phone=phone_norm, full_name=full_name)

  # --- המשך הזרימה שלך כרגיל (יצירת דף תשלום) ---
  appointment = get_object_or_404(Appointment, id=appointment_id)
  activity = get_object_or_404(Activity, id=activity_id)
  total_price = request.POST.get("total_price")

  context = {
    "appointment": appointment,
    "activity": activity,
    "duration_minutes": duration_minutes,
    "first_name": first_name,
    "last_name": last_name,
    "phone": phone_raw,
    "email": email,
    "participants": participants,
    "activity_type": activity_type,
    "total_price": total_price,
    "wine": wine,
  }
  return render(request, "homePage/mock_checkout.html", context)

@transaction.atomic
def booking_form(request):
    """טיפול ב-GET להצגת הטופס, כולל API פנימי לבדיקות AJAX (הסכמה, מחיר, שיווק)"""
    logger.debug("booking_form called with method: %s", request.method)

    variant_q = (request.GET.get("variant") or "").lower()
    if request.GET.get("ajax") == "consent":
        phone = request.GET.get("phone", "")
        need = not _has_consent_by_phone(phone)
        return JsonResponse({
            "needs_consent": need,
            "versions": {
                "terms": getattr(settings, "TERMS_VERSION", "1.0"),
                "privacy": getattr(settings, "PRIVACY_VERSION", "1.0"),
            }
        })
    elif request.GET.get("ajax") == "price":
        appointment_id = request.GET.get("appointment_id")
        activity_id = request.GET.get("activity_id")
        duration_minutes = request.GET.get("duration_minutes")
        selected_type = request.GET.get("activity_type")
        qty_raw = request.GET.get("participants")
        variant_q = (request.GET.get("variant") or "").lower()

        appointment = get_object_or_404(Appointment, id=appointment_id)
        activity = get_object_or_404(Activity, id=activity_id)

        # --- אותה לוגיקה בדיוק כמו אצלך בהמשך (רק בקיצור) ---
        variant = variant_q
        if activity.name == "רכיבה זוגית" and not variant:
            variant = "day"

        allow_wine = (activity.name == "רכיבה זוגית" and variant == "picnic")

        qs = Activity.objects.filter(name=activity.name)
        try:
            if duration_minutes:
                qs = qs.filter(duration_minutes=int(duration_minutes))
        except (TypeError, ValueError):
            pass

        if activity.name == "רכיבה זוגית" and variant in VARIANT_TO_TYPE:
            qs = qs.filter(activity_type__iexact=VARIANT_TO_TYPE[variant])

        # type_options (כמו אצלך)
        variants = list(qs.values('activity_type', 'price').order_by('activity_type'))
        choices_map = dict(Activity._meta.get_field('activity_type').choices)
        type_options = [{
            "code": v["activity_type"],
            "label": choices_map.get(v["activity_type"], v["activity_type"]),
            "unit_price": v["price"],
        } for v in variants]

        # משתתפים
        if activity.min_participants == activity.max_participants:
            selected_participants = activity.min_participants
        else:
            selected_participants = int(qty_raw) if (qty_raw and qty_raw.isdigit()) else None

        # unit_price
        unit_price = None
        if selected_type:
            unit_price = next(
                (v["price"] for v in variants if v["activity_type"] == selected_type and v["price"] is not None), None)

        # total_price
        total_price = None
        if unit_price is not None:
            if activity.name != "טיול כרכרה" and selected_participants:
                total_price = unit_price * selected_participants
            else:
                total_price = unit_price

        return JsonResponse({
            "ok": True,
            "unit_price": str(unit_price) if unit_price is not None else None,
            "total_price": str(total_price) if total_price is not None else None,
            "selected_participants": selected_participants,
            "allow_wine": allow_wine,
        })
    elif request.GET.get("ajax") == "marketing_exists":
        raw_p = request.GET.get("phone", "") or ""
        phone = normalize_phone_il(raw_p)  # אותו נירמול כמו ב-pay_start
        email = (request.GET.get("email") or "").strip().lower()

        obj = MarketingConsent.objects.filter(subject_id=phone).first() if phone else None

        # לוגיקה:
        # אין רשומה לטלפון → להציג (true)
        # יש רשומה בלי מייל → להציג (true) כדי להשלים מייל
        # יש רשומה עם מייל → לא להציג (false)
        show_checkbox = True
        if obj and (obj.customer_email or "").strip():
            show_checkbox = False

        return JsonResponse({"show_checkbox": show_checkbox})

    appointment_id   = request.GET.get('appointment_id')
    activity_id      = request.GET.get('activity_id')
    duration_minutes = request.GET.get('duration_minutes')      # מחרוזת או None
    selected_type    = request.GET.get('activity_type')         # קוד activity_type שנבחר (אם נבחר)
    qty_raw          = request.GET.get('participants')          # כמות משתתפים (אם נבחרה)

    appointment = get_object_or_404(Appointment, id=appointment_id)
    activity    = get_object_or_404(Activity, id=activity_id)

    # ===================== HOLD CHECK (לשים כאן) =====================
    now = timezone.now()
    token = request.session.get("hold_token")  # אותו טוקן ששמרת כשעשית hold

    # אם אין טוקן בסשן – זה אומר שהמשתמש הגיע בלי HOLD (או סשן חדש)
    if not token:
        messages.error(request, "כדי להמשיך חייבים לבחור תור מחדש.")
        return redirect(request.META.get("HTTP_REFERER", "home"))

    # אם אין hold_until או שהוא פג
    if (not appointment.hold_until) or (appointment.hold_until <= now):
        messages.error(request, "השמירה על התור פגה / ארעה שגיאה. בבקשה לבחור תור מחדש.")
        return redirect(request.META.get("HTTP_REFERER", "home"))

    # אם התור מוחזק ע״י מישהו אחר (טוקן שונה)
    if str(appointment.hold_token) != str(token):
        messages.error(request, "מישהו אחר תפס את התור כרגע. בבקשה לבחור תור אחר.")
        return redirect(request.META.get("HTTP_REFERER", "home"))
    # =================== END HOLD CHECK ===================


    # קביעה מאיזה וריאנט הגענו (רק לזוגית)
    variant = variant_q
    is_couple_day = False
    if activity.name == "רכיבה זוגית":
        if not variant:
            appt_names = set()
            if hasattr(appointment, "activities"):
                appt_names = set(appointment.activities.values_list("name", flat=True))

            if "רכיבה בזריחה" in appt_names:
                variant = "sunrise"
            elif "רכיבת לילה" in appt_names:
                variant = "night"
            else:
                is_couple_day = True
                variant = "day"

    # חסימת הזמנה ישירה לרכיבת לילה בחורף (גם אם הגיעו עם URL ידני)
    try:
        appt_day = appointment.date  # Appointment כאן הוא המודל עם date/time
    except Exception:
        appt_day = None

    if (activity.name == "רכיבת לילה" or variant == "night") and appt_day:
        if detect_season(appt_day) == Season.WINTER:
            raise Http404("רכיבת לילה אינה פעילה בחורף")


    # אם זו רכיבה זוגית ביום והאורך לפחות 90 דק' — מותר לבחור יין
    try:
        minutes = int(duration_minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    # יין תמיד רק בפיקניק (בלי קשר לאורך)
    is_couple_picnic = (activity.name == "רכיבה זוגית" and variant == "picnic")
    allow_wine = is_couple_picnic

    qs = Activity.objects.filter(name=activity.name)
    try:
        if duration_minutes:
            qs = qs.filter(duration_minutes=int(duration_minutes))
    except (TypeError, ValueError):
        pass

    # צמצום ספציפי לזוגית:
    if activity.name == "רכיבה זוגית":
        if variant in VARIANT_TO_TYPE:
            qs = qs.filter(activity_type__iexact=VARIANT_TO_TYPE[variant])

        elif variant == "day":
            qs = (qs.exclude(activity_type__icontains="couple_sunrise")
                  .exclude(activity_type__icontains="couple_night")
                  .exclude(activity_type__icontains="couple_picnic"))
        elif variant == "sunrise":
            qs = qs.filter(activity_type__icontains="couple_sunrise")
        elif variant == "night":
            qs = qs.filter(activity_type__icontains="couple_night")

    is_couple_day = (activity.name == "רכיבה זוגית" and variant == "day")
    # === בחירת Activity מדויקת להזמנה ===
    chosen_activity = None

    try:
        chosen_activity = qs.get()
    except Activity.MultipleObjectsReturned:
        print("⚠️ MULTIPLE ACTIVITIES MATCH:", list(qs.values("id", "activity_type", "duration_minutes")))
        chosen_activity = qs.order_by("id").first()
    except Activity.DoesNotExist:
        print("❌ NO ACTIVITY MATCH")
    # 2) וריאציות רלוונטיות להצגה ולחישוב (בלי כפילות price)
    variants = list(
        qs.values('activity_type', 'price').order_by('activity_type')
    )

    # 3) מיפוי קוד → תווית ידידותית, ואיסוף מחירים לאופציות בטמפלט
    choices_map = dict(Activity._meta.get_field('activity_type').choices)
    type_options = []
    for v in variants:
        unit = v['price']  # מגיע כ-Decimal מה-ORM (או None)
        code = v['activity_type']
        type_options.append({
            'code': code,
            'label': choices_map.get(code, code),
            'unit_price': unit,
        })

    # 4) קביעה בטוחה של unit_price או טווח מחירים (כשיש וריאציות במחיר)
    prices_qs = list(
        qs.exclude(price__isnull=True).values_list('price', flat=True).distinct()
    )
    unit_price = None
    price_min = price_max = None

    # מחיר יחידה – לזוגית לפי הסינון שכבר עשינו ל-qs (כולל picnic)
    if activity.name == "רכיבה זוגית" and variant in ("day", "sunrise", "night", "picnic") and duration_minutes:
        try:
            d = int(duration_minutes)
            unit_price = (qs.filter(duration_minutes=d)
                          .exclude(price__isnull=True)
                          .values_list("price", flat=True)
                          .first())
        except (TypeError, ValueError):
            unit_price = None

    # אם המשתמש בחר activity_type מפורש – נכבד אותו (עדיין מתוך qs המסונן)
    if unit_price is None and selected_type:
        unit_price = next(
            (v['price'] for v in variants
             if v['activity_type'] == selected_type and v['price'] is not None),
            None
        )

    # ואם עדיין אין – ננסה להסיק ממחיר יחיד בכל הווריאציות ב-qs (אותו משך)
    if unit_price is None:
        if len(prices_qs) == 1:
            unit_price = prices_qs[0]
        elif len(prices_qs) > 1:
            price_min, price_max = min(prices_qs), max(prices_qs)

    # אחרון: רק אם עדיין לא נמצא מחיר, ניפול למחיר מתוך "רכיבת לילה"/"רכיבה בזריחה"
    if (unit_price is None and activity.name == "רכיבה זוגית"
            and variant in ("night", "sunrise") and duration_minutes):
        try:
            d = int(duration_minutes)
            fallback_name = "רכיבת לילה" if variant == "night" else "רכיבה בזריחה"
            unit_price = (Activity.objects
                          .filter(name=fallback_name, duration_minutes=d)
                          .exclude(price__isnull=True)
                          .values_list('price', flat=True)
                          .first())
        except (TypeError, ValueError):
            pass

    # 5) כמות משתתפים שנבחרה
    if activity.min_participants == activity.max_participants:
        selected_participants = activity.min_participants
    else:
        selected_participants = int(qty_raw) if (qty_raw and qty_raw.isdigit()) else None

    # 6) מחיר כולל — רק אם יש מחיר יחידה ברור
    #    פעילויות שמחירן פר-הזמנה (כמו "טיול כרכרה") לא מוכפלות בכמות
    total_price = None
    if unit_price is not None:
        if activity.name != "טיול כרכרה" and selected_participants:
            # unit_price הוא Decimal מה-ORM; כפל ב-int מחזיר Decimal
            total_price = unit_price * selected_participants
        else:
            total_price = unit_price


    wine = request.GET.get("wine") if allow_wine else None
    if wine not in {"white", "red", "none"}:
        wine = None


    # 7) טווח לבחירת כמות לתבנית
    participants_range = range(activity.min_participants, activity.max_participants + 1)
    show_selector = (
        (len(type_options) > 1 and activity.name != "רכיבה זוגית") or
        (activity.min_participants != activity.max_participants) or
        allow_wine
    )

    # זמן התחלה
    start_dt = datetime.combine(appointment.date, appointment.time)

    # משך בדקות
    try:
        minutes_int = int(duration_minutes or 0)
    except (TypeError, ValueError):
        minutes_int = 0


    # זמן סוף
    end_dt = start_dt + timedelta(minutes=minutes_int) if minutes_int else None

    start_time_str = appointment.time.strftime("%H:%M") if appointment.time else ""
    end_time_str = end_dt.strftime("%H:%M") if end_dt else ""

    # 8) החזרת הקשר לטמפלט
    return render(request, 'homePage/user_details.html', {
        'chosen_activity_id': chosen_activity.id if chosen_activity else activity.id,
        'show_selector': show_selector,
        'allow_wine': allow_wine,
        'appointment': appointment,
        'activity': activity,
        'duration': duration_minutes,
        'participants_range': participants_range,
        'type_options': type_options,
        'selected_type': selected_type,
        'unit_price': unit_price,                 # מחיר יחידה אם קיים
        'price_min': price_min,                   # אם אין unit_price – הציגי טווח
        'price_max': price_max,
        'selected_participants': selected_participants,
        'total_price': total_price,               # Decimal או None
        'wine': wine,
        'is_couple_day': is_couple_day,
        'start_time': start_time_str,
        'end_time': end_time_str,
        'appt_date': appointment.date,
        'variant': variant,
    })

def available_appointment_view(request, activity_id):
  """טיפול ב-GET להצגת התורים הפנויים לפעילות מסוימת, עם לוגיקה של זמינות לפי שעה, עונה, וריאנט וכו'"""
  logger.debug("available_appointment_view called with method: %s", request.method)

  activity = get_object_or_404(Activity, id=activity_id)
  variant = (request.GET.get("variant") or "").lower()

  # --- טווח תאריכים ---
  date_str = request.GET.get("date")
  if date_str:
    try:
      selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
      selected_date = date.today()
    start_day = end_day = selected_date
  else:
    selected_date = None
    start_day = date.today()
    end_day = start_day + timedelta(days=7)

  # ✅ חדש/מסודר: לחשב פעם אחת
  now_aw = timezone.now().astimezone(ZoneInfo("Asia/Jerusalem"))
  today = now_aw.date()
  now_naive = datetime.combine(today, now_aw.time())
  two_hours_ahead_time = (now_aw + timedelta(hours=2)).time()

  base_qs = (
    Appointment.objects
    .filter(is_booked=False, is_break=False, date__range=(start_day, end_day))
    .filter(Q(hold_until__isnull=True) | Q(hold_until__lte=now_aw))
    .order_by("date", "time")
    .distinct()
  )

  # ✅ מסנן תורים של היום עד שעתיים קדימה
  if selected_date is None or selected_date == today:
    base_qs = base_qs.exclude(date=today, time__lte=two_hours_ahead_time)

  # --- אם זו רכיבה בזריחה/לילה (או זוגית בטאבים sunrise/night) ואחרי 16:00 — לא מציגים תורים של היום ---
  # --- כללי 16:00 ---
  # אם זו רכיבת "זריחה" (כולל זוגית-זריחה) ואחרי 16:00,
  # לא מציגים תורים גם להיום וגם למחרת.
  # לרכיבת לילה (כולל זוגית-לילה) נשאיר את הכלל הקיים: אחרי 16:00 לא מציגים היום.
  tomorrow = today + timedelta(days=1)

  is_sunrise = (
      activity.name == "רכיבה בזריחה"
      or (activity.name == "רכיבה זוגית" and variant == "sunrise")
  )
  is_night = (
      activity.name == "רכיבת לילה"
      or (activity.name == "רכיבה זוגית" and variant == "night")
  )

  #  חסימה קבועה: רכיבת לילה לא פעילה בחורף (גם לזוגית-לילה)
  if is_night:
    if selected_date:
      if detect_season(selected_date) == Season.WINTER:
        base_qs = base_qs.none()
    else:
      # תצוגת טווח (שבוע) – אם עכשיו חורף, אין בכלל תורי לילה
      if detect_season() == Season.WINTER:
        base_qs = base_qs.none()

  if now_aw.hour >= 16:
    # זריחה: לחסום היום + מחר
    if is_sunrise:
      if selected_date is None:
        # תצוגת טווח (שבוע) – תסנני גם היום וגם מחר
        base_qs = base_qs.exclude(date=today).exclude(date=tomorrow)
      elif selected_date == today:
        base_qs = base_qs.exclude(date=today)
      elif selected_date == tomorrow:
        # אם בחרו ספציפית "מחר" – תחזירי ריק
        base_qs = base_qs.none()

    # לילה: כמו קודם – אחרי 16:00 לא מציגים היום
    if is_night:
      if selected_date:
        # אם המשתמש בחר תאריך ספציפי – בודקים את העונה של אותו יום
        if detect_season(selected_date) == Season.WINTER:
          base_qs = base_qs.none()
      else:
        # תצוגת טווח (שבוע): אם עכשיו חורף – לא מציגים לילות בכלל
        if detect_season() == Season.WINTER:
          base_qs = base_qs.none()
      if selected_date is None or selected_date == today:
        base_qs = base_qs.exclude(date=today)

  # --- משכים + מקור סלוטים לפי הטאב ---
  if activity.name == "רכיבה זוגית" and variant in ("day", "sunrise", "night", "picnic"):
    durations = _durations_for_variant(variant)
    target_activity_name = VARIANT_TO_TARGET_ACTIVITY_NAME[variant]

    if variant in ("day", "picnic"):
      appts_qs = base_qs.filter(activities__isnull=True)
      rules_activity = activity
      apply_window, apply_cutoff = True, False
    else:
      target_acts = Activity.objects.filter(name=target_activity_name)
      appts_qs = base_qs.filter(activities__in=target_acts)
      rules_activity = target_acts.first()
      apply_window, apply_cutoff = True, True
  else:
    durations = sorted(set(
      Activity.objects.filter(name=activity.name)
      .values_list("duration_minutes", flat=True)
      .distinct()
    )) or [getattr(activity, "duration_minutes", 60)]

    if activity.name in {"רכיבת לילה", "רכיבה בזריחה"}:
      appts_qs = base_qs.filter(activities=activity)
    else:
      appts_qs = base_qs.filter(Q(activities__isnull=True) | Q(activities=activity))
    rules_activity = activity
    apply_window, apply_cutoff = True, False

  # --- אינדקס שעות פנויות לכל יום מתוך appts_qs לאחר כל הפילטרים ---
  appts_list = list(appts_qs)  # נשתמש גם לבניית הסט וגם ללולאה
  free_times_by_date = {}
  for a in appts_list:
    free_times_by_date.setdefault(a.date, set()).add(a.time)

  rules_cache = {}  # date -> (cutoff_min, win_start_dt, win_end_dt)

  def rules_for_date(the_date: date):
    if the_date in rules_cache:
      return rules_cache[the_date]
    if rules_activity:
      _, cutoff_min, win_start_dt, win_end_dt = get_rules_for(rules_activity, the_date)
    else:
      cutoff_min, win_start_dt, win_end_dt = 0, None, None
    rules_cache[the_date] = (cutoff_min or 0, win_start_dt, win_end_dt)
    return rules_cache[the_date]

  grouped_appointments = {d: [] for d in durations}

  for appt in appts_list:
    start_dt = datetime.combine(appt.date, appt.time)
    cutoff_min, win_start_dt, win_end_dt = rules_for_date(appt.date)

    # ✅ ביטוח: לא להציג תורים של היום עד שעתיים קדימה
    if appt.date == today and start_dt <= (now_naive + timedelta(hours=2)):
      continue

    # חלון התחלה
    if apply_window and win_start_dt and start_dt < win_start_dt:
      continue

    # cutoff (למשל שעה מראש) – רק בטאבים הרלוונטיים ועל היום הנוכחי
    if apply_cutoff and appt.date == today and cutoff_min > 0:
      if start_dt < now_naive + timedelta(minutes=cutoff_min):
        continue

    # --- NEW: בדיקת רצף סלוטים פנויים לכל משך d + הפסקת 15 דק' אם d>30 ---
    free_set = free_times_by_date.get(appt.date, set())

    for d in durations:
      end_dt = start_dt + timedelta(minutes=d)

      # חלון סוף (ללא ההפסקה)
      if apply_window and win_end_dt and end_dt > win_end_dt:
        continue

      # כמה סלוטים של 15 דק' נדרשים למפגש עצמו
      slots_needed = (d + 14) // 15  # ceil(d/15)

      # כל סלוטי המפגש חייבים להיות פנויים
      ok = True
      for i in range(slots_needed):
        t = (start_dt + timedelta(minutes=15 * i)).time()
        if t not in free_set:
          ok = False
          break
      if not ok:
        continue

      # אם המפגש ארוך מ-30 דק׳ – נדרשת גם "הפסקה" של 15 דק׳ בסוף
      if d > 30:
        buffer_start_dt = start_dt + timedelta(minutes=15 * slots_needed)  # מיד אחרי סוף המפגש
        # אם יש חלון, ודאי שגם ההפסקה נכנסת לתוכו
        if apply_window and win_end_dt and (buffer_start_dt + timedelta(minutes=15)) > win_end_dt:
          continue
        buffer_time = buffer_start_dt.time()
        # ההפסקה חייבת להיות סלוט פנוי (כלומר קיימת ב-appts_qs ולא תפוסה/הפסקה)
        if buffer_time not in free_set:
          continue

      # אם הגענו לכאן – יש רצף תקין וגם הפסקת 15 דק׳ (אם צריך)
      grouped_appointments[d].append({
        "id": appt.id,
        "date": appt.date,
        "time": appt.time,
        "end_time": end_dt.time(),
      })

  total_slots = sum(len(v) for v in grouped_appointments.values())
  has_slots = total_slots > 0
  context = {
    "activity": activity,
    "durations": durations,
    "grouped_appointments": grouped_appointments,
    "selected_date": selected_date,
    "has_slots": has_slots,
  }
  return render(request, "homePage/available_appointment.html", context)

@require_GET
def appointments_snapshot(request):
    """API פנימי ללקוח AJAX לקבלת סלוטים פנויים בזמן אמת, כולל חישוב stamp אמיתי מבסיס הנתונים"""
    logger.debug("appointments_snapshot called with method: %s", request.method)

    activity_id = request.GET.get("activity_id")
    date_str = request.GET.get("date")

    now_aw = timezone.now().astimezone(ZoneInfo("Asia/Jerusalem"))

    qs = Appointment.objects.filter(
        is_booked=False,
        is_break=False
    ).filter(
        Q(hold_until__isnull=True) | Q(hold_until__lte=now_aw)
    )

    if date_str:
        qs = qs.filter(date=date_str)

    # ✅ stamp אמיתי
    stamp = qs.aggregate(max_updated=Max("updated_at"))["max_updated"]

    slots = list(
        qs.values("id", "date", "time")
        .order_by("date", "time")
    )

    return JsonResponse({
        "stamp": stamp.isoformat() if stamp else None,
        "slots": slots
    })

@require_POST
def renew_hold(request):
    """API פנימי ללקוח AJAX לחדש את ה-HOLD על תור ספציפי, עם בדיקות אבטחה (טוקן, תור קיים, לא תפוס, לא פג)"""
    logger.debug("renew_hold called with method: %s", request.method)

    appointment_id = request.POST.get("appointment_id")
    if not appointment_id:
        return JsonResponse({"ok": False, "msg": "missing appointment_id"}, status=400)

    token = request.session.get("hold_token")
    if not token:
        return JsonResponse({"ok": False, "msg": "no session token"}, status=400)

    now = timezone.now()
    HOLD_MINUTES = 15

    with transaction.atomic():
        appt = get_object_or_404(Appointment.objects.select_for_update(), id=appointment_id)

        # חייב להיות אותו טוקן (כלומר זה באמת אותה הזמנה)
        if str(appt.hold_token) != str(token):
            return JsonResponse({"ok": False, "msg": "token mismatch"}, status=409)

        appt.hold_until = now + timedelta(minutes=HOLD_MINUTES)
        appt.save(update_fields=["hold_until"])

    return JsonResponse({"ok": True, "hold_until": appt.hold_until.isoformat()})

@require_POST
@transaction.atomic
def release_hold(request):
    """API פנימי ללקוח AJAX לשחרור ה-HOLD (למשל אם המשתמש ביטל או סיים), עם בדיקות אבטחה (טוקן, תורים קיימים עם הטוקן)"""
    logger.debug("release_hold called with method: %s", request.method)

    token = request.session.get("hold_token")
    if not token:
        return JsonResponse({"ok": True, "released": 0})

    now = timezone.now()

    qs = Appointment.objects.select_for_update().filter(
        hold_token=token,
        is_booked=False,
        hold_until__gt=now,
    )

    released = qs.update(hold_until=None, hold_token=None)
    return JsonResponse({"ok": True, "released": released})

def _is_ajax(request):
  """פונקציה עזר פנימית לזיהוי קריאות AJAX, עם הדפסה לוגית לבדיקה"""
  logger.debug("_is_ajax called with headers: %s", request.headers)

  return request.headers.get("x-requested-with") == "XMLHttpRequest"

@require_POST
@transaction.atomic
def hold_appointment(request):
    """API פנימי ללקוח AJAX להחזקת תור ספציפי, עם כל הלוגיקה של בדיקות זמינות, רצף סלוטים, הפסקות, טוקן בסשן, ותגובה מותאמת ל-AJAX או ל-redirect רגיל"""
    logger.debug("hold_appointment called with method: %s", request.method)

    appt_id = int(request.POST.get("appointment_id"))
    duration = int(request.POST.get("duration_minutes", "60"))
    now = timezone.now()
    hold_minutes = 15
    token = request.session.get("hold_token")

    if not token:
        token = str(uuid4())
        request.session["hold_token"] = token

    activity_id = int(request.POST.get("activity_id"))
    date_q = request.POST.get("date", "")
    variant_q = request.POST.get("variant", "")

    def ok_redirect(url):
        if _is_ajax(request):
            return JsonResponse({"ok": True, "redirect_url": url})
        return redirect(url)

    def err(msg):
        if _is_ajax(request):
            return JsonResponse({"ok": False, "message": msg}, status=409)
        messages.error(request, msg)
        base_url = reverse("available_appointment", kwargs={"activity_id": activity_id})
        qs = urlencode({"date": date_q, "variant": variant_q})
        return redirect(f"{base_url}?{qs}")

    base = Appointment.objects.select_for_update().get(id=appt_id)

    # ✅ אם מוחזק ע"י מישהו אחר ועדיין לא פג
    if base.hold_until and base.hold_until > now and str(base.hold_token) != token:
        return err("מישהו אחר כבר תפס את התור הזה. בבקשה לבחור שעה אחרת.")

    if base.is_booked or base.is_break:
        return err("מישהו אחר תפס את התור. בבקשה לבחור שעה אחרת.")

    base_dt = datetime.combine(base.date, base.time)
    slot_count = max(1, (duration + 14) // 15)
    times_needed = [(base_dt + timedelta(minutes=15*i)).time() for i in range(slot_count)]

    need_break = duration > 30
    if need_break:
        break_time = (base_dt + timedelta(minutes=15*slot_count)).time()
        times_needed.append(break_time)

    qs = (Appointment.objects.select_for_update()
          .filter(date=base.date, time__in=times_needed, is_break=False))

    chain = list(qs)
    if len(chain) != len(times_needed):
        return err("השעה הזו כבר לא זמינה למשך שבחרת. בבקשה לבחור שעה אחרת.")

    for a in chain:
        if a.is_booked or a.is_break:
            return err("התור נתפס ממש עכשיו. בבקשה לבחור שעה אחרת.")
        if a.hold_until and a.hold_until > now and str(a.hold_token) != token:
            return err("מישהו אחר מחזיק את התור כרגע. בבקשה לבחור שעה אחרת.")

    until = now + timedelta(minutes=hold_minutes)
    for a in chain:
        a.hold_token = token
        a.hold_until = until
        if not a.hold_created_at:
            a.hold_created_at = now
        a.save(update_fields=["hold_token", "hold_until", "hold_created_at", "updated_at"])

    # הצלחה → מחזירים URL לדף הפרטים
    qs = urlencode({
        "appointment_id": appt_id,
        "activity_id": activity_id,
        "duration_minutes": duration,
        "variant": variant_q
    })
    return ok_redirect(f"{reverse('booking_form')}?{qs}")