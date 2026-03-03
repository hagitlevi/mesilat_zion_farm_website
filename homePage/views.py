from homePage.models import ActivityRule, BusinessHours, Season, Activity, Appointment, Booking, Payment, SiteReview, CancellationRequest, TermsConsent, MarketingConsent
from .forms import SiteReviewForm, CancelRequestForm
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_http_methods, require_POST, require_GET
from django.db.models import Q, Avg, Max
from django.core.paginator import Paginator
from zoneinfo import ZoneInfo
from .utils import group_consecutive_hours
import json
import secrets
from homePage.utils import assign_unique_ref
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from django.db import transaction
from django.contrib import messages
from datetime import date, datetime, time as dtime, timedelta
import time as time_mod
from homePage.services.ntfy_gateway import format_booking_sms, send_booking_email, normalize_phone_il
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from uuid import uuid4
from django.utils import timezone


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



def home(request):
    print("DEBUG: home view called")  # הדפסה לוגית לבדיקה
    hours_rows = build_business_hours_rows()         # שלך, מחזיר ראשון..שבת
    hours_rows = group_consecutive_hours(hours_rows) # קיבוץ רצפים זהים
    popup_payload = request.session.pop('payment_popup', None)  # קריאה חד-פעמית
    is_winter = ((timezone.localtime().dst() or timedelta(0)) == timedelta(0))
    latest_reviews = list(SiteReview.objects.order_by('-created_at')[:4])  # ← 4 אחרונות
    reviews_total  = SiteReview.objects.count()
    reviews_avg    = SiteReview.objects.aggregate(avg=Avg('rating'))['avg'] or 0
    return render(request, "homePage/home.html", {
        "hours_rows": hours_rows,    # שם המפתח לא משתנה → אין שינוי בתבנית
        "is_winter": is_winter,
        "payment_popup_json": json.dumps(popup_payload) if popup_payload else None,
        "latest_reviews": latest_reviews,
        "reviews_total": reviews_total,
        "reviews_avg": reviews_avg,
    })

def riding_lessons_view(request):
    print("DEBUG: riding_lessons_view called")  # הדפסה לוגית לבדיקה
    activity = get_object_or_404(Activity, name="שיעורי רכיבה/ טיפולית")
    return render(request, 'homePage/riding_lessons.html', {'activity': activity})

def night_riding_view(request):
    print("DEBUG: night_riding_view called")  # הדפסה לוגית לבדיקה
    is_winter_now = ((timezone.localtime().dst() or timedelta(0)) == timedelta(0))
    if is_winter_now:
        raise Http404("‌رכיבת לילה אינה פעילה בחורף")

    activity = get_object_or_404(Activity, name="רכיבת לילה")
    return render(request, "homePage/night_riding.html", {"activity": activity})

def sunrise_riding_view(request):
    print("DEBUG: sunrise_riding_view called")  # הדפסה לוגית לבדיקה
    activity = get_object_or_404(Activity, name="רכיבה בזריחה")
    return render(request, 'homePage/sunrise_riding.html', {'activity': activity})

def couple_riding_view(request):
    print("DEBUG: couple_riding_view called")  # הדפסה לוגית לבדיקה
    activity = (
        Activity.objects
        .filter(name="רכיבה זוגית", activity_type="couple")  # יום רגיל כברירת מחדל
        .first()
    )

    if not activity:
        activity = Activity.objects.filter(name="רכיבה זוגית").order_by("id").first()
    if not activity:
        raise Http404("לא נמצאה פעילות 'רכיבה זוגית'")
    return render(request, 'homePage/couple_riding.html', {'activity': activity})

def group_riding_view(request):
    print("DEBUG: group_riding_view called")  # הדפסה לוגית לבדיקה
    qs = Activity.objects.filter(name="רכיבת שטח").order_by('id')
    activity = qs.first()
    if not activity:
        raise Http404("לא נמצאה פעילות 'רכיבת שטח'")
    return render(request, 'homePage/group_riding.html', {'activity': activity})

def carriage_trip_view(request):
    print("DEBUG: carriage_trip_view called")  # הדפסה לוגית לבדיקה
    qs = Activity.objects.filter(name="טיול כרכרה").order_by('id')
    activity = qs.first()  # תחזירי אחת – הראשונה
    if not activity:
        raise Http404("לא נמצאה פעילות 'טיול כירכרה'")
    return render(request, 'homePage/carriage_trip.html', {'activity': activity})

def photographs_view(request):
    print("DEBUG: photographs_view called")  # הדפסה לוגית לבדיקה
    qs = Activity.objects.filter(name="צילומים").order_by('id')
    activity = qs.first()  # תחזירי אחת – הראשונה
    if not activity:
        raise Http404("לא נמצאה פעילות 'צילומים'")
    return render(request, 'homePage/photographs.html', {'activity': activity})

def children_riding_view(request):
    print("DEBUG: children_riding_view called")  # הדפסה לוגית לבדיקה
    activity = get_object_or_404(Activity, name="רכיבת ילדים")
    return render(request, 'homePage/children_riding.html', {'activity': activity})

def gallery_view(request):
    print("DEBUG: gallery_view called")  # הדפסה לוגית לבדיקה
    return render(request, 'homePage/gallery.html')

def _strict_window(activity, base_date):
    """
    מחזיר חלון זמנים קשיח (datetime start, datetime end) לפעילויות מיוחדות.
    אם אין חלון קשיח – מחזיר (None, None).
    הערה: 'רכיבת לילה' חוצה חצות → end הוא ביום הבא ב-00:00.
    """
    print("DEBUG: _strict_window called for activity:", getattr(activity, "name", None), "and date:", base_date)  # הדפסה לוגית לבדיקה
    name = getattr(activity, "name", "")
    if name == "רכיבת לילה":
        start_dt = datetime.combine(base_date, dtime(20, 0))           # 20:00
        end_dt   = datetime.combine(base_date, dtime(0, 0)) + timedelta(days=1)  # 24:00 (00:00 שלמחרת)
        return start_dt, end_dt
    if name == "רכיבה בזריחה":
        start_dt = datetime.combine(base_date, dtime(5, 0))            # 05:00
        end_dt   = datetime.combine(base_date, dtime(8, 0))            # 08:00
        return start_dt, end_dt
    return None, None

def detect_season(d=None):
    """
    ▼ גרסה זמנית תואמת-ישראל (DST):
    - אם d=None → מחליטה לפי עכשיו
    - אם d הוא date → מחליטה לפי 12:00 באותו יום (נמנע מנפילה על רגע המעבר)
    """
    print("DEBUG: detect_season called with date:", d)  # הדפסה לוגית לבדיקה
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
    print("DEBUG: get_rules_for called for activity:", getattr(activity, "name", None), "and date:", d)  # הדפסה לוגית לבדיקה
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
        assigned_only   = bool(arule.assigned_only)
        cutoff_minutes  = int(arule.booking_cutoff_minutes or 0)
        win_start_dt    = datetime.combine(d, arule.start_time) if arule.start_time else None

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
        win_end_dt   = datetime.combine(d, bh.end_time)
        # ברירת מחדל עבור שעות כלליות: לא מחייב שיוך, בלי cutoff
        return False, 0, win_start_dt, win_end_dt

    # 3) אין כלל – בלי חלון ובלי מגבלות
    return False, 0, None, None

def _durations_for_variant(variant: str):
    """
    משכים לטאב (יום/זריחה/לילה) מתוך 'רכיבה זוגית', עם פולבקים חכמים למקרה של כתיב/רווחים.
    """
    print("DEBUG: _durations_for_variant called with variant:", variant)  # הדפסה לוגית לבדיקה
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

def build_business_hours_rows(season=None):
    """מחזיר רשימה מוכנה לתצוגה: [{label, closed, start, end}, ...] לפי ה-BusinessHours מהאדמין."""
    print("DEBUG: build_business_hours_rows called with season:", season)  # הדפסה לוגית לבדיקה
    if season is None:
        # קובע עונה לפי שינוי השעון בישראל (Asia/Jerusalem) בזמן המקומי הנוכחי
        now_local = timezone.localtime()
        season = "summer" if (now_local.dst() or timedelta(0)) != timedelta(0) else "winter"

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

def available_appointment_view(request, activity_id):
    print("DEBUG: available_appointment_view called with activity_id:", activity_id)  # הדפסה לוגית לבדיקה
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

@csrf_exempt
def confirm_booking(request):
    print("DEBUG: confirm_booking called with method:", request.method)  # הדפסה לוגית לבדיקה
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    # שדות מהטופס:
    appointment_id   = request.POST.get("appointment_id")
    activity_id      = request.POST.get("activity_id")
    duration_minutes = request.POST.get("duration_minutes")
    first_name       = request.POST.get("first_name")
    last_name        = request.POST.get("last_name")
    phone_raw        = request.POST.get("phone")
    email            = request.POST.get("email") or request.POST.get("email")  # תואם לאחור
    participants     = request.POST.get("participants")
    activity_type    = request.POST.get("activity_type")
    wine             = request.POST.get("wine")

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
    activity    = get_object_or_404(Activity, id=activity_id)
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

# --- עזר: לתפוס 15 דק' אחרי סוף התור ולהפוך אותן ל"הפסקה" ---
def _capture_trailing_quarter_slot_as_break(base_appt, slot_count, field_names, activity_obj):
    """
    מנסה לתפוס סלוט נוסף של 15 דק' מיד אחרי סוף התור ולסמן אותו כהפסקה.
    מחזיר את הרשומה שתפס (Appointment) או None אם לא נתפס.
    - מסמן: is_booked=True, is_paid=False, is_break=True (+ us_break=True אם יש)
    - מקשר פעילות אם יש activity או activities.
    """
    print("DEBUG: _capture_trailing_quarter_slot_as_break called with base_appt id:", getattr(base_appt, "id", None), "slot_count:", slot_count, "field_names:", field_names)  # הדפסה לוגית לבדיקה
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

def _capture_slots_and_break(appt, duration_minutes, booking, activity=None, payment_ref=None):
    """
    תופסת רצף סלוטים של 15 דק' לפי משך, ומוסיפה הפסקה של 15 דק' אם צריך.
    מסמנת את הסלוטים כ-is_booked=True, is_paid=True (לא להפסקה), is_break=False.
    מחזירה (times_captured, extra_break) כאשר extra_break הוא ה-Appointment של ההפסקה אם נתפס.
    """
    print("DEBUG: _capture_slots_and_break called with appt id:", getattr(appt, "id", None), "duration_minutes:", duration_minutes, "booking id:", getattr(booking, 'id', None), "activity id:", getattr(activity, 'id', None))  # הדפסה לוגית לבדיקה
    if duration_minutes is None:
        duration_minutes = 60

    slot_count = max(1, (int(duration_minutes) + 14) // 15)
    base_dt = datetime.combine(appt.date, appt.time)
    times_needed = [(base_dt + timedelta(minutes=15*i)).time() for i in range(slot_count)]

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
        a.save(update_fields=[f for f in ["booking","is_paid","is_booked","is_break","payment_reference","activity"]
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
            extra.save(update_fields=[f for f in ["booking","is_paid","is_booked","is_break","activity"]
                                      if hasattr(extra, f)])
            extra_appt = extra
            # לחיבור ל-M2M אם קיים
            if hasattr(booking, "slots"):
                try:
                    booking.slots.add(extra)
                except Exception:
                    pass

    return times_needed, extra_appt

@transaction.atomic
def mock_payment_success(request):
    """
    יוצר Booking, תופס סלוטים של 15 דק' לפי duration_minutes,
    מסמן אותם כשולמו/נתפסו, ותופס עוד 15 דק' בסוף כהפסקה (is_break=True) אם > 30 דקות.
    לא מעדכן name/phone על Appointment.
    שומר popup ב-session ומפנה ל-home.

    מצפה: appointment_id, duration_minutes, (אופציונלי) participants, total_price, activity_id, payment_method, payment_ref,
           (אופציונלי) customer_name / customer_phone / customer_email (ל-Booking).
    """
    print("DEBUG: mock_payment_success called with method:", request.method)  # הדפסה לוגית לבדיקה
    data = request.GET if request.method == "GET" else request.POST

    def _to_int(v, default=None, min_value=None):
        if v in (None, ""): return default
        try:
            n = int(v)
            if min_value is not None and n < min_value: return default
            return n
        except (TypeError, ValueError):
            return default

    def _str(v): return (v or "").strip()

    appointment_id   = _to_int(data.get("appointment_id"),   min_value=1)
    duration_minutes = _to_int(data.get("duration_minutes"), min_value=1)
    participants     = _to_int(data.get("participants"),     default=1, min_value=1)
    total_price_str  = _str(data.get("total_price"))
    activity_id      = _to_int(data.get("activity_id"),      min_value=1)
    payment_method = "manual"
    payment_ref      = _str(data.get("payment_ref")) or f"MOCK-{timezone.now().strftime('%Y%m%d%H%M%S')}"

    first_name = _str(data.get("first_name"))
    last_name = _str(data.get("last_name"))

    # פרטי לקוח ל-Booking (לא ל-Appointment)
    customer_name = _str(data.get("customer_name")) or " ".join(x for x in [first_name, last_name] if x)
    customer_phone = _str(data.get("customer_phone")) or _str(data.get("phone"))
    customer_email = _str(data.get("customer_email")) or _str(data.get("email")) or _str(data.get("email"))

    if not appointment_id or not duration_minutes:
        request.session['payment_popup'] = {"type":"error","title":"שגיאה","text":"חסרים פרמטרים"}
        return redirect("home")

    base_appt = get_object_or_404(Appointment.objects.select_for_update(), id=appointment_id)
    activity  = Activity.objects.filter(id=activity_id).first() if activity_id else None

    # כמה סלוטים של 15 דק' צריך
    slot_count = max(1, (duration_minutes + 14) // 15)
    base_dt = datetime.combine(base_appt.date, base_appt.time)
    times_needed = [(base_dt + timedelta(minutes=15*i)).time() for i in range(slot_count)]

    # שליפת סלוטים לפגישה (פנויים, לא הפסקות)
    qs = Appointment.objects.select_for_update().filter(
        date=base_appt.date,
        time__in=times_needed,
        is_booked=False,
        is_break=False,
    )
    appts = list(qs)
    if len(appts) != slot_count or any(a.is_booked for a in appts):
        request.session['payment_popup'] = {"type":"error","title":"לא נתפס","text":"חסרים סלוטים פנויים"}
        return redirect("home")

    # יצירת ההזמנה
    try:
        total_price = Decimal(total_price_str) if total_price_str else None
    except Exception:
        total_price = None


    wine = (data.get("wine") or "").strip().lower()   # חדש
    if wine not in ("white", "red", "none"):
        wine = ""
    details_payload = {}
    if wine:
        wine_he = {"white": "יין לבן", "red": "יין אדום", "none": "בלי יין"}[wine]
        details_payload["wine"] = wine_he
    details_txt = f"יין: {wine_he}" if wine else None


    booking = Booking.objects.create(
        activity=activity or base_appt.activity,  # fallback אם יש FK על הסלוט
        customer_name=customer_name,
        customer_phone=customer_phone,
        customer_email=customer_email,
        participants=participants or 1,
        total_price=total_price,
        payment_method=payment_method,
        payment_ref=payment_ref,
        status="paid",
        start_dt=base_dt,
        end_dt=base_dt + timedelta(minutes=duration_minutes),
        details=details_txt,
    )

    # סימון סלוטי המפגש: שולם/נתפס, קישור להזמנה, לא הפסקה
    for a in appts:
        a.booking = booking
        a.is_paid = True
        a.is_booked = True
        a.is_break = False
        a.payment_reference = payment_ref
        if activity and a.activity_id != activity.id:
            a.activity = activity
        a.save(update_fields=["booking", "is_paid", "is_booked", "is_break", "payment_reference", "activity"])

        # אם יש M2M activities ואת רוצה לשייך – אפשר:
        if activity and hasattr(a, "activities"):
            a.activities.add(activity)

    # תפיסת "הפסקה" של 15 דק' אחרי סוף התור אם > 30 דק'
    extra_appt = None
    if duration_minutes > 30:
        extra_start_dt = base_dt + timedelta(minutes=15 * slot_count)
        extra = Appointment.objects.select_for_update().filter(
            date=base_appt.date,
            time=extra_start_dt.time(),
            is_booked=False,
            is_break=False,
        ).first()
        if extra:
            extra.booking = booking
            extra.is_paid = False
            extra.is_booked = True
            extra.is_break = True
            if activity and extra.activity_id != activity.id:
                extra.activity = activity
            extra.save(update_fields=["booking", "is_paid", "is_booked", "is_break", "activity"])
            extra_appt = extra
            # אופציונלי: להציג גם את תחילת ההפסקה בפופאפ
            times_needed.append(extra_start_dt.time())

    booking.payment_ref = "MZ-" + "".join(secrets.choice("0123456789") for _ in range(8))
    booking.save(update_fields=["payment_ref"])

    # פופאפ הצלחה
    request.session['payment_popup'] = {
        "type": "success",
        "title": "התשלום בוצע בהצלחה ✅",
        "ref":  booking.payment_ref,
        "date": base_appt.date.isoformat(),
        "times": [t.strftime("%H:%M") for t in sorted(times_needed)],
        "duration_minutes": duration_minutes,
        "participants": participants,
        "total_price": str(total_price) if total_price is not None else None,
        "booking_id": booking.id,
        "extra_quarter_captured": bool(extra_appt),
    }
    return redirect("home")


@require_http_methods(["GET", "POST"])
def site_reviews(request):
    print("DEBUG: site_reviews called with method:", request.method)  # הדפסה לוגית לבדיקה
    focus_rating_error = False  # <- דגל לגלילה

    if request.method == "POST":
        form = SiteReviewForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request,  "תודה רבה על שיתוף הפעולה." , extra_tags="review_saved")
            return redirect('site_reviews')
        else:
            # אם השגיאה היא על rating — לא מציגים פופאפ כלל, רק נגלול לטופס
            if 'rating' in form.errors:
                focus_rating_error = True
            else:
                # לשגיאות אחרות מותר להציג פופאפ (אם תרצי אפשר גם לוותר)
                messages.error(request, "יש בעיה בפרטים. נסה שוב." , extra_tags="review_error")
    else:
        form = SiteReviewForm()

    qs = SiteReview.objects.order_by('-created_at')
    paginator = Paginator(qs, 10)
    page_obj = paginator.get_page(request.GET.get('page'))

    agg = qs.aggregate(avg=Avg('rating'))
    return render(request, "homePage/site_reviews.html", {
        "reviews": page_obj.object_list,
        "page_obj": page_obj,
        "rating_avg": agg['avg'] or 0,
        "rating_count": qs.count(),
        "form": form,
        "focus_rating_error": focus_rating_error,  # <- לדף
    })

def _client_ip(request):
    print("DEBUG: _client_ip called")  # הדפסה לוגית לבדיקה
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")

def _find_booking_by_payment_ref(payment_ref: str):
    print("DEBUG: _find_booking_by_payment_ref called with payment_ref:", payment_ref)  # הדפסה לוגית לבדיקה
    ref = (payment_ref or "").strip()
    if not ref:
        return None
    # חיפוש לא-תלוי-רישיות בשדה payment_ref
    return Booking.objects.filter(payment_ref__iexact=ref).order_by("-id").first()

@require_http_methods(["GET", "POST"])
def cancel_request_view(request):
    print("DEBUG: cancel_request_view called with method:", request.method)  # הדפסה לוגית לבדיקה
    initial = {}

    # פרה־פייל לפי booking_id (כמו שהיה)
    booking_id = request.GET.get("booking_id")
    if booking_id and booking_id.isdigit():
        b = Booking.objects.filter(pk=int(booking_id)).first()
        if b:
            initial["booking"] = b
            appt = getattr(b, "appointment", None)
            if appt:
                initial["appointment"] = appt
                try:
                    if hasattr(appt, "date") and hasattr(appt, "time") and appt.date and appt.time:
                        initial["start_dt"] = datetime.combine(appt.date, appt.time)
                    elif hasattr(appt, "start_dt") and appt.start_dt:
                        initial["start_dt"] = appt.start_dt
                except Exception:
                    pass

    if request.method == "POST":
        form = CancelRequestForm(request.POST, initial=initial)
        if form.is_valid():
            obj: CancellationRequest = form.save(commit=False)
            obj.channel = "web"
            obj.ip_address = _client_ip(request)
            obj.user_agent = request.META.get("HTTP_USER_AGENT", "")[:255]

            # השמה אוטומטית של Booking אם הוזן payment_ref
            if not obj.booking and obj.order_id:
                b = _find_booking_by_payment_ref(obj.order_id)
                if b:
                    obj.booking = b

            # שאיבת התור מתוך ה-Booking אם יש
            if not obj.appointment and obj.booking and hasattr(obj.booking, "appointment"):
                obj.appointment = obj.booking.appointment

            # חישוב start_dt אם חסר
            if (not obj.start_dt) and obj.appointment:
                try:
                    if hasattr(obj.appointment, "date") and hasattr(obj.appointment, "time"):
                        obj.start_dt = datetime.combine(obj.appointment.date, obj.appointment.time)
                    elif hasattr(obj.appointment, "start_dt") and obj.appointment.start_dt:
                        obj.start_dt = obj.appointment.start_dt
                except Exception:
                    pass

            obj.save()

            # ❌ אין שליחת מיילים כאן.
            messages.success(request, "קיבלנו את בקשת הביטול שלך ונחזור אליך בהקדם.")
            return redirect("home")
    else:
        form = CancelRequestForm(initial=initial)

    return render(request, "homePage/cancel_request.html", {"form": form})

def _has_consent_by_phone(phone: str) -> bool:
    print("DEBUG: _has_consent_by_phone called with phone:", phone)  # הדפסה לוגית לבדיקה
    sid = normalize_phone_il(phone)
    if not sid:
        return False
    tv = getattr(settings, "TERMS_VERSION", "1.0")
    pv = getattr(settings, "PRIVACY_VERSION", "1.0")
    return (
        TermsConsent.objects.filter(policy="terms",   version=tv, subject_id=sid).exists() and
        TermsConsent.objects.filter(policy="privacy", version=pv, subject_id=sid).exists()
    )

def _save_consent_by_phone(request, phone: str, full_name: str = ""):
    print("DEBUG: _save_consent_by_phone called with phone:", phone, "full_name:", full_name)  # הדפסה לוגית לבדיקה
    sid = normalize_phone_il(phone)
    if not sid:
        return
    ip = request.META.get("REMOTE_ADDR")
    ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]
    tv = getattr(settings, "TERMS_VERSION", "1.0")
    pv = getattr(settings, "PRIVACY_VERSION", "1.0")

    TermsConsent.objects.get_or_create(
        policy="terms", version=tv, subject_id=sid,
        defaults={"full_name": full_name, "ip": ip, "user_agent": ua}
    )
    TermsConsent.objects.get_or_create(
        policy="privacy", version=pv, subject_id=sid,
        defaults={"full_name": full_name, "ip": ip, "user_agent": ua}
    )

@require_GET
def consent_status(request):
    """API: מחזיר אם צריך צ'קבוקס בהתאם לטלפון ולגרסאות הנוכחיות (בלי קוקיז)"""
    print("DEBUG: consent_status called with method:", request.method)  # הדפסה לוגית לבדיקה
    phone = request.GET.get("phone") or ""
    needs = not _has_consent_by_phone(phone)
    return JsonResponse({
        "needs_consent": needs,
        "versions": {"terms": settings.TERMS_VERSION, "privacy": settings.PRIVACY_VERSION}
    })

@transaction.atomic
def booking_form(request):
    print("DEBUG: booking_form called with method:", request.method)  # הדפסה לוגית לבדיקה

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

# ——— עזר ———
def _abs(request, name, **kwargs):
    print("DEBUG: _abs called with name:", name, "kwargs:", kwargs)  # הדפסה לוגית לבדיקה
    return request.build_absolute_uri(reverse(name, kwargs=kwargs if kwargs else None))

# ——— 1) התחלת תשלום ———
def pay_start(request):
    print("DEBUG: pay_start called with method:", request.method)  # הדפסה לוגית לבדיקה

    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    appointment_id   = request.POST.get("appointment_id")
    activity_id      = request.POST.get("activity_id")
    duration_minutes = int(request.POST.get("duration_minutes", "60"))
    participants     = int(request.POST.get("participants", "1"))
    first_name       = request.POST.get("first_name","")
    last_name        = request.POST.get("last_name","")
    phone            = request.POST.get("phone","")
    email            = request.POST.get("email","")

    # ✅ NEW: אכיפת/שמירת הסכמה לפני המשך לתשלום
    full_name = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
    sid = normalize_phone_il(phone)
    if not _has_consent_by_phone(sid):
        if not request.POST.get("accept_terms"):
            # מחזירים לדף המילוי עם הודעת שגיאה + פרמטרים לשחזור הטופס
            qs = urlencode({
                "appointment_id": appointment_id or "",
                "activity_id": activity_id or "",
                "duration_minutes": duration_minutes or "",
                "participants": participants or "",
                "activity_type": request.POST.get("activity_type") or "",
                "wine": request.POST.get("wine") or "",
                "consent_error": "1",
            })
            return redirect(f"{reverse('booking_form')}?{qs}")
        else:
            # נשמור את ההסכמה כי המשתמש סימן עכשיו
            _save_consent_by_phone(request, phone=sid, full_name=full_name)

    activity_id = request.POST.get("activity_id")
    activity = get_object_or_404(Activity, id=activity_id)

    # שימוש במחיר יחידה שמגיע מהטופס (אם אין – נופלים למחיר הפעילות)
    full_amount = float(request.POST.get("unit_price"))
    if activity.name != "טיול כרכרה":
        full_amount =  full_amount * int(participants)
    amount_agorot = int((Decimal(str(full_amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    # אם המשתמש סימן וי – נשמור רשומת Marketing בסיסית
    if request.POST.get("marketing_optin"):
        MarketingConsent.objects.get_or_create(
            subject_id=sid,  # הטלפון המנורמל שלך (0XXXXXXXXX)
            defaults={
                "version": getattr(settings, "MARKETING_VERSION", "1.0"),
                "full_name": full_name,
                "customer_email": (email or "").strip(),
                "ip": request.META.get("REMOTE_ADDR"),
                "user_agent": request.META.get("HTTP_USER_AGENT", ""),
            }
        )


    # --- שמירת/עדכון שיווק מינימלית ---
    if request.POST.get("marketing_optin"):
        email_norm = (email or "").strip().lower()
        obj, created = MarketingConsent.objects.get_or_create(
            subject_id=sid,
            defaults={
                "version": getattr(settings, "MARKETING_VERSION", "1.0"),
                "full_name": full_name,
                "customer_email": email_norm,
                "ip": request.META.get("REMOTE_ADDR"),
                "user_agent": (request.META.get("HTTP_USER_AGENT", "") or "")[:255],
            }
        )
        if not created and email_norm and not (obj.customer_email or "").strip():
            obj.customer_email = email_norm
            obj.save(update_fields=["customer_email"])

    payment = Payment.objects.create(
        provider="mock",
        amount_agorot=amount_agorot,
        currency="ILS",
        status="pending",
        appointment_id=appointment_id,
        activity_id=activity_id,
        duration_minutes=duration_minutes,
        participants=participants,
        customer_name=full_name,
        phone=phone.strip(),
        email=email.strip(),
    )
    request.session['last_payment_id'] = payment.id
    return redirect(reverse("mock_checkout", kwargs={"payment_id": payment.id}))
# ——— 2) חזרה מהסליקה — מחליטים על הודעה, ומפנים לדף הבית ———

def pay_return(request):
    print("DEBUG: pay_return called with method:", request.method)  # הדפסה לוגית לבדיקה

    # נשלוף וננקה את ה-id מהסשן כדי לא להציג שוב הודעות בשגגה
    pid = request.GET.get("payment_id") or request.session.pop("last_payment_id", None)
    if not pid:
        messages.error(request, "לא נמצא תשלום תואם.", extra_tags="payment_failed")
        return redirect('home')

    # נחכה בשקט עד 5 שניות שה-webhook יסיים (בלי להראות "מעבדים...")
    deadline = time_mod.time() + 5.0
    payment = None
    finals = ('succeeded', 'failed', 'canceled', 'refunded')

    while time_mod.time() < deadline:
        payment = Payment.objects.filter(id=pid).only('status', 'charge_id').first()
        if payment and payment.status in finals:
            break
        time_mod.sleep(0.3)

    # בדיקה אחרונה והודעה אחת בלבד: הצלחה או שגיאה
    payment = Payment.objects.filter(id=pid).only('status', 'charge_id').first()
    if payment and payment.status == 'succeeded':
        messages.success(
            request,
            "הקבלה והפרטים על התור יישלחו במייל ובהודעת SMS בדקות הקרובות.",
            extra_tags="payment_succeeded",
        )
    else:
        ref = (payment.charge_id if payment else None) or "—"
        messages.error(
            request,
            f"התשלום לא הושלם. ניתן לנסות שוב או ליצור קשר.",
            extra_tags="payment_failed",
        )

    return redirect('home')

def _pick_booking_status(booking_model, *candidates):
    """
    מחזיר סטטוס ראשון מתוך candidates שקיים בבחירות של המודל (אם יש choices),
    אחרת מחזיר את הראשון.
    """
    print("DEBUG: _pick_booking_status called with candidates:", candidates)  # הדפסה לוגית לבדיקה
    try:
        choices = {c[0] for c in booking_model._meta.get_field("status").choices or []}
    except Exception:
        choices = set()
    for c in candidates:
        if not choices or c in choices:
            return c
    return candidates[0]

def _append_qs(url, **params):
    print("DEBUG: _append_qs called with url:", url, "params:", params)  # הדפסה לוגית לבדיקה
    s, n, p, q, f = urlsplit(url)
    data = dict(parse_qsl(q))
    data.update({k: v for k, v in params.items() if v is not None})
    return urlunsplit((s, n, p, urlencode(data), f))

# ——— 3) Webhook — כאן קובעים סטטוס סופי, יוצרים Booking, ושולחים מייל+SMS ———
@csrf_exempt
def pay_webhook(request):
    """
    Webhook למוק:
    מקבל POST עם: payment_id, outcome=success|fail|cancel, [next=<url לחזרה>]
    מעדכן את ה-Payment לסטטוס סופי, מפעיל לוגיקה לאחר הצלחה,
    ובמוק מחזיר redirect ל-`next` (אם קיים) במקום JSON.
    """
    print("DEBUG: pay_webhook called with method:", request.method)  # הדפסה לוגית לבדיקה
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    try:
        pid = int(request.POST.get("payment_id"))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("missing payment_id")

    outcome = (request.POST.get("outcome") or "").lower()
    status_map = {"success": "succeeded", "fail": "failed", "cancel": "canceled"}
    new_status = status_map.get(outcome, "failed")

    payment = get_object_or_404(Payment, id=pid)
    FINALS = ("succeeded", "failed", "canceled", "refunded")

    # אידמפוטנטיות: אם כבר הצליח – לא משנים סטטוס. רק משלימים מזהה אם צריך.
    if payment.status == "succeeded":
        if not payment.charge_id and getattr(payment, "booking_id", None):
            # נעדיף את ה-ref מההזמנה אם קיים; אחרת מספר ההזמנה
            payment.charge_id = (getattr(payment.booking, "payment_ref", None) or str(payment.booking_id))
            payment.save(update_fields=["charge_id"])
        # המשך ל-redirect/JSON בסוף הפונקציה
    elif payment.status not in FINALS:
        if new_status == "succeeded":
            # 1) יצירת/איתור Booking
            booking = _finalize_booking_after_payment(payment)

            # 2) הקצאת מזהה עסקה ייחודי בפורמט MZ-XXXXXXXX לשני הצדדים (Booking & Payment)
            ref = assign_unique_ref(booking, payment, digits=8)

            # 3) סימון סטטוס וסימון זמן קליטת ה-webhook
            payment.status = "succeeded"
            payment.webhook_received_at = timezone.now()
            # charge_id עודכן ע"י assign_unique_ref אם היה חסר; נשמור את שלושתם
            payment.save(update_fields=["status", "webhook_received_at", "charge_id"])

            # 4) עדכון סטטוס ההזמנה
            new_bstatus = _pick_booking_status(Booking, "confirmed", "paid", "succeeded")
            if getattr(booking, "status", None) != new_bstatus:
                try:
                    booking.status = new_bstatus
                    booking.save(update_fields=["status"])
                except Exception:
                    booking.save()

            # 5) התראות ללקוח — קודם מייל, ואם הצליח אז SMS

            email_ok = False
            try:
                email_ok = send_booking_email(payment, booking)
            except Exception:
                email_ok = False

            # ואז SMS עם כל הפרטים:
            if getattr(settings, "SEND_SMS", False):
                sms_text = format_booking_sms(payment, booking)

                sent = False
                try:
                    from homePage.services.ntfy_gateway import send_sms_via_ntfy
                    sent = send_sms_via_ntfy(payment.phone, sms_text)
                except Exception:
                    sent = False



        else:
            # fail / cancel / refunded — סוגרים את התשלום בלי ליצור הזמנה
            payment.status = new_status
            payment.webhook_received_at = timezone.now()
            payment.save(update_fields=["status", "webhook_received_at"])
    else:
        # כאן payment.status כבר באחד הסופיים (failed/canceled/refunded).
        # לא משנים אותו כדי לשמור על אידמפוטנטיות.
        pass

    # Redirect אם הגיע next; אחרת JSON
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url:
        if "payment_id=" not in next_url:
            next_url = _append_qs(next_url, payment_id=payment.id)
        return redirect(next_url)
    return JsonResponse({"ok": True, "status": payment.status, "charge_id": payment.charge_id})

def _resolve_activity_for_booking(payment: Payment, appt: Appointment | None):
    print("DEBUG: _resolve_activity_for_booking called with payment_id:", payment.id, "appt_id:", getattr(appt, 'id', None))  # הדפסה לוגית לבדיקה
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

def _finalize_booking_after_payment(payment: Payment):
    """
    סוגרת תור, מוצאת/יוצרת Booking, מקשרת אותו ל-Payment, ותופסת את כל הסלוטים לפי המשך.
    אידמפוטנטית ככל האפשר.
    """
    print("DEBUG: _finalize_booking_after_payment called with payment_id:", payment.id)  # הדפסה לוגית לבדיקה
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
            else:
                raise ValueError("Payment בלי appointment_id: אי אפשר לייצר Booking בלי זמנים.")

            minutes = (getattr(payment, "duration_minutes", None)
                       or getattr(appt, "duration_minutes", None)
                       or getattr(activity, "duration_minutes", None)
                       or 60)
            end_dt = start_dt + timedelta(minutes=minutes)

            total_price = (Decimal(payment.amount_agorot) / Decimal("100")) if getattr(payment, "amount_agorot", None) is not None else None

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

# ——— 4) דפי Mock — בדיקות ———
def mock_checkout(request, payment_id: int):
    print("DEBUG: mock_checkout called with payment_id:", payment_id)  # הדפסה לוגית לבדיקה
    payment = get_object_or_404(Payment, id=payment_id)
    amount_nis = payment.amount_agorot / 100.0
    return render(request, "homePage/mock_checkout.html",
                  {"payment": payment, "amount_nis": amount_nis})

def _is_ajax(request):
    print("DEBUG: _is_ajax called with headers:", request.headers)  # הדפסה לוגית לבדיקה
    return request.headers.get("x-requested-with") == "XMLHttpRequest"

@require_POST
@transaction.atomic
def hold_appointment(request):
    print("DEBUG: hold_appointment called at", timezone.now())    # הדפסה לוגית לבדיקה
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

@require_POST
@transaction.atomic
def release_hold(request):
    print("DEBUG: release_hold called at", timezone.now())  # הדפסה לוגית לבדיקה
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

@require_GET
def appointments_snapshot(request):
    print("DEBUG: appointments_snapshot called with method:", request.method)  # הדפסה לוגית לבדיקה
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
    print("DEBUG: renew_hold called at", timezone.now())  # הדפסה לוגית לבדיקה
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