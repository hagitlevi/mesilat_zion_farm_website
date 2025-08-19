from django.http import Http404, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from homePage.models import ActivityRule, BusinessHours, Season, Appointment, Activity
from datetime import datetime, timedelta, date, time
from django.shortcuts import render, get_object_or_404
from django.db.models import Q
from decimal import Decimal
import json
from zoneinfo import ZoneInfo
from .utils import group_consecutive_hours


VARIANT_TO_TYPE = {
    "day":     "רכיבה זוגית ביום",
    "sunrise": "רכיבה זוגית בזריחה",
    "night":   "רכיבה זוגית בלילה",
}

VARIANT_TO_TARGET_ACTIVITY_NAME = {
    "day":     None,              # סלוטים ללא שיוך
    "sunrise": "רכיבה בזריחה",
    "night":   "רכיבת לילה",
}



def home(request):
    hours_rows = build_business_hours_rows()         # שלך, מחזיר ראשון..שבת
    hours_rows = group_consecutive_hours(hours_rows) # קיבוץ רצפים זהים
    popup_payload = request.session.pop('payment_popup', None)  # קריאה חד-פעמית
    is_winter = (detect_season(timezone.localdate()) == Season.WINTER)
    return render(request, "homePage/home.html", {
        "hours_rows": hours_rows,    # שם המפתח לא משתנה → אין שינוי בתבנית
        "is_winter": is_winter,
        "payment_popup_json": json.dumps(popup_payload) if popup_payload else None,
    })

def riding_lessons_view(request):
    return render(request, 'homePage/riding_lessons.html')

def night_riding_view(request):
    if detect_season(timezone.localdate()) == Season.WINTER:
        raise Http404("רכיבת לילה אינה פעילה בחורף")
    activity = get_object_or_404(Activity, name="רכיבת לילה")
    return render(request, 'homePage/night_riding.html', {'activity': activity})

def sunrise_riding_view(request):
    activity = get_object_or_404(Activity, name="רכיבה בזריחה")
    return render(request, 'homePage/sunrise_riding.html', {'activity': activity})

def couple_riding_view(request):
    qs = Activity.objects.filter(name="רכיבה זוגית").order_by('id')
    activity = qs.first()
    if not activity:
        raise Http404("לא נמצאה פעילות 'רכיבה זוגית'")
    return render(request, 'homePage/couple_riding.html', {'activity': activity})

def group_riding_view(request):
    qs = Activity.objects.filter(name="רכיבת שטח").order_by('id')
    activity = qs.first()
    if not activity:
        raise Http404("לא נמצאה פעילות 'רכיבת שטח'")
    return render(request, 'homePage/group_riding.html', {'activity': activity})

def carriage_trip_view(request):
    qs = Activity.objects.filter(name="טיול כרכרה").order_by('id')
    activity = qs.first()  # תחזירי אחת – הראשונה
    if not activity:
        raise Http404("לא נמצאה פעילות 'טיול כירכרה'")
    return render(request, 'homePage/carriage_trip.html', {'activity': activity})

def photographs_view(request):
    qs = Activity.objects.filter(name="צילומים").order_by('id')
    activity = qs.first()  # תחזירי אחת – הראשונה
    if not activity:
        raise Http404("לא נמצאה פעילות 'צילומים'")
    return render(request, 'homePage/photographs.html', {'activity': activity})

def children_riding_view(request):
    activity = get_object_or_404(Activity, name="רכיבת ילדים")
    return render(request, 'homePage/children_riding.html', {'activity': activity})

def gallery_view(request):
    return render(request, 'homePage/gallery.html')

def _strict_window(activity, base_date):
    """
    מחזיר חלון זמנים קשיח (datetime start, datetime end) לפעילויות מיוחדות.
    אם אין חלון קשיח – מחזיר (None, None).
    הערה: 'רכיבת לילה' חוצה חצות → end הוא ביום הבא ב-00:00.
    """
    name = getattr(activity, "name", "")
    if name == "רכיבת לילה":
        start_dt = datetime.combine(base_date, time(20, 0))           # 20:00
        end_dt   = datetime.combine(base_date, time(0, 0)) + timedelta(days=1)  # 24:00 (00:00 שלמחרת)
        return start_dt, end_dt
    if name == "רכיבה בזריחה":
        start_dt = datetime.combine(base_date, time(5, 0))            # 05:00
        end_dt   = datetime.combine(base_date, time(8, 0))            # 08:00
        return start_dt, end_dt
    return None, None

def detect_season(d):
    """
    לוגיקה פשוטה: אפריל–ספטמבר = קיץ, אחרת חורף.
    אם יש אצלך לוגיקה אחרת/טבלת עונות – החליפי כאן.
    """
    return Season.SUMMER if 4 <= d.month <= 9 else Season.WINTER

def get_rules_for(activity, d):
    """
    מחזיר כללים ליום נתון ופעילות נתונה:
      assigned_only: האם להציג רק תורים שמשויכים לפעילות הזו
      cutoff_minutes: מינימום דקות מראש להזמנה
      win_start_dt: תחילת חלון (datetime) אם מוגדר
      win_end_dt:   סוף חלון (datetime) אם מוגדר
    סדר עדיפויות: ActivityRule (ספציפי לפעילות) → BusinessHours (כללי) → בלי כללים.
    """
    weekday = d.weekday()                     # Monday=0 ... Sunday=6
    season = detect_season(d)

    # 1) כלל ספציפי לפעילות (אם קיים ליום+עונה)
    arule = (ActivityRule.objects
             .filter(activity=activity, season=season, days__code=weekday)
             .first())

    if arule:
        assigned_only   = bool(arule.assigned_only)
        cutoff_minutes  = int(arule.booking_cutoff_minutes or 0)
        win_start_dt    = datetime.combine(d, arule.start_time) if arule.start_time else None

        if arule.end_time:
            # תמיכה ב"סוף היום" (24:00) באמצעות סימון end_is_midnight_next_day
            if arule.end_is_midnight_next_day:
                win_end_dt = datetime.combine(d, time(0, 0)) + timedelta(days=1)  # 00:00 שלמחרת
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
        win_start_dt   = datetime.combine(d, bh.start_time)
        win_end_dt     = datetime.combine(d, bh.end_time)
        # ברירת מחדל עבור שעות כלליות: לא מחייב שיוך, בלי cutoff
        return False, 0, win_start_dt, win_end_dt

    # 3) אין כלל – בלי חלון ובלי מגבלות
    return False, 0, None, None

def _durations_for_variant(variant: str):
    """
    משכים לטאב (יום/זריחה/לילה) מתוך 'רכיבה זוגית', עם פולבקים חכמים למקרה של כתיב/רווחים.
    """
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
        return mins or [30, 45, 60]

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

    return [60]

def available_appointment_view(request, activity_id):
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
        .order_by("date", "time")
        .distinct()
    )

    # ✅ מסנן תורים של היום עד שעתיים קדימה
    if selected_date is None or selected_date == today:
        base_qs = base_qs.exclude(date=today, time__lte=two_hours_ahead_time)



    # --- משכים + מקור סלוטים לפי הטאב ---
    if activity.name == "רכיבה זוגית" and variant in ("day", "sunrise", "night"):
        durations = _durations_for_variant(variant)
        target_activity_name = VARIANT_TO_TARGET_ACTIVITY_NAME[variant]

        if variant == "day":
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

    for appt in appts_qs:
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

        # בדיקה שהסיום לא חוצה סוף חלון
        for d in durations:
            end_dt = start_dt + timedelta(minutes=d)
            if apply_window and win_end_dt and end_dt > win_end_dt:
                continue
            grouped_appointments[d].append({
                "id": appt.id,
                "date": appt.date,
                "time": appt.time,
                "end_time": end_dt.time(),
            })

    context = {
        "activity": activity,
        "durations": durations,
        "grouped_appointments": grouped_appointments,
        "selected_date": selected_date,
    }
    return render(request, "homePage/available_appointment.html", context)

def _detect_season(d):
    # קיץ: אפריל–ספטמבר; תרגישי חופשי לשנות
    return Season.SUMMER if 4 <= d.month <= 9 else Season.WINTER

def build_business_hours_rows(season=None):
    """מחזיר רשימה מוכנה לתצוגה: [{label, closed, start, end}, ...] לפי ה-BusinessHours מהאדמין."""
    if season is None:
        season = _detect_season(timezone.localtime().date())

    # Monday=0 ... Sunday=6; נציג לפי ראשון..שבת
    day_map = {0: None, 1: None, 2: None, 3: None, 4: None, 5: None, 6: None}

    qs = BusinessHours.objects.filter(season=season).prefetch_related("days")
    for bh in qs:
        for wd in bh.days.all():     # wd.code: 0..6
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

def booking_form(request):
    appointment_id   = request.GET.get('appointment_id')
    activity_id      = request.GET.get('activity_id')
    duration_minutes = request.GET.get('duration_minutes')      # מחרוזת או None
    selected_type    = request.GET.get('activity_type')         # קוד activity_type שנבחר (אם נבחר)
    qty_raw          = request.GET.get('participants')          # כמות משתתפים (אם נבחרה)

    appointment = get_object_or_404(Appointment, id=appointment_id)
    activity    = get_object_or_404(Activity, id=activity_id)

    # 1) מסננים וריאציות לפי שם → משך (לא מסננים לפי סוג כדי להציג את כולן לבחירה)
    qs = Activity.objects.filter(name=activity.name)
    try:
        if duration_minutes:
            qs = qs.filter(duration_minutes=int(duration_minutes))
    except (TypeError, ValueError):
        pass

    # 2) וריאציות רלוונטיות להצגה ולחישוב
    variants = list(
        qs.values('activity_type', 'price', 'price').order_by('activity_type')
    )

    # 3) מיפוי קוד → תווית ידידותית, ורשימת אופציות לטמפלייט (כולל מחיר ליחידה לכל סוג)
    choices_map = dict(Activity._meta.get_field('activity_type').choices)
    type_options = []
    for v in variants:
        unit = v['price'] or v['price']
        code = v['activity_type']
        type_options.append({
            'code': code,
            'label': choices_map.get(code, code),
            'unit_price': unit,
        })

    # 4) unit_price לפי סדר עדיפויות: (א) וריאציה יחידה, (ב) נבחר סוג, (ג) כל הווריאציות באותו מחיר
    unit_price = None
    if len(variants) == 1:
        unit_price = variants[0]['price'] or variants[0]['price']
    elif selected_type:
        for v in variants:
            if v['activity_type'] == selected_type:
                unit_price = v['price'] or v['price']
                break
    else:
        prices = { (v['price'] or v['price']) for v in variants }
        if len(prices) == 1:
            unit_price = prices.pop()

    # 5) כמות שנבחרה
    if activity.min_participants == activity.max_participants:
        selected_participants = activity.min_participants
    else:
        selected_participants = int(qty_raw) if (qty_raw and qty_raw.isdigit()) else None

    # 6) סיכום כולל (רק אם יש מחיר ליחידה וגם יש כמות)
    total_price = None
    if unit_price is not None and selected_participants and activity.name != "טיול כרכרה":
        total_price = Decimal(str(unit_price)) * Decimal(selected_participants)
    else:
        total_price = Decimal(str(unit_price))
    # 7) טווח לבחירה בכמות
    participants_range = range(activity.min_participants, activity.max_participants + 1)

    return render(request, 'homePage/user_details.html', {
        'appointment': appointment,
        'activity': activity,
        'duration': duration_minutes,
        'participants_range': participants_range,
        'type_options': type_options,
        'selected_type': selected_type,
        'unit_price': unit_price,
        'selected_participants': selected_participants,
        'total_price': total_price,
    })

@csrf_exempt
def confirm_booking(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    # שדות שכבר יש לך בטופס:
    appointment_id   = request.POST.get("appointment_id")
    activity_id      = request.POST.get("activity_id")
    duration_minutes = request.POST.get("duration_minutes")
    first_name       = request.POST.get("first_name")
    last_name        = request.POST.get("last_name")
    phone            = request.POST.get("phone")
    gmail            = request.POST.get("gmail")
    participants     = request.POST.get("participants")
    activity_type    = request.POST.get("activity_type")

    # *** החדשה: בחירת אמצעי תשלום ***
    payment_method   = request.POST.get("payment_method")  # 'credit_card' / 'bit' / 'paybox'

    # ולידציה בסיסית בצד שרת
    if payment_method not in ("credit_card", "bit", "paybox"):
        return HttpResponseBadRequest("Payment method is required")

    # דוגמאות לשימוש:
    # 1) שמירה במסד (אם יש שדה מתאים במודל ההזמנה שלך)
    # booking.payment_method = payment_method
    # booking.save()

    # 2) או העברת זה לדף סיכום/תשלום:
    appointment = get_object_or_404(Appointment, id=appointment_id)
    activity    = get_object_or_404(Activity, id=activity_id)

    context = {
        "appointment": appointment,
        "activity": activity,
        "duration_minutes": duration_minutes,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "gmail": gmail,
        "participants": participants,
        "activity_type": activity_type,
        "payment_method": payment_method,
    }
    return render(request, "homePage/payment_page.html", context)



import math
from datetime import datetime, timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

@transaction.atomic
@transaction.atomic
def mock_payment_success(request):
    """
    מסמן תשלום כהושלם, תופס סלוטים בני 15 דק' לפי duration_minutes,
    ואז מפנה למסך הבית עם פופאפ 'תשלום בוצע בהצלחה' (דרך session).
    מצופה לקבל ב-GET: appointment_id, duration_minutes, (אופציונלי) participants, total_price, payment_ref.
    """

    # --- עזר להמרות בטוחות ---
    def _to_int(val, default=None, min_value=None):
        if val in (None, ""):
            return default
        try:
            n = int(val)
            if min_value is not None and n < min_value:
                return default
            return n
        except (TypeError, ValueError):
            return default

    def _to_str_or_none(val):
        return val if (val not in (None, "")) else None

    # --- קריאת פרמטרים בצורה חסינה ---
    appointment_id   = _to_int(request.GET.get("appointment_id"),   default=None, min_value=1)
    duration_minutes = _to_int(request.GET.get("duration_minutes"), default=None, min_value=1)
    participants     = _to_int(request.GET.get("participants"),     default=1,    min_value=1)
    total_price      = _to_str_or_none(request.GET.get("total_price"))
    payment_ref      = _to_str_or_none(request.GET.get("payment_ref")) or f"MOCK-{timezone.now().strftime('%Y%m%d%H%M%S')}"

    if appointment_id is None or duration_minutes is None:
        request.session['payment_popup'] = {
            "type": "error",
            "title": "שגיאה בתשלום",
            "text": "חסר/שגוי: appointment_id או duration_minutes",
        }
        return redirect("home")

    # --- שליפת הסלוט הראשי ונעילה למניעת מרוצי עדכון ---
    base_appt = get_object_or_404(Appointment.objects.select_for_update(), id=appointment_id)

    # --- חישוב מספר הסלוטים לפי 15 דק' ---
    slot_count = max(1, math.ceil(duration_minutes / 15))

    base_dt = datetime.combine(base_appt.date, base_appt.time)
    times_needed = [(base_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

    # --- האם יש שדות מיוחדים במודל? ---
    field_names = {f.name for f in Appointment._meta.fields}
    has_is_break = "is_break" in field_names
    has_payment_reference = "payment_reference" in field_names

    # --- שליפת הסלוטים הנדרשים לאותו תאריך ושעות ---
    qs = Appointment.objects.select_for_update().filter(
        date=base_appt.date,
        time__in=times_needed,
    )
    if has_is_break:
        qs = qs.filter(is_break=False)

    appts = list(qs)
    if len(appts) != slot_count:
        request.session['payment_popup'] = {
            "type": "error",
            "title": "לא נתפס",
            "text": "לא נמצאו כל סלוטי הזמן הנדרשים. נסי לבחור תור אחר.",
        }
        return redirect("home")

    if any(getattr(a, "is_booked", False) for a in appts):
        request.session['payment_popup'] = {
            "type": "error",
            "title": "לא נתפס",
            "text": "חלק מהסלוטים כבר נתפסו. נסי לבחור תור אחר.",
        }
        return redirect("home")

    # --- עדכון כל הסלוטים: שולם + נתפס (+ רפרנס אם קיים) ---
    update_fields = ["is_paid", "is_booked"]
    if has_payment_reference:
        update_fields.append("payment_reference")

    for a in appts:
        a.is_paid = True
        a.is_booked = True
        if has_payment_reference:
            a.payment_reference = payment_ref
        a.save(update_fields=update_fields)

    # --- מטען לפופאפ הביתה דרך session ---
    request.session['payment_popup'] = {
        "type": "success",
        "title": "התשלום בוצע בהצלחה ✅",
        "ref": payment_ref,
        "date": base_appt.date.isoformat(),
        "times": [t.strftime("%H:%M") for t in times_needed],
        "duration_minutes": duration_minutes,
        "participants": participants,
        "total_price": total_price,  # יכול להיות None
    }

    return redirect("home")

