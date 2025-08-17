from django.http import Http404
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import redirect
from django.utils import timezone
from django.db.models import Q
from django.shortcuts import render, get_object_or_404
from datetime import datetime, timedelta, time, date
from homePage.models import ActivityRule, BusinessHours, Season, Appointment, Activity


def home(request):
    hours_rows = build_business_hours_rows()
    return render(request, "homePage/home.html", {"hours_rows": hours_rows})

def riding_lessons_view(request):
    return render(request, 'homePage/riding_lessons.html')

def night_riding_view(request):
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

def a_vailable_appointment_view(request, activity_id):
    activity = get_object_or_404(Activity, id=activity_id)

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

    # --- כל המשכים של הפעילות הנוכחית (לשם tabs וכו') ---
    durations = (
        Activity.objects
        .filter(name=activity.name)
        .values_list('duration_minutes', flat=True)
        .distinct()
    )

    base_qs = Appointment.objects.filter(
        is_booked=False,
        is_break=False,
        date__range=(start_day, end_day)
    )

    # === לוגיקה של שיוך ===
    if activity.name in ["רכיבת לילה", "רכיבה בזריחה"]:
        # רק תורים שמשויכים לפעילות הזו
        all_appointments = base_qs.filter(activities=activity)
    else:
        # תורים משותפים או משויכים לפעילות
        all_appointments = base_qs.filter(
            Q(activities__isnull=True) | Q(activities=activity)
        )

    all_appointments = all_appointments.order_by('date', 'time').distinct()

    # --- קיבוץ לפי משכים ---
    grouped_appointments = {d: [] for d in durations}

    for appt in all_appointments:
        start_dt = datetime.combine(appt.date, appt.time)

        # חלון קשיח לפעילויות לילה/זריחה (אחרת None)
        win_start, win_end = _strict_window(activity, appt.date)

        for d in durations:
            end_dt = start_dt + timedelta(minutes=d)

            # אם יש חלון קשיח: נציג רק סלוטים שההתחלה ≥ התחלת החלון והסיום ≤ סוף החלון
            if win_start and not (start_dt >= win_start and end_dt <= win_end):
                continue

            grouped_appointments[d].append({
                "id": appt.id,
                "date": appt.date,
                "time": appt.time,
                "end_time": end_dt.time(),
            })

    context = {
        "activity": activity,
        "durations": sorted(durations),
        "grouped_appointments": grouped_appointments,
        "selected_date": selected_date,
    }
    return render(request, "homePage/available_appointment.html", context)


@csrf_exempt
def confirm_booking(request):

    if request.method == 'POST':
        appointment_id = request.POST.get('appointment_id')
        activity_id = request.POST.get('activity_id')
        name = request.POST.get('name')
        phone = request.POST.get('phone')
        participants_str = request.POST.get('participants')

        if not all([appointment_id, activity_id, participants_str]):
            return render(request, 'homePage/unbooking.html', {'message': 'נתונים חסרים'})

        try:
            participants = int(participants_str)
        except ValueError:
            return render(request, 'homePage/unbooking.html', {'message': 'מספר משתתפים לא תקין'})

        appointment = get_object_or_404(Appointment, id=appointment_id)
        activity = get_object_or_404(Activity, id=activity_id)

        if appointment.is_booked:
            return render(request, 'homePage/unbooking.html', {'message': 'התור כבר תפוס'})

        if not (activity.min_participants <= participants <= activity.max_participants):
            return render(request, 'homePage/unbooking.html', {
                'message': f'מספר משתתפים צריך להיות בין {activity.min_participants} ל־{activity.max_participants}'
            })

        # עדכון התור
        appointment.is_booked = True
        appointment.participants_count = participants
        appointment.save()

        # אפשר לשמור גם את השם והטלפון אם תוסיף שדות מתאימים בטבלה
        return redirect('home')

    return render(request, 'homePage/unbooking.html', {'message': 'שגיאה בבקשה'})

def booking_form(request):
    appointment_id = request.GET.get('appointment_id')
    activity_id = request.GET.get('activity_id')

    appointment = get_object_or_404(Appointment, id=appointment_id)
    activity = get_object_or_404(Activity, id=activity_id)

    return render(request, 'homePage/booking.html', {
        'appointment': appointment,
        'activity': activity,
    })



###################



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

def available_appointment_view(request, activity_id):
    activity = get_object_or_404(Activity, id=activity_id)

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

    # --- משכים (לטאבים) לפי שם הפעילות (או נופל למשך הפעילות עצמה) ---
    durations = (
        Activity.objects
        .filter(name=activity.name)
        .values_list('duration_minutes', flat=True)
        .distinct()
    )
    durations = list(durations) or [getattr(activity, "duration_minutes", 60)]

    # --- שליפת תורים פנויים בתקופה ---
    base_qs = Appointment.objects.filter(
        is_booked=False,
        is_break=False,
        date__range=(start_day, end_day)
    )

    grouped_appointments = {d: [] for d in durations}

    # נרוץ על כל התורים – לכל תור נחלץ כללים לפי היום שלו והפעילות
    now_local = timezone.localtime()

    # מסייע לשיוך: לכל תור נחלץ אם צריך "assigned_only" והחלון שלו
    for appt in base_qs.order_by("date", "time"):
        assigned_only, cutoff_minutes, win_start_dt, win_end_dt = get_rules_for(activity, appt.date)

        # סינון לפי שיוך:
        if assigned_only:
            # רק תורים ששויכו לפעילות הנוכחית (M2M)
            # אם אצלך זה FK – החליפי ל: appt.activity_id == activity.id
            if not appt.activities.filter(id=activity.id).exists():
                continue
        else:
            # תורים משותפים או משויכים
            # אם יש M2M activities, "משותף" = ללא שיוך (count=0)
            if appt.activities.exists() and not appt.activities.filter(id=activity.id).exists():
                continue

        # חיתוך לפי "כמה זמן מראש" אם הוגדר (למשל 60 דק')
        if cutoff_minutes:
            cutoff_dt = (now_local + timedelta(minutes=cutoff_minutes)).replace(tzinfo=None)
            start_dt = datetime.combine(appt.date, appt.time)
            if start_dt <= cutoff_dt:
                continue
        else:
            start_dt = datetime.combine(appt.date, appt.time)

        # חיתוך לפי חלון (אם הוגדר)
        # מציגים רק אם start>=win_start ו-end<=win_end
        for d in durations:
            end_dt = start_dt + timedelta(minutes=d)

            if win_start_dt and start_dt < win_start_dt:
                continue
            if win_end_dt and end_dt > win_end_dt:
                continue

            grouped_appointments[d].append({
                "id": appt.id,
                "date": appt.date,
                "time": appt.time,
                "end_time": end_dt.time(),
            })

    context = {
        "activity": activity,
        "durations": sorted(durations),
        "grouped_appointments": grouped_appointments,
        "selected_date": selected_date,
    }
    return render(request, "homePage/available_appointment.html", context)


from datetime import datetime, time
from django.utils import timezone
from homePage.models import BusinessHours, Season

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
