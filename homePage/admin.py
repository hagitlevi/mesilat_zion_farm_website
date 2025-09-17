from django.contrib import admin
import json
from django import forms
from .models import Activity, Appointment, CustomSchedule, Booking, SiteReview,CancellationRequest, TermsConsent, ScheduleBoard
from django.contrib import messages
from django.shortcuts import redirect
from zoneinfo import ZoneInfo
from django.db import transaction
import secrets
from .models import Weekday, BusinessHours, ActivityRule
from types import SimpleNamespace
from django.db.models import Q
from decimal import Decimal
from django.contrib.admin.helpers import ActionForm
from urllib.parse import urlencode
from .views import get_rules_for
from django.http import JsonResponse
from django.conf import settings
from homePage.views import _has_consent_by_phone as has_consent_by_phone, _normalize_phone_il as normalize_phone_il, _format_booking_sms, _send_booking_email
from datetime import datetime, date as ddate, time as dtime, timedelta
from django.template.response import TemplateResponse
from django.urls import path,reverse
from django.utils import timezone
from django.contrib.admin.actions import delete_selected
delete_selected.short_description = "מחיקה"


def _normalize_il_phone(p: str) -> str:
    d = ''.join(ch for ch in (p or '') if ch.isdigit())
    if d.startswith('972'):
        d = '0' + d[3:]
    return d

def _base_id_map_for_names():
    out = {}
    for n in distinct_activity_names():
        base_id = (Activity.objects
                   .filter(name=n)
                   .order_by('duration_minutes', 'id')
                   .values_list('id', flat=True)
                   .first())
        if base_id:
            out[n] = base_id
    return out

def _gen_unique_mz_ref(digits=8) -> str:
    for _ in range(25):
        cand = "MZ-" + "".join(secrets.choice("0123456789") for _ in range(digits))
        if not Booking.objects.filter(payment_ref=cand).exists():
            return cand
    # פולבאק נדיר
    from django.utils import timezone
    return "MZ-" + timezone.now().strftime("%y%m%d%H%M%S")

# ======== עוזרים פנימיים לגריד ========

def _day_range(base: ddate):
    """7 ימים החל מ-base (כולל)."""
    return [base + timedelta(days=i) for i in range(7)]


def _timeslots(start_h=7, end_h=22, step_min=15):
    """
    רשימת שעות 'HH:MM' כל 15 דק' בין start_h ל-end_h.
    אם end_h הוא 24, הוא מייצג חצות (כולל את 23:xx).
    """

    # אם שעת הסיום היא 24, השתמש ב-23 לצורך הלולאה
    if end_h == 24:
        end_h_loop = 23
    else:
        end_h_loop = end_h

    # יצירת אובייקטי הזמן
    t = dtime(hour=start_h, minute=0)
    end = dtime(hour=end_h_loop, minute=0)
    out = []

    # הלולאה ליצירת זמנים
    while t < end:
        out.append(t.strftime("%H:%M"))
        dt = datetime.combine(timezone.localdate(), t) + timedelta(minutes=step_min)
        t = dt.time()

    # הוספת שעת הסיום הסופית אם היא 24
    if end_h == 24:
        out.append("23:00")  # הוספת השעה 23
        out.append("23:15")  # והדקות הבאות שלה
        out.append("23:30")
        out.append("23:45")

    return out

def _fetch_appointments(d0: ddate, d1: ddate):
    """כל הסלוטים (תפוסים/הפסקות) בטווח התאריכים, ממופים למילון לפי (date,time)."""
    qs = Appointment.objects.filter(
        date__gte=d0, date__lte=d1,
    ).select_related("booking", "activity").order_by("date", "time")

    by_key = {}   # (date, "HH:MM") -> Appointment
    for a in qs:
        by_key[(a.date, a.time.strftime("%H:%M"))] = a
    return by_key

def calc_hours(duration):
    if duration == 60:
        return "שעה"
    if duration == 90:
        return "שעה חצי"
    if duration == 120:
        return "שעתיים"
    if duration == 150:
        return "שעתיים וחצי"
    if duration == 180:
        return "שלוש שעות"

def _build_grid(days, timeslots, appt_map):
    """
    יוצר גריד לתצוגה:
    - rows: [{'time': '07:00', 'cells': [cell0.cell6]}, ...]
    - cell = {
        type:'free'|'event'|'skip',
        rowspan:int,
        title:str,
        meta:str,
        status:'paid'|'pending'|'break',
        url:str      # ← קישור להזמנה/סלוט
      }
    """
    # הכנה ריקה
    rows = []
    for ts in timeslots:
        rows.append({'time': ts, 'cells': [{'type': 'free'} for _ in range(len(days))]})

    # עזר למציאת אינדקס שורה לפי שעה (כרגע לא בשימוש, נשאיר למקרה הצורך)
    row_index = {ts: idx for idx, ts in enumerate(timeslots)}

    # נרוץ על כל יום וכל שעה, ובכל פעם שנמצא סלוט תפוס/הפסקה נבלע את הרצף למטה
    for c, day in enumerate(days):
        r = 0
        while r < len(timeslots):
            key = (day, timeslots[r])
            appt = appt_map.get(key)

            if not appt:
                # חופשי
                r += 1
                continue

            # קובעים אם זה הפסקה / שולם / ממתין בצורה חסינה
            is_break = bool(getattr(appt, "is_break", False))

            paid = False
            # 1) אם לשורה עצמה יש is_paid=True
            if getattr(appt, "is_paid", None) is True:
                paid = True
            else:
                # 2) בדיקה דרך ה-Booking המקושר
                b = getattr(appt, "booking", None)
                if b:
                    # אם יש שדה is_paid על ההזמנה
                    if getattr(b, "is_paid", None) is True:
                        paid = True
                    else:
                        # או לפי סטטוס טקסטואלי (גם בעברית)
                        status_val = str(getattr(b, "status", "")).strip().lower()
                        if status_val in {"paid", "שולם"}:
                            paid = True

            status = "break" if is_break else ("paid" if paid else "pending")

            # אם זה לא סלוט ראשון ברצף—נשאיר כ'free' כי התא מעליו יקבל rowspan ויכסה
            # בודקים האם יש סלוט קודם שנראה אותו booking/הפסקה
            if r > 0:
                prev_key = (day, timeslots[r-1])
                prev = appt_map.get(prev_key)
                if prev and (prev.is_break == appt.is_break) and (prev.booking_id == appt.booking_id):
                    # לא מתחילים אירוע חדש; נסומן כ-skip (לא נצייר תא)
                    rows[r]['cells'][c] = {'type': 'skip'}
                    r += 1
                    continue

            # מודדים את האורך מטה (כמה 15-דק רצופים לאותו booking/הפסקה)
            span = 1
            while r + span < len(timeslots):
                nxt_key = (day, timeslots[r + span])
                nxt = appt_map.get(nxt_key)
                if not nxt:
                    break
                if not (nxt.is_break == appt.is_break and nxt.booking_id == appt.booking_id):
                    break
                span += 1

            SLOT_MIN = 15
            duration_min = span * SLOT_MIN

            # כותרת/מידע להצגה
            title = "הפסקה" if is_break else (appt.activity.name if appt.activity else "")
            meta = ""
            if not is_break and appt.booking_id:
                b = appt.booking
                if b:
                    if duration_min <= 45:
                        meta = f"{b.customer_name or ''} • {b.participants or 1} משתתפים • {duration_min} דק׳ "
                    else:
                        hours = calc_hours(duration_min)
                        meta = f"{b.customer_name or ''} • {b.participants or 1} משתתפים • {hours}"

                else:
                    meta = appt.payment_reference or ""

            # === חישוב כתובת היעד ללחיצה ===
            if is_break:
                url = None
            elif appt.booking_id:
                url = reverse("admin:homePage_booking_change", args=[appt.booking_id])
            else:
                url = reverse("admin:homePage_appointment_change", args=[appt.id])

            rows[r]['cells'][c] = {
                'type': 'event',
                'rowspan': span,
                'title': title,
                'meta': meta,
                'status': status,
                'url': url,   # ← חדש
            }

            # את הסלוטים שמתחת לאותו אירוע נסמן כ-skip
            for k in range(1, span):
                rows[r + k]['cells'][c] = {'type': 'skip'}

            r += span

    return rows
# ======================================

class CreateBookingActionForm(ActionForm):
    duration_minutes = forms.ChoiceField(
        label="משך (דקות)",
        choices=[(30,'30'), (45,'45'), (60,'60'), (90,'90'), (120,'120')],
        initial=60
    )
    participants = forms.IntegerField(label="משתתפים", min_value=1, initial=1)
    activity = forms.ModelChoiceField(label="פעילות (אופציונלי)", queryset=Activity.objects.all(), required=False)
    mark_paid = forms.BooleanField(label="לסמן כשולם", required=False, initial=True)
    capture_buffer = forms.BooleanField(label="לתפוס 15 ד׳ הפסקה בסוף (>30)", required=False, initial=True)

@admin.register(ScheduleBoard)
class ScheduleBoardAdmin(admin.ModelAdmin):
    # לא משתמשים ברשימת אובייקטים; מציגים תבנית מותאמת
    change_list_template = "admin/homePage/schedule/change_list.html"

    def has_add_permission(self, request):
        return False


    def changelist_view(self, request, extra_context=None):
        # מצב תצוגה: week / day
        view_mode = request.GET.get("view", "week")

        # תאריך פתיחה (אם לא נשלח – היום)
        try:
            start_str = request.GET.get("start")
            start_day = datetime.fromisoformat(start_str).date() if start_str else timezone.localdate()
        except Exception:
            start_day = timezone.localdate()

        # ימים לתצוגה
        if view_mode == "day":
            days = [start_day]
        else:  # week
            days = _day_range(start_day)

        # סלוטים, פגישות וגריד
        timeslots = _timeslots(5, 24, 15)
        appt_map = _fetch_appointments(days[0], days[-1])
        rows = _build_grid(days, timeslots, appt_map)

        # ניווט קדימה/אחורה לפי מצב
        if view_mode == "day":
            prev_start = (start_day - timedelta(days=1)).isoformat()
            next_start = (start_day + timedelta(days=1)).isoformat()
        else:
            prev_start = (days[0] - timedelta(days=7)).isoformat()
            next_start = (days[0] + timedelta(days=7)).isoformat()

        # פונקציה לשמירה/עדכון פרמטרים ב־URL
        def keep(**kw):
            params = request.GET.copy()
            for k, v in kw.items():
                params[k] = v
            return urlencode(params, doseq=True)

        # תוויות לימים
        week_days = [(d, d.strftime("%d/%m")) for d in days]

        # today (לטאב יום)
        today_iso = timezone.localdate().isoformat()

        ctx = {
            "title": "לוח שנה",
            "rows": rows,
            "week_days": week_days,

            "view_mode": view_mode,
            "qs_day_prev": keep(view="day", start=prev_start),
            "qs_day_next": keep(view="day", start=next_start),
            "qs_day_today": keep(view="day", start=today_iso),
            "qs_week_prev": keep(view="week", start=prev_start),
            "qs_week_next": keep(view="week", start=next_start),

            "start_day": days[0],
            "opts": self.model._meta,
        }
        return TemplateResponse(request, self.change_list_template, ctx)

@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "activity_type", "duration_minutes")
    search_fields = ("name", "description")
    list_filter = ("activity_type", "duration_minutes")
    ordering = ("name",)
    list_display_links = ("id", "name")

@admin.register(CustomSchedule)
class CustomScheduleAdmin(admin.ModelAdmin):
    list_display = ("date", "start_time", "end_time", "is_active")
    list_filter = ("is_active",)
    date_hierarchy = "date"
    ordering = ("-date",)

@admin.register(Weekday)
class WeekdayAdmin(admin.ModelAdmin):
    list_display = ("code", "name")

@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "duration_minutes", "is_booked", "is_break")
    list_filter = ("is_booked", "is_break", "date")
    date_hierarchy = "date"
    ordering = ("-date", "time")
    search_fields = ("customer_name", "customer_phone")
    filter_horizontal = ("activities",)

@admin.register(BusinessHours)
class BusinessHoursAdmin(admin.ModelAdmin):
    list_display = ("season", "start_time", "end_time")
    filter_horizontal = ("days",)   # מציג צ׳קבוקסים / widget מרובה

@admin.register(ActivityRule)
class ActivityRuleAdmin(admin.ModelAdmin):
    list_display = ("activity","season","start_time","end_time","assigned_only","booking_cutoff_minutes")
    filter_horizontal = ("days",)

class AppointmentInline(admin.TabularInline):
    model = Appointment
    extra = 0
    can_delete = False
    fields = ("date", "time", "duration_minutes", "is_break", "is_booked", "is_paid", "payment_reference", "activity")
    readonly_fields = fields

class BookingAdminForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = "__all__"
        labels = {
            "activity": "פעילות",
            "customer_name": "שם לקוח",
            "customer_phone": "טלפון",
            "customer_email": "אימייל",
            "participants": "מספר משתתפים",
            "total_price": "סה\"כ לתשלום",
            "payment_method": "אמצעי תשלום",
            "payment_ref": "אסמכתא/מזהה תשלום",
            "status": "סטטוס",
            "notes": "פרטים/הערות",
            "start_dt": "תאריך ושעת התחלה",
            "end_dt": "תאריך ושעת סיום",
        }

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    actions = [delete_selected]

    form = BookingAdminForm
    list_display  = ("id", "activity", "start_dt", "end_dt",
                     "customer_name", "customer_phone", "status", "payment_ref", "total_price", "participants", "created_at")
    list_filter   = ("activity", "status","created_at", "start_dt")
    search_fields = ("customer_name", "customer_phone", "customer_email", "payment_ref")
    inlines = [AppointmentInline]

    # כפתור "קביעת הזמנה חדשה" בעמוד הרשימה
    change_list_template = "admin/homePage/appointment_change_list.html"




    def has_add_permission(self, request):
        return False

    def get_urls(self):
        urls = super().get_urls()
        extra = [
            # טופס הוויזארד + AJAX לזמני התחלה (אותו URL, עם ?ajax=times)
            path(
                "book/",
                self.admin_site.admin_view(self.book_wizard),
                name="homePage_appointment_book",
            ),
        ]
        return extra + urls

    def book_wizard(self, request):
        """
        GET (ללא ajax): מציג את טופס יצירת ההזמנה הידנית.
        GET (?ajax=times): מחזיר JSON של שעות התחלה פנויות לתאריך/אורך/פעילות.
        POST: יוצר הזמנה, תופס סלוטים, נותן payment_ref ייחודי, מחשב מחיר ושולח מייל.
        """
        # ---- AJAX: זמני התחלה פנויים ----
        # ודאי שבראש הקובץ יש:
        # from datetime import datetime
        # from django.http import JsonResponse

        if request.method == "GET" and request.GET.get("ajax") == "times":
            name = (request.GET.get("name") or "").strip()
            minutes_s = (request.GET.get("minutes") or "").strip()
            date_s = (request.GET.get("date") or "").strip()
            variant = (request.GET.get("variant") or "").strip().lower()  # 'day' | 'night' | 'sunrise' | ''

            # המרות בטוחות
            try:
                minutes = int(minutes_s)
            except ValueError:
                return JsonResponse({"times": []})

            try:
                picked_date = datetime.fromisoformat(date_s).date()
            except Exception:
                return JsonResponse({"times": []})

            if not (name and minutes and picked_date):
                return JsonResponse({"times": []})

            # נעביר variant רק כשזו "רכיבה זוגית" וערך חוקי
            v = variant if (name == "רכיבה זוגית" and variant in ("day", "night", "sunrise")) else None

            times = find_free_start_times(picked_date, minutes, name, variant=v)
            return JsonResponse({"times": times})

        # ---- GET רגיל: תצוגת הוויזארד ----
        if request.method == "GET":
            names = distinct_activity_names()
            durations_map = {n: durations_for_name(n) for n in names}
            ctx = {
                "opts": self.model._meta,
                "media": self.media,
                "app_label": self.model._meta.app_label,
                "activity_names": names,
                "durations_map_json": json.dumps(durations_map, ensure_ascii=False),
                "today": timezone.localdate().isoformat(),
                "ajax_times_url": reverse("admin:homePage_appointment_book"),
            }
            return TemplateResponse(
                request,
                "admin/homePage/appointment_book_wizard.html",
                ctx,
            )

        # ---- POST: יצירה אמיתית של הזמנה ----
        name = (request.POST.get("activity_name") or "").strip()
        minutes = int(request.POST.get("duration_minutes") or 0)
        date_str = (request.POST.get("date") or "").strip()
        start_str = (request.POST.get("start_time") or "").strip()
        participants = max(1, int(request.POST.get("participants") or 1))
        mark_paid = str(request.POST.get("mark_paid", "")).lower() in {"1", "true", "on", "yes"}

        # פרטי הלקוח
        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        email = (request.POST.get("email") or "").strip()

        # ---- הסכמה לתנאים/פרטיות לפי טלפון ----
        # ---- הסכמה לתנאים/פרטיות לפי טלפון ----
        sid = normalize_phone_il(phone)  # 05xxxxxxxx מנורמל
        has_consent = has_consent_by_phone(sid)

        # אם אין הסכמה ואין צ'קבוקס מסומן – עוצרים (התנאי היחיד!)
        if (not has_consent) and (not request.POST.get("accept_terms")):
            messages.error(request, "יש לאשר את תנאי השימוש ומדיניות הפרטיות לפני יצירת הזמנה.")
            return redirect(reverse("admin:homePage_appointment_book"))

        # אם סומן הצ'קבוקס – נשמור את ההסכמה (כדי שלא יבקש שוב)
        if request.POST.get("accept_terms") and sid:
            tv = getattr(settings, "TERMS_VERSION", "1.0")
            pv = getattr(settings, "PRIVACY_VERSION", "1.0")
            TermsConsent.objects.get_or_create(
                policy="terms", version=tv, subject_id=sid,
                defaults={"accepted_at": timezone.now()}
            )
            TermsConsent.objects.get_or_create(
                policy="privacy", version=pv, subject_id=sid,
                defaults={"accepted_at": timezone.now()}
            )



        # שדה מיוחד: יין (רק לרכיבה זוגית)
        wine = (request.POST.get("wine") or "").strip().lower()
        if wine not in ("white", "red", "none", ""):
            wine = ""
        # שדה מיוחד: צבע סוס (רק לצילומים)
        horse_color = (request.POST.get("horse_color") or "").strip().lower()
        if not (name and minutes and date_str and start_str and first_name and last_name and phone and email):
            messages.error(request, "חסרים שדות חובה.")
            return redirect(reverse("admin:homePage_appointment_book"))

        if name == "צילומים" and not horse_color:
            messages.error(request, "בחרי צבע סוס לצילומים.")
            return redirect(reverse("admin:homePage_appointment_book"))

        # מאתרים פעילות לפי שם + אורך
        activity = Activity.objects.filter(name=name, duration_minutes=minutes).order_by("id").first()
        if not activity:
            messages.error(request, "לא נמצאה פעילות מתאימה למשך שנבחר.")
            return redirect(reverse("admin:homePage_appointment_book"))

        # פרסינג של תאריך/שעה
        try:
            d = datetime.fromisoformat(date_str).date()
            t = datetime.strptime(start_str, "%H:%M").time()
        except Exception:
            messages.error(request, "תאריך/שעה לא תקינים.")
            return redirect(reverse("admin:homePage_appointment_book"))

        start_dt = datetime.combine(d, t)
        slot_count = max(1, (minutes + 14) // 15)  # ceil ל־15 דק׳
        times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

        # יצירה ותפיסה אטומית
        with transaction.atomic():
            # שואבים את כל הסלוטים הנדרשים
            appts = list(
                Appointment.objects.select_for_update().filter(
                    date=d, time__in=times_needed, is_booked=False, is_break=False
                ).order_by("time")
            )
            if len(appts) != slot_count:
                messages.error(request, "הסלוטים כבר נתפסו; נסי שעה אחרת.")
                return redirect(reverse("admin:homePage_appointment_book"))

            # טקסט פרטים (יין) כשצריך
            details_txt = ""
            if name == "רכיבה זוגית" and wine:
                details_txt = "יין: " + {"white": "יין לבן", "red": "יין אדום", "none": "בלי יין"}.get(wine, "")
            if name == "צילומים" and horse_color:
                color_map = {"white": "סוס לבן", "brown": "סוס חום"}
                details_txt = (
                                  details_txt + " | " if details_txt else "") + f"צבע סוס: {color_map.get(horse_color, horse_color)}"

            # יצירת ההזמנה
            booking = Booking.objects.create(
                activity=activity,
                customer_name=f"{first_name} {last_name}".strip(),
                customer_phone=phone,
                customer_email=email,
                participants=participants,
                total_price=None,
                payment_method="admin",
                status=("paid" if mark_paid else "pending"),
                start_dt=start_dt,
                end_dt=start_dt + timedelta(minutes=minutes),
                details=details_txt or "נקבע באדמין",
            )

            # מספר עסקה ייחודי בפורמט MZ-XXXXXXXX
            ref = _gen_unique_mz_ref()
            booking.payment_ref = ref
            booking.save(update_fields=["payment_ref"])

            # סימון הסלוטים שתפסנו
            for a in appts:
                a.booking = booking
                a.is_booked = True
                a.is_paid = bool(mark_paid)
                a.is_break = False
                a.payment_reference = ref
                if a.activity_id != activity.id:
                    a.activity = activity
                a.save(update_fields=["booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"])

            # הפסקה של 15 דק׳ אחרי אם המשך > 30 דק׳ (אופציונלי)
            if minutes > 30:
                extra_start = start_dt + timedelta(minutes=15 * slot_count)
                extra = Appointment.objects.select_for_update().filter(
                    date=d, time=extra_start.time(), is_booked=False, is_break=False
                ).first()
                if extra:
                    extra.booking = booking
                    extra.is_booked = True
                    extra.is_paid = False
                    extra.is_break = True
                    extra.payment_reference = ref
                    if extra.activity_id != activity.id:
                        extra.activity = activity
                    extra.save(
                        update_fields=["booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"])

            # חישוב מחיר כמו באתר
            unit_price = (
                             Activity.objects
                             .filter(name=activity.name, duration_minutes=minutes)
                             .exclude(price__isnull=True)
                             .values_list("price", flat=True)
                             .first()
                         ) or activity.price

            total_price = None
            if unit_price is not None:
                if activity.name in ("טיול כרכרה", "רכיבה זוגית"):
                    total_price = unit_price  # מחיר פר הזמנה
                else:
                    total_price = unit_price * max(1, participants)  # מחיר לאדם × משתתפים
                booking.total_price = total_price
                try:
                    booking.save(update_fields=["total_price"])
                except Exception:
                    booking.save()

        # שליחת מייל אישור (כמו באתר)
        try:
            amount_agorot = int(Decimal(total_price or 0) * 100)
            payment_like = SimpleNamespace(
                email=booking.customer_email,
                customer_name=booking.customer_name,
                amount_agorot=amount_agorot,
                charge_id=booking.payment_ref,  # אותו מספר הזמנה
            )
            _send_booking_email(payment_like, booking)
            if getattr(settings, "SEND_SMS", False) and (booking.customer_phone or "").strip():
                sms_text = _format_booking_sms(payment_like, booking)

                sent = False
                try:
                    from homePage.services.ntfy_gateway import send_sms_via_ntfy
                    sent = send_sms_via_ntfy(booking.customer_phone, sms_text)
                except Exception:
                    sent = False


        except Exception:
            pass

        messages.success(request, f"הזמנה #{booking.id} נוצרה בהצלחה (ref: {booking.payment_ref}).")
        return redirect(reverse("admin:homePage_booking_change", args=[booking.id]))

    def book_times(self, request):
        """AJAX: מחזיר שעות התחלה פנויות לפי שם פעילות/אורך/תאריך."""
        name = (request.GET.get("name") or "").strip()
        minutes = int(request.GET.get("minutes") or 0)
        date_str = (request.GET.get("date") or "").strip()

        if not (name and minutes and date_str):
            return JsonResponse({"times": []})

        try:
            chosen_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            return JsonResponse({"times": []})

        times = find_free_start_times(chosen_date, minutes, name)
        return JsonResponse({"times": times})

    def ajax_start_times(self, request):
        name = request.GET.get("name", "")
        minutes = int(request.GET.get("minutes") or 0)
        date_str = request.GET.get("date") or ""
        try:
            chosen_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            return JsonResponse({"times": []})
        return JsonResponse({"times": find_free_start_times(chosen_date, minutes, name)})

    # --- אופציונלי אם ה-template קורא ב-AJAX ---
    def ajax_durations(self, request):
        name = request.GET.get("name", "")
        return JsonResponse({"durations": durations_for_name(name)})


    @admin.action(description="צור הזמנה מרצף החל מהסלוט")
    def action_create_booking_from_slot(self, request, queryset):
        # נדרש לבחור סלוט התחלה אחד בלבד
        if queryset.count() != 1:
            self.message_user(request, "בחרי סלוט התחלה אחד בלבד.", level=messages.ERROR)
            return

        base_slot: Appointment = queryset.first()

        # קריאת פרמטרים מהטופס
        try:
            duration = int(request.POST.get("duration_minutes") or 60)
            participants = int(request.POST.get("participants") or 1)
        except ValueError:
            self.message_user(request, "משך/משתתפים לא תקינים.", level=messages.ERROR)
            return

        activity_id = request.POST.get("activity") or None
        activity = Activity.objects.filter(pk=activity_id).first() if activity_id else None
        mark_paid = bool(request.POST.get("mark_paid"))
        capture_buffer = bool(request.POST.get("capture_buffer"))

        try:
            with transaction.atomic():
                base = Appointment.objects.select_for_update().get(pk=base_slot.pk)
                if base.is_booked or base.is_break:
                    raise ValueError("סלוט ההתחלה תפוס או מסומן כהפסקה")

                if not activity:
                    activity = getattr(base, "activity", None)
                    if not activity and hasattr(base, "activities"):
                        activity = base.activities.first()
                if not activity:
                    raise ValueError("לא נמצאה פעילות מתאימה לסלוט")

                start_dt = datetime.combine(base.date, base.time)
                slot_count = max(1, (duration + 14) // 15)
                times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

                appts = list(Appointment.objects.select_for_update().filter(
                    date=base.date, time__in=times_needed,
                    is_booked=False, is_break=False
                ).order_by("time"))
                if len(appts) != slot_count:
                    raise ValueError("אין רצף סלוטים פנוי לכל המשך שנבחר")

                booking = Booking.objects.create(
                    activity=activity,
                    customer_name=(base.customer_name or "").strip(),
                    customer_phone=(base.customer_phone or "").strip(),
                    customer_email="",
                    participants=max(1, participants),
                    total_price=None,
                    payment_method="admin",
                    status="paid" if mark_paid else "pending",
                    start_dt=start_dt,
                    end_dt=start_dt + timedelta(minutes=duration),
                    details="נקבע באדמין",
                )

                ref = _gen_unique_mz_ref()
                booking.payment_ref = ref
                booking.save(update_fields=["payment_ref"])

                for a in appts:
                    a.booking = booking
                    a.is_booked = True
                    a.is_paid = bool(mark_paid)
                    a.is_break = False
                    a.payment_reference = ref
                    if a.activity_id != activity.id:
                        a.activity = activity
                    a.save(update_fields=["booking","is_booked","is_paid","is_break","payment_reference","activity"])
                    if hasattr(a, "activities"):
                        a.activities.add(activity)

                if capture_buffer and duration > 30:
                    extra_start = start_dt + timedelta(minutes=15 * slot_count)
                    extra = Appointment.objects.select_for_update().filter(
                        date=base.date, time=extra_start.time(),
                        is_booked=False, is_break=False
                    ).first()
                    if extra:
                        extra.booking = booking
                        extra.is_booked = True
                        extra.is_paid = False
                        extra.is_break = True
                        if extra.activity_id != activity.id:
                            extra.activity = activity
                        extra.payment_reference = ref
                        extra.save(update_fields=["booking","is_booked","is_paid","is_break","activity","payment_reference"])

                # מחיר + מייל כמו באתר
                minutes_len = int((booking.end_dt - booking.start_dt).total_seconds() // 60)
                unit_price = (
                    Activity.objects
                    .filter(name=booking.activity.name, duration_minutes=minutes_len)
                    .exclude(price__isnull=True)
                    .values_list('price', flat=True)
                    .first()
                ) or booking.activity.price

                total_price = None
                if unit_price is not None:
                    total_price = (unit_price if booking.activity.name == "טיול כרכרה"
                                   else unit_price * max(1, booking.participants))
                    booking.total_price = total_price
                    try:
                        booking.save(update_fields=["total_price"])
                    except Exception:
                        booking.save()

                amount_agorot = int(Decimal(total_price or 0) * 100)
                payment_like = SimpleNamespace(
                    email=booking.customer_email or "",
                    customer_name=booking.customer_name or "",
                    amount_agorot=amount_agorot,
                    charge_id=booking.payment_ref,
                )
                try:
                    _send_booking_email(payment_like, booking)
                    if getattr(settings, "SEND_SMS", False) and (booking.customer_phone or "").strip():
                        sms_text = _format_booking_sms(payment_like, booking)

                        sent = False
                        try:
                            from homePage.services.ntfy_gateway import send_sms_via_ntfy
                            sent = send_sms_via_ntfy(booking.customer_phone, sms_text)
                        except Exception:
                            sent = False


                except Exception:
                    pass

        except Exception as e:
            self.message_user(request, f"שגיאה: {e}", level=messages.ERROR)
            return

        self.message_user(request, f"הזמנה #{booking.id} נוצרה בהצלחה (ref: {booking.payment_ref}).", level=messages.SUCCESS)
        return redirect(reverse("admin:homePage_booking_change", args=[booking.id]))

@admin.register(SiteReview)
class SiteReviewAdmin(admin.ModelAdmin):
    list_display  = ('name', 'rating', 'created_at')      # הורדנו is_approved/email
    list_filter   = ('rating',)
    search_fields = ('name', 'comment')
    ordering      = ('-created_at',)

@admin.register(CancellationRequest)
class CancellationRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "full_name", "phone", "order_id", "status", "created_at")
    list_filter = ("status", "channel", "created_at")
    search_fields = (
        "full_name", "phone", "email", "order_id",
        "booking__payment_ref",  # חיפוש לפי מספר ההזמנה ב-Booking
        "appointment__id",
    )


    def save_model(self, request, obj, form, change):
        raw = (getattr(obj, "order_id", "") or "").strip()
        if raw and not getattr(obj, "booking_id", None):
            b = Booking.objects.filter(payment_ref__iexact=raw).first()
            if b:
                obj.booking = b
                # אופציונלי: להשלים גם רשומת Appointment ו-start_dt
                appt = getattr(b, "appointment", None)
                if appt and not getattr(obj, "appointment_id", None):
                    obj.appointment = appt
                if (not getattr(obj, "start_dt", None)) and appt:
                    try:
                        obj.start_dt = datetime.combine(appt.date, appt.time)
                    except Exception:
                        if getattr(appt, "start_dt", None):
                            obj.start_dt = appt.start_dt
        super().save_model(request, obj, form, change)



@admin.register(TermsConsent)
class TermsConsentAdmin(admin.ModelAdmin):
    list_display   = ("subject_id", "full_name", "policy", "version", "accepted_at", "ip")
    list_filter    = ("policy", "version", "accepted_at")
    search_fields  = ("subject_id", "full_name", "ip", "user_agent")
    date_hierarchy = "accepted_at"
    ordering       = ("-accepted_at",)
    readonly_fields = ("accepted_at",)

# עוזרים: שמות פעילות ייחודיים, ואורכים מותרים לשם
def distinct_activity_names():
    return sorted(set(Activity.objects.values_list("name", flat=True)))

def durations_for_name(name: str):
    return sorted(set(
        Activity.objects.filter(name=name).values_list("duration_minutes", flat=True)
    ))

def find_free_start_times(chosen_date, minutes, activity_name, variant=None):
    """
    זמינות שעות התחלה ('HH:MM') ליום/אורך נתון.
    תוספת: כשactivity='רכיבה זוגית' משתמשים ב-variant:
      - day     → סלוטים ללא שיוך פעילות
      - night   → סלוטים של 'רכיבת לילה'
      - sunrise → סלוטים של 'רכיבה בזריחה'
    יתר הפעילויות כמו קודם: סלוטים ריקים או משויכים לפעילות עצמה,
    או במקרה של 'רכיבה בזריחה'/'רכיבת לילה' – רק סלוטים משויכים.
    אין חסימת "שעתיים קדימה" ואין כלל 16:00.
    """
    now_aw = timezone.now().astimezone(ZoneInfo("Asia/Jerusalem"))
    today = now_aw.date()
    now_naive = datetime.combine(today, now_aw.time())

    activity = Activity.objects.filter(name=activity_name).first()

    base_qs = (
        Appointment.objects
        .filter(is_booked=False, is_break=False, date=chosen_date)
        .order_by("time")
        .distinct()
    )

    apply_window, apply_cutoff = True, False
    appts_qs = base_qs
    rules_activity = activity

    if activity and activity.name == "רכיבה זוגית":
        v = (variant or "day").lower()
        if v == "day":
            appts_qs = base_qs.filter(activities__isnull=True)
            rules_activity = activity
            apply_cutoff = False
        elif v == "night":
            target_acts = Activity.objects.filter(name="רכיבת לילה")
            appts_qs = base_qs.filter(activities__in=list(target_acts))
            rules_activity = target_acts.first()
            apply_cutoff = True   # כמו בטאב night אצלך
        elif v == "sunrise":
            target_acts = Activity.objects.filter(name="רכיבה בזריחה")
            appts_qs = base_qs.filter(activities__in=list(target_acts))
            rules_activity = target_acts.first()
            apply_cutoff = True   # כמו בטאב sunrise אצלך
        else:
            # לא הועבר variant חוקי → נניח day
            appts_qs = base_qs.filter(activities__isnull=True)
            rules_activity = activity
            apply_cutoff = False
    else:
        if activity and activity.name in {"רכיבה בזריחה", "רכיבת לילה"}:
            appts_qs = base_qs.filter(activities=activity)
        else:
            # כללי: ריקים או משויכים לאותה פעילות
            appts_qs = base_qs.filter(Q(activities__isnull=True) | Q(activities=activity))
        rules_activity = activity
        apply_cutoff = False

    appts_list = list(appts_qs)
    free_set = {a.time for a in appts_list}

    if rules_activity:
        # get_rules_for אמור להחזיר: (_, cutoff_min, win_start_dt, win_end_dt)
        _, cutoff_min, win_start_dt, win_end_dt = get_rules_for(rules_activity, chosen_date)
        cutoff_min = cutoff_min or 0
    else:
        cutoff_min, win_start_dt, win_end_dt = 0, None, None

    slots_needed = max(1, (int(minutes) + 14)//15)
    needs_buffer = int(minutes) > 30

    start_times = []

    for appt in appts_list:
        start_dt = datetime.combine(appt.date, appt.time)

        if appt.date == today and start_dt < now_naive:
            continue

        # חלון התחלה
        if apply_window and win_start_dt and start_dt < win_start_dt:
            continue

        # cutoff (למשל "חייבים להזמין X דקות מראש") – רלוונטי להיום בלבד וכשapply_cutoff=True
        if apply_cutoff and appt.date == today and cutoff_min > 0:
            if start_dt < now_naive + timedelta(minutes=cutoff_min):
                continue

        # רצף סלוטים לפגישה עצמה
        ok = True
        for i in range(slots_needed):
            t = (start_dt + timedelta(minutes=15*i)).time()
            if t not in free_set:
                ok = False
                break
        if not ok:
            continue

        # חלון סוף – סיום המפגש (בלי ההפסקה)
        end_dt = start_dt + timedelta(minutes=int(minutes))
        if apply_window and win_end_dt and end_dt > win_end_dt:
            continue

        # הפסקה 15 ד׳ אם צריך
        if needs_buffer:
            buffer_start_dt = start_dt + timedelta(minutes=15*slots_needed)
            buffer_end_dt = buffer_start_dt + timedelta(minutes=15)
            if apply_window and win_end_dt and buffer_end_dt > win_end_dt:
                continue
            if buffer_start_dt.time() not in free_set:
                continue

        start_times.append(appt.time.strftime("%H:%M"))

    return sorted(set(start_times))





