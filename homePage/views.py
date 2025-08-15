from django.http import Http404
from django.shortcuts import render, get_object_or_404
from homePage.models import Appointment, Activity
from django.views.decorators.csrf import csrf_exempt  # אם צריך לפתח בלי CSRF זמנית
from django.shortcuts import redirect
from collections import defaultdict
from datetime import datetime, timedelta, date

def home(request):
    return render(request, 'homePage/home.html')

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

def available_appointment_view(request, activity_id):
    activity = get_object_or_404(Activity, id=activity_id)

    # --- סינון טווח תאריכים (כמו אצלך) ---
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

    # --- שליפת כל המשכים של פעילויות עם אותו שם ---
    durations = (
        Activity.objects
        .filter(name=activity.name)
        .values_list('duration_minutes', flat=True)
        .distinct()
    )
    # אם אין כפילויות, זה יחזיר רק משך אחד (כמו קודם)

    # --- שליפת התורים הפנויים (בריכה משותפת) ---
    all_appointments = (
        Appointment.objects
        .filter(is_booked=False, is_break=False, date__range=(start_day, end_day))
        .order_by('date', 'time')
    )

    # --- קיבוץ לפי כל אחד מהמשכים שמצאנו ---
    grouped_appointments = {d: [] for d in durations}

    for appt in all_appointments:
        start_dt = datetime.combine(appt.date, appt.time)
        for d in durations:
            end_dt = start_dt + timedelta(minutes=d)
            # לא נוגעים באובייקט המקורי (כדי לא לדרוס end_time בין משכים שונים)
            grouped_appointments[d].append({
                "id": appt.id,
                "date": appt.date,
                "time": appt.time,          # אובייקט time
                "end_time": end_dt.time(),  # אובייקט time

                # אפשר להוסיף שדות נוספים לתבנית לפי הצורך:
                # "participants_count": appt.participants_count,
            })

    context = {
        "activity": activity,
        "durations": sorted(durations),                 # שימושי לניווט/כותרות בטמפלט
        "grouped_appointments": grouped_appointments,   # {משך: [סלוטים...]}
        "selected_date": selected_date,
    }
    return render(request, "homePage/available_appointment.html", context)


@csrf_exempt  # להסיר בייצור אם יש {% csrf_token %}
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
