from django.contrib import admin
import json
from .models import (
    Activity, Appointment, CustomSchedule, Booking, SiteReview,
    CancellationRequest, TermsConsent, ScheduleBoard, Weekday,
    BusinessHours, ActivityRule, Instructor, TreatmentSession,
    MonthlySummary,
)
from homePage.services.ntfy_gateway import (
    format_session_sms, format_booking_sms, send_treatment_email,
    send_booking_email, get_treatment_amount_nis, ltr_text,
    gen_unique_ref_any, notify_time_change, notify_time_change_booking
)
from django.contrib import messages
from django.shortcuts import redirect
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from django.contrib.admin.helpers import ActionForm
from urllib.parse import urlencode
from datetime import datetime, date as ddate, time as dtime, timedelta
from django.template.response import TemplateResponse
from django.urls import path,reverse
from django.contrib.admin.actions import delete_selected
from decimal import Decimal , ROUND_HALF_UP
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError
from django import forms
from django.shortcuts import render
from homePage.services.ntfy_gateway import send_sms_via_ntfy, normalize_phone_il, send_booking_deleted_email
from django.conf import settings
import re
from django.utils.safestring import mark_safe
from datetime import date
from django.db.models.functions import Coalesce
from django.db.models import Count, Sum, Value, DecimalField
from django.contrib.admin.widgets import AdminDateWidget, AdminTimeWidget, FilteredSelectMultiple
from django.http import HttpResponse
from django.utils.html import format_html
from .models import MarketingConsent
import csv
from django.views.decorators.http import require_POST
from django.utils.crypto import get_random_string
from homePage.services.slot_hold import try_hold_chain, release_hold
from django.db import transaction
from django.utils import timezone
import uuid
from django.db.models import Q

delete_selected.short_description = "מחיקה"

DETAILS_ONLY_PERM = "homePage.change_details_only"
FULL_CHANGE_PERM  = "homePage.change_treatmentsession"

MINUTES_PER_SLOT = 15

NIGHT_RE    = r"(night|לילה)"
SUNRISE_RE  = r"(sunrise|זריחה|שקיעה|sunset)"
NON_DAY_RE  = r"(night|לילה|sunrise|זריחה|שקיעה|sunset)"


def has_consent_by_phone(phone: str) -> bool:
    sid = normalize_phone_il(phone)
    if not sid:
        return False
    tv = getattr(settings, "TERMS_VERSION", "1.0")
    pv = getattr(settings, "PRIVACY_VERSION", "1.0")
    return (
        TermsConsent.objects.filter(policy="terms",   version=tv, subject_id=sid).exists() and
        TermsConsent.objects.filter(policy="privacy", version=pv, subject_id=sid).exists()
    )

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

def _day_range(base: ddate):
    """7 ימים החל מ-base (כולל)."""
    return [base + timedelta(days=i) for i in range(7)]

def _fmt_ils(amount: Decimal) -> str:
        q = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        s = f"{q:,.2f}".replace(",", "")
        if s.endswith(".00"):
            s = s[:-3]
        return f"₪ {s}"

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
            # --- תיוג לרכיבה זוגית לפי activity_type כדי להבדיל ביומן ---
            if (not is_break) and appt.activity and (appt.activity.name == "רכיבה זוגית"):
                t = (appt.activity.activity_type or "").strip().lower()

                if ("picnic" in t) or ("פיקניק" in t):
                    tag = "פיקניק"
                elif ("night" in t) or ("לילה" in t):
                    tag = "לילה"
                elif ("sunrise" in t) or ("זריחה" in t):
                    tag = "זריחה"
                else:
                    tag = "יום"

                title = f"רכיבה זוגית — {tag}"
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

    act_qs = Activity.objects.filter(name__iexact=activity_name)
    activity = act_qs.first()

    base_qs = (
        Appointment.objects
        .filter(is_break=False, date=chosen_date)
        .filter(
            Q(is_booked=False) &
            (Q(hold_until__isnull=True) | Q(hold_until__lte=now_aw))
        )
        .order_by("time")
        .distinct()
    )
    base_free_set = {a.time for a in base_qs}

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
    allowed_set = free_set

    if rules_activity:
        # get_rules_for אמור להחזיר: (_, cutoff_min, win_start_dt, win_end_dt)
        from .views import get_rules_for
        _, cutoff_min, win_start_dt, win_end_dt = get_rules_for(rules_activity, chosen_date)
        cutoff_min = cutoff_min or 0
    else:
        cutoff_min, win_start_dt, win_end_dt = 0, None, None

    slots_needed = max(1, (int(minutes) + 14)//15)
    needs_buffer = int(minutes) > 30

    start_times = []

    for appt in appts_list:
        start_dt = datetime.combine(appt.date, appt.time)

        #if appt.date == today and start_dt < now_naive:
            #continue

        # חלון התחלה
        if apply_window and win_start_dt and start_dt < win_start_dt:
            continue

        # cutoff (למשל "חייבים להזמין X דקות מראש") – רלוונטי להיום בלבד וכשapply_cutoff=True
        if apply_cutoff and appt.date == today and cutoff_min > 0:
            if start_dt < now_naive + timedelta(minutes=cutoff_min):
                continue

        # -------- בדיקת רצף סלוטים --------
        # הסלוט הראשון חייב להיות מתוך appts_qs (כלומר free_set),
        # סלוטי המשך יכולים להיות מכלל הסלוטים הפנויים של אותו יום (base_free_set).
        ok = True
        for i in range(slots_needed):
            t = (start_dt + timedelta(minutes=15 * i)).time()
            if i == 0:
                # תחילת המפגש — חייבת להיות בסט המסונן לפי הווריאנט
                if t not in free_set:
                    ok = False
                    break
            else:
                # סלוטי ההמשך — מספיק שיהיו פנויים ביום
                if t not in allowed_set:
                    ok = False
                    break
        if not ok:
            continue

        # חלון סוף — סיום המפגש (בלי ההפסקה)
        end_dt = start_dt + timedelta(minutes=int(minutes))
        if apply_window and win_end_dt and end_dt > win_end_dt:
            continue

        # הפסקה 15 ד׳ אם צריך — גם היא מותרת להיות מכל סלוט פנוי ביום
        if needs_buffer:
            buffer_start_dt = start_dt + timedelta(minutes=15 * slots_needed)
            buffer_end_dt = buffer_start_dt + timedelta(minutes=15)
            if apply_window and win_end_dt and buffer_end_dt > win_end_dt:
                continue
            if buffer_start_dt.time() not in allowed_set:
                continue

        start_times.append(appt.time.strftime("%H:%M"))

    return sorted(set(start_times))

def _parse_time_flex(tstr: str):
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(tstr, fmt).time().replace(second=0, microsecond=0)
            except ValueError:
                pass
        raise ValueError("bad time")

# ---- עזר לבחירת שעות מדריכים (09:00–20:00 כל 15 דק') ----
def time_choices(start_h=9, end_h=20, step_min=15):
    base = datetime(2000, 1, 1, start_h, 0)
    end = datetime(2000, 1, 1, end_h, 0)
    out = []
    while base <= end:
        out.append((base.time().replace(second=0, microsecond=0), base.strftime("%H:%M")))
        base += timedelta(minutes=step_min)
    return out

def _format_session_time_range(date_obj, start_time, end_time) -> str:
    def hhmm(t): return t.strftime("%H:%M") if t else ""
    if date_obj and start_time and end_time:
        rng = f"{hhmm(start_time)}–{hhmm(end_time)}"
        return f"{date_obj:%d.%m.%Y} בשעה {ltr_text(rng)}"
    if date_obj and start_time:
        return f"{date_obj:%d.%m.%Y} בשעה {ltr_text(hhmm(start_time))}"
    if date_obj:
        return f"{date_obj:%d.%m.%Y}"
    return ""

def _defer_capture_for_activity(act) -> bool:
    """
    החזר True אם לא רוצים לתפוס סלוטים עד תשלום.
    כאן לפי שם פעילות, אפשר להפוך בעתיד לשדה ייעודי במודל.
    """
    nm = (getattr(act, "name", "") or "").strip()
    return nm in {"רכיבה טיפולית", "שיעורי רכיבה", "שיעורי רכיבה/ טיפולית"}

def admin_pay_stub(request):
    """
    דף תשלום אחיד:
    - GET: מציג תמיד אותו דף עם כפתור "בצע תשלום".
    - POST: לפי מצב מזוהה (mode) מבצע את התשלום/יצירה/תפיסת סלוטים ושולח אישורים.

    סדר זיהוי מצב (mode):
      1) יש טיוטת הזמנה ב-session  → 'booking_draft'
      2) יש טיוטת מפגש ב-session   → 'session_draft'
      3) יש id ב-GET/POST → קודם ננסה Booking, אם לא – TreatmentSession
      4) אם אין שום הקשר → נחזיר הודעה ו־redirect אחורה (אין מסך chooser).
    """
    title = "תשלום"
    booking_draft = request.session.get("booking_draft") or {}
    session_draft  = request.session.get("session_draft") or {}
    raw_id = (request.GET.get("id") or request.POST.get("id") or "").strip()

    mode = None
    obj  = None

    if booking_draft:
        mode = "booking_draft"
    elif session_draft:
        mode = "session_draft"
    elif raw_id.isdigit():
        obj = Booking.objects.filter(pk=int(raw_id)).first()
        if obj:
            mode = "booking"
        else:
            obj = TreatmentSession.objects.filter(pk=int(raw_id)).first()
            if obj:
                mode = "session"

    if not mode:
        messages.error(request, "אין תשלום פעיל. חזרי למסך ההוספה/הלוח והמשיכי לתשלום משם.")
        return redirect(reverse("admin:index"))

    # ===== POST: ביצוע התשלום =====
    if request.method == "POST":
        # ---------- הזמנה מטיוטה ----------
        if mode == "booking_draft":
            try:
                with transaction.atomic():
                    activity = Activity.objects.select_for_update().get(pk=int(booking_draft["activity_id"]))
                    d = datetime.fromisoformat(booking_draft["date"]).date()
                    start_time = datetime.strptime(booking_draft["start"], "%H:%M:%S").time()
                    minutes = int(booking_draft["minutes"])
                    participants = int(booking_draft["participants"])

                    start_dt = datetime.combine(d, start_time)
                    slot_count = max(1, (minutes + 14) // 15)
                    times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

                    # לוודא שהסלוטים עדיין פנויים
                    appts = list(
                        Appointment.objects.select_for_update().filter(
                            date=d, time__in=times_needed, is_booked=False, is_break=False
                        ).order_by("time")
                    )
                    if len(appts) != slot_count:
                        messages.error(request, "הסלוטים כבר נתפסו; התשלום לא בוצע.")
                        return redirect(reverse("admin:homePage_appointment_book"))

                    # סכום
                    total_price = Decimal(booking_draft["total_price"]) if booking_draft.get("total_price") else None
                    manual_total = Decimal(booking_draft["manual_total"]) if booking_draft.get("manual_total") else None
                    if manual_total is not None:
                        total_price = manual_total

                    # יצירת ההזמנה (שולם)
                    booking = Booking.objects.create(
                        activity=activity,
                        customer_name=f'{booking_draft["customer"]["first_name"]} {booking_draft["customer"]["last_name"]}'.strip(),
                        customer_phone=booking_draft["customer"]["phone"],
                        customer_email=booking_draft["customer"]["email"],
                        participants=participants,
                        total_price=total_price,
                        payment_method="admin",
                        status="paid",
                        start_dt=start_dt,
                        end_dt=start_dt + timedelta(minutes=minutes),
                        details=booking_draft.get("details") or "נקבע באדמין",
                    )
                    booking.payment_ref = gen_unique_ref_any()
                    booking.save(update_fields=["payment_ref"])

                    # תפיסת סלוטים
                    for a in appts:
                        a.booking = booking
                        a.is_booked = True
                        a.is_paid = True
                        a.is_break = False
                        a.payment_reference = booking.payment_ref
                        if a.activity_id != activity.id:
                            a.activity = activity
                        a.save(update_fields=["booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"])
                        if hasattr(a, "activities"):
                            a.activities.add(activity)

                    # הפסקת 15 דק׳ אם צריך (אם פנויה)
                    if minutes > 30:
                        extra_start = start_dt + timedelta(minutes=15 * slot_count)
                        extra = Appointment.objects.select_for_update().filter(
                            date=d, time=extra_start.time(), is_booked=False, is_break=False
                        ).first()
                        if extra:
                            extra.booking = booking
                            extra.is_booked = True
                            extra.is_break = True
                            extra.is_paid = False
                            extra.payment_reference = booking.payment_ref
                            if extra.activity_id != activity.id:
                                extra.activity = activity
                            extra.save(update_fields=["booking", "is_booked", "is_break", "is_paid", "payment_reference", "activity"])

            except Exception:
                messages.error(request, "שגיאה במהלך התשלום/היצירה.")
                return redirect(reverse("admin:homePage_appointment_book"))

            # מייל + SMS
            try:
                agorot = int(Decimal(booking.total_price or 0) * 100)
                payment_like = SimpleNamespace(
                    email=booking.customer_email,
                    customer_name=booking.customer_name,
                    amount_agorot=agorot,
                    charge_id=booking.payment_ref,
                )
                send_booking_email(payment_like, booking)
                if getattr(settings, "SEND_SMS", False) and (booking.customer_phone or "").strip():
                    sms_text = format_booking_sms(payment_like, booking)
                    try:
                        send_sms_via_ntfy(booking.customer_phone, sms_text)
                    except Exception:
                        pass
            except Exception:
                pass

            request.session.pop("booking_draft", None)
            messages.success(request, f"התשלום בוצע וההזמנה #{booking.id} נוצרה.")
            return redirect(reverse("admin:homePage_booking_change", args=[booking.id]))

        # ---------- תשלום להזמנה קיימת ----------
        if mode == "booking" and obj:
            start_dt = obj.start_dt
            end_dt = obj.end_dt
            if not (start_dt and end_dt):
                messages.error(request, "חסרות שעות התחלה/סיום בהזמנה.")
                return redirect(reverse("admin:homePage_booking_change", args=[obj.id]))

            minutes = int((end_dt - start_dt).total_seconds() // 60)
            slot_count = max(1, (minutes + 14) // 15)
            times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]
            day = start_dt.date()

            try:
                with transaction.atomic():
                    if not (obj.payment_ref or "").strip():
                        obj.payment_ref = gen_unique_ref_any()

                    appts = list(
                        Appointment.objects.select_for_update().filter(
                            date=day, time__in=times_needed, is_booked=False, is_break=False
                        ).order_by("time")
                    )
                    if len(appts) != slot_count:
                        messages.error(request, "הסלוטים כבר נתפסו. התשלום בוטל.")
                        return redirect(reverse("admin:homePage_booking_change", args=[obj.id]))

                    for a in appts:
                        a.booking = obj
                        a.is_booked = True
                        a.is_paid = True
                        a.is_break = False
                        a.payment_reference = obj.payment_ref
                        if a.activity_id != obj.activity_id:
                            a.activity = obj.activity
                        a.save(update_fields=["booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"])
                        if hasattr(a, "activities"):
                            a.activities.add(obj.activity)

                    if minutes > 30:
                        extra_start = start_dt + timedelta(minutes=15 * slot_count)
                        extra = Appointment.objects.select_for_update().filter(
                            date=day, time=extra_start.time(), is_booked=False, is_break=False
                        ).first()
                        if extra:
                            extra.booking = obj
                            extra.is_booked = True
                            extra.is_break = True
                            extra.is_paid = False
                            extra.payment_reference = obj.payment_ref
                            if extra.activity_id != obj.activity_id:
                                extra.activity = obj.activity
                            extra.save(update_fields=["booking", "is_booked", "is_break", "is_paid", "payment_reference", "activity"])

                    obj.status = "paid"
                    if not (obj.payment_method or "").strip():
                        obj.payment_method = "admin"
                    obj.save()
            except Exception:
                messages.error(request, "שגיאה במהלך התשלום/תפיסת סלוטים.")
                return redirect(reverse("admin:homePage_booking_change", args=[obj.id]))

            try:
                agorot = int(Decimal(obj.total_price or 0) * 100)
                payment_like = SimpleNamespace(
                    email=obj.customer_email,
                    customer_name=obj.customer_name,
                    amount_agorot=agorot,
                    charge_id=obj.payment_ref,
                )
                send_booking_email(payment_like, obj)
                if getattr(settings, "SEND_SMS", False) and (obj.customer_phone or "").strip():
                    sms_text = format_booking_sms(payment_like, obj)
                    try:
                        send_sms_via_ntfy(obj.customer_phone, sms_text)
                    except Exception:
                        pass
            except Exception:
                pass

            messages.success(request, f"התשלום בוצע להזמנה #{obj.id}.")
            return redirect(reverse("admin:homePage_booking_change", args=[obj.id]))

        # ---------- יצירת מפגש מטיוטה ----------
        if mode == "session_draft":
            try:
                with transaction.atomic():
                    d  = datetime.fromisoformat(session_draft["date"]).date()
                    st = datetime.strptime(session_draft["start_time"], "%H:%M:%S").time() if session_draft.get("start_time") else None
                    et = datetime.strptime(session_draft["end_time"],   "%H:%M:%S").time() if session_draft.get("end_time")   else None
                    instr = None
                    if session_draft.get("instructor_id"):
                        instr = Instructor.objects.select_for_update().filter(pk=int(session_draft["instructor_id"])).first()

                    obj = TreatmentSession.objects.create(
                        date=d,
                        start_time=st, end_time=et,
                        instructor=instr,
                        customer_full_name=session_draft.get("customer_full_name") or "",
                        customer_phone=session_draft.get("customer_phone") or "",
                        customer_email=session_draft.get("customer_email") or "",
                        lesson_type=session_draft.get("lesson_type") or "",
                        details=session_draft.get("details") or "",
                        payment_ref=gen_unique_ref_any(),
                    )
                    sid = normalize_phone_il((obj.customer_phone or "").strip())
                    if session_draft.get("accept_terms") and sid:
                        # IP מאחורי פרוקסי (אם יש)
                        xff = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
                        ip = xff or request.META.get("HTTP_CF_CONNECTING_IP") \
                             or request.META.get("HTTP_X_REAL_IP") \
                             or request.META.get("REMOTE_ADDR") or ""

                        ua = (request.META.get("HTTP_USER_AGENT") or "")[:400]
                        full_name = (obj.customer_full_name or "").strip()

                        tv = getattr(settings, "TERMS_VERSION", "1.0")
                        pv = getattr(settings, "PRIVACY_VERSION", "1.0")

                        TermsConsent.objects.update_or_create(
                            policy="terms", version=tv, subject_id=sid,
                            defaults={"accepted_at": timezone.now(), "full_name": full_name, "ip": ip,
                                      "user_agent": ua},
                        )
                        TermsConsent.objects.update_or_create(
                            policy="privacy", version=pv, subject_id=sid,
                            defaults={"accepted_at": timezone.now(), "full_name": full_name, "ip": ip,
                                      "user_agent": ua},
                        )

            except Exception:
                messages.error(request, "שגיאה במהלך יצירת המפגש/התשלום.")
                return redirect(reverse("admin:homePage_treatmentsession_add"))

            # אישורים (אופציונלי סכום override בטופס)
            amount = None
            override = session_draft.get("override_amount_nis")
            if override:
                try:
                    amount = Decimal(override)
                except Exception:
                    amount = None
            try:
                send_treatment_email(obj, amount=amount)
            except Exception:
                pass
            try:
                if getattr(settings, "SEND_SMS", False) and (obj.customer_phone or "").strip():
                    phone = normalize_phone_il(obj.customer_phone)
                    sms_text = format_session_sms(obj, amount=amount)
                    try:
                        send_sms_via_ntfy(phone, sms_text)
                    except Exception:
                        pass
            except Exception:
                pass

            request.session.pop("session_draft", None)
            messages.success(request, f"התשלום בוצע ונשלחו אישורים למפגש #{obj.id}.")
            return redirect(reverse("admin:homePage_treatmentsession_change", args=[obj.id]))

        # ---------- "תשלום" למפגש קיים ----------
        if mode == "session" and obj:
            if not (obj.payment_ref or "").strip():
                obj.payment_ref = gen_unique_ref_any()
                obj.save(update_fields=["payment_ref"])

            try:
                send_treatment_email(obj)
            except Exception:
                pass
            try:
                if getattr(settings, "SEND_SMS", False) and (obj.customer_phone or "").strip():
                    phone = normalize_phone_il(obj.customer_phone)
                    sms_text = format_session_sms(obj)
                    try:
                        send_sms_via_ntfy(phone, sms_text)
                    except Exception:
                        pass
            except Exception:
                pass

            messages.success(request, f"התשלום בוצע ונשלחו אישורים למפגש #{obj.id}.")
            return redirect(reverse("admin:homePage_treatmentsession_change", args=[obj.id]))

        # אם התגלגלנו לפה – אין מצב נתמך
        messages.error(request, "מצב תשלום לא מזוהה.")
        return redirect(reverse("admin:index"))

    # ===== GET: מציגים תמיד אותו דף תשלום (בלי chooser) =====
    ctx = {
        "title": title,
        "opts": (Booking._meta if mode.startswith("booking") else TreatmentSession._meta),
        "mode": mode,
        "obj": obj,
        "draft": booking_draft if mode == "booking_draft" else (session_draft if mode == "session_draft" else None),
        "has_draft": mode in {"booking_draft", "session_draft"},
    }
    return render(request, "admin/homePage/pay_stub.html", ctx)

# === עזרי חישוב מחיר לפי Activity "שיעורי רכיבה/ טיפולית" ===
def _minutes_between(date_obj, start_time, end_time):
    if not (date_obj and start_time and end_time):
        return None
    dt0 = datetime.combine(date_obj, start_time)
    dt1 = datetime.combine(date_obj, end_time)
    if dt1 <= dt0:  # אם חוצה חצות
        dt1 += timedelta(days=1)
    return int((dt1 - dt0).total_seconds() // 60)

def _price_from_treatment_activity_by_minutes(minutes) -> Decimal | None:
    try:
        qs = Activity.objects.filter(name="שיעורי רכיבה/ טיפולית").exclude(price__isnull=True)
        if minutes is not None:
            act = qs.filter(duration_minutes=minutes).order_by("id").first()
            if act and act.price is not None:
                return Decimal(act.price)
        act = qs.order_by("duration_minutes", "id").first()
        if act and act.price is not None:
            return Decimal(act.price)
    except Exception:
        pass
    return None

def _suggest_amount_for_session(session) -> Decimal | None:
    minutes = _minutes_between(getattr(session, "date", None),
                               getattr(session, "start_time", None),
                               getattr(session, "end_time", None))
    return _price_from_treatment_activity_by_minutes(minutes)



class TreatmentSessionAdminForm(forms.ModelForm):
    calc_amount_nis = forms.DecimalField(
        label="סכום לתשלום (מחושב) ₪",
        required=False, disabled=True, decimal_places=2, max_digits=8,
        help_text="נלקח אוטומטית מה'שיעורי רכיבה/ טיפולית' לפי משך המפגש."
    )
    override_amount_nis = forms.DecimalField(
        label="שינוי מחיר להזמנה זו (₪)",
        required=False, decimal_places=2, max_digits=8,
        help_text="אם תוזן כאן–זה יהיה המחיר הסופי להזמנה זו (למייל/‏SMS)."
    )
    accept_terms = forms.BooleanField(
        label="אני מאשר/ת את תנאי השימוש ומדיניות הפרטיות",
        required=False
    )

    class Meta:
        model = TreatmentSession
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

        # ווידג'טים לשעות (כמו אצלך)
        if "start_time" in self.fields:
            self.fields["start_time"].widget = forms.Select(choices=time_choices())
        if "end_time" in self.fields:
            self.fields["end_time"].widget = forms.Select(choices=time_choices())

        # מחיר מחושב להצגה
        sug = _suggest_amount_for_session(self.instance)
        if sug is None:
            date_i  = self.initial.get("date") or getattr(self.instance, "date", None)
            st_i    = self.initial.get("start_time") or getattr(self.instance, "start_time", None)
            et_i    = self.initial.get("end_time") or getattr(self.instance, "end_time", None)
            minutes = _minutes_between(date_i, st_i, et_i)
            sug     = _price_from_treatment_activity_by_minutes(minutes)
        if sug is not None:
            self.fields["calc_amount_nis"].initial = sug

        # הרשאות מדריכים (נשאר כמו שהיה)
        user = getattr(self.request, "user", None)
        is_instructor = bool(user and user.groups.filter(name="Instructors").exists())
        inst = getattr(self.instance, "instructor", None)
        owns = bool(is_instructor and inst and getattr(inst, "user_id", None) == getattr(user, "id", None))

        if is_instructor:
            if owns:
                for name, field in self.fields.items():
                    field.disabled = (name != "details")
                self.fields["details"].required = False
            else:
                for field in self.fields.values():
                    field.disabled = True

        self.fields["accept_terms"].help_text = mark_safe(r"""
        <script>
        (function(){
          function ready(fn){ if(document.readyState!=="loading") fn(); else document.addEventListener("DOMContentLoaded", fn); }
          ready(function(){
            var phone = document.getElementById("id_customer_phone");
            var row   = document.querySelector(".field-accept_terms");
            var box   = document.getElementById("id_accept_terms");
            if(!phone || !row || !box) return;

            async function refresh(){
              var val = (phone.value || "").trim();
              if(!val){
                // אין טלפון → נשאיר את הצ'קבוקס גלוי
                row.style.display = "";
                box.checked = false;
                return;
              }
              try{
                const url = "/admin/homePage/treatmentsession/check-consent/?phone=" + encodeURIComponent(val);
                const r = await fetch(url, {headers: {"X-Requested-With":"XMLHttpRequest"}});
                const data = await r.json();
                const has = !!(data.has || data.has_consent);
                if(has){
                  row.style.display = "none";
                  box.checked = true;
                  box.required = false;   // ← חדש
                }else{
                  row.style.display = "";
                  box.checked = false;
                  box.required = true;    // ← חדש
                }

              }catch(e){
                row.style.display = "";         // במקרה כשל, לא להסתיר
              }
            }

            // אירועים רלוונטיים
            ["change","blur","keyup","input"].forEach(function(ev){
              phone.addEventListener(ev, function(){ refresh(); });
            });

            // בדיקה ראשונית בטעינה
            refresh();
          });
        })();
        </script>
                """)

    def clean(self):
        cleaned = super().clean()
        phone = normalize_phone_il((cleaned.get("customer_phone") or "").strip())
        need_accept = bool(phone) and (not has_consent_by_phone(phone))
        if need_accept and not cleaned.get("accept_terms"):
            self.add_error("accept_terms", "יש לאשר את תנאי השימוש ומדיניות הפרטיות.")
        return cleaned

    def clean_override_amount_nis(self):
        v = self.cleaned_data.get("override_amount_nis")
        if v is not None and v < 0:
            raise forms.ValidationError("מחיר לא יכול להיות שלילי.")
        return v

@admin.register(Instructor)
class InstructorAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "user", "active")
    list_filter = ("active",)
    search_fields = ("full_name", "phone", "user__username")

@admin.register(TreatmentSession)
class TreatmentSessionAdmin(admin.ModelAdmin):
    form = TreatmentSessionAdminForm
    change_list_template = "admin/homePage/treatmentsession_changelist.html"

    list_display = (
        "date", "start_time", "end_time","payment_ref", "get_instructor_name",
        "customer_full_name", "customer_phone", "_details_short"
    )

    list_filter = ("date",)
    search_fields = ("customer_full_name", "customer_phone", "instructor__full_name", "details", "payment_ref")
    ordering = ("-date", "start_time")

    fieldsets = (
        ("פרטי מפגש", {"fields": ("date", "start_time", "end_time", "instructor", "payment_ref")}),
        ("פרטי לקוח", {"fields": ("customer_full_name", "customer_phone", "customer_email", "accept_terms")}),
        ("סוג ומחיר", {"fields": ("lesson_type", "calc_amount_nis", "override_amount_nis")}),
        ("פרטים/הערות", {"fields": ("details",)}),
    )

    @admin.display(description="מדריך/ה")
    def instructor_plain(self, obj):
        return obj.instructor.full_name if obj and obj.instructor else "-"


    def add_view(self, request, form_url='', extra_context=None):
        if request.method == "POST":
            FormClass = self.get_form(request)
            form = FormClass(request.POST, request.FILES)
            if form.is_valid():
                cd = form.cleaned_data
                draft = {
                    "date": (cd.get("date") or timezone.localdate()).isoformat(),
                    "start_time": cd.get("start_time").strftime("%H:%M:%S") if cd.get("start_time") else None,
                    "end_time":   cd.get("end_time").strftime("%H:%M:%S")   if cd.get("end_time")   else None,
                    "instructor_id": cd["instructor"].pk if cd.get("instructor") else None,
                    "customer_full_name": cd.get("customer_full_name") or "",
                    "customer_phone":     cd.get("customer_phone") or "",
                    "customer_email":     cd.get("customer_email") or "",
                    "lesson_type":        cd.get("lesson_type") or "",
                    "details":            cd.get("details") or "",
                    "accept_terms": bool(cd.get("accept_terms")),
                    # אם יש לך שדה בטופס לשינוי מחיר:
                    "override_amount_nis": str(cd.get("override_amount_nis") or ""),
                }
                request.session["session_draft"] = draft
                request.session.modified = True
                messages.success(request, "הפרטים נשמרו כטיוטה. המשיכי לתשלום.")
                return redirect(f"{reverse('admin:homePage_admin_pay_stub')}?kind=session_draft")
            # אם הטופס לא תקין – נציג שגיאות כרגיל
        return super().add_view(request, form_url, extra_context)

    def response_add(self, request, obj, post_url_continue=None):
        # לא נשתמש בזה במסלול הטיוטה
        return super().response_add(request, obj, post_url_continue)

    def get_fieldsets(self, request, obj=None):
        fs = list(super().get_fieldsets(request, obj))
        if request.user.has_perm(DETAILS_ONLY_PERM) and not request.user.has_perm(FULL_CHANGE_PERM):
            new = []
            for title, opts in fs:
                fields = opts.get("fields")
                if fields:
                    def repl(x):
                        if isinstance(x, (list, tuple)):
                            return tuple("instructor_plain" if f == "instructor" else f for f in x)
                        return "instructor_plain" if x == "instructor" else x

                    fields = tuple(repl(f) for f in fields)
                    opts = {**opts, "fields": fields}
                new.append((title, opts))
            fs = new
        return fs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name == "instructor":
            w = formfield.widget
            for attr in ("can_add_related", "can_change_related", "can_view_related", "can_delete_related"):
                if hasattr(w, attr):
                    setattr(w, attr, False)
        return formfield

    def changelist_view(self, request, extra_context=None):
        # אם זה /list/ - מציגים את הרשימה ולא עושים redirect ללוח
        if getattr(request, "resolver_match", None) and request.resolver_match.url_name == "treatmentsession_list":
            return super().changelist_view(request, extra_context=extra_context)

        # תמיכה לאחור אם בכל זאת יבוא ?list=1
        if (request.GET.get("list", "").lower() in {"1", "true", "yes"}):
            return super().changelist_view(request, extra_context=extra_context)

        # ברירת מחדל: ללוח
        return redirect(reverse("admin:treatmentsession_calendar"))

    def force_list_view(self, request):
        """
        מציג את צ'יינג־ליסט תמיד, בלי תלות בפרמטרים.
        """
        return super().changelist_view(request)

    def get_instructor_name(self, obj):
        return obj.instructor.full_name if obj.instructor else ""

    get_instructor_name.short_description = "מדריך/ה"

    def get_form(self, request, obj=None, **kwargs):
        Form = super().get_form(request, obj, **kwargs)

        class BoundForm(Form):
            def __init__(self2, *a, **k):
                k["request"] = request
                super().__init__(*a, **k)

        return BoundForm

    def get_changeform_initial_data(self, request):
        """
        גורם ל-Add Form לקבל את השעות שנשלחו מהלוח (HH:MM) ולהפוך ל-HH:MM:00
        כדי שיתאימו ל-choices שהם אובייקטי זמן (str(value) -> 'HH:MM:SS').
        """
        data = super().get_changeform_initial_data(request).copy()

        def normalize_time(val: str | None) -> str | None:
            if not val:
                return None
            # אם התקבל 'HH:MM' נוסיף ':00'
            if len(val) == 5 and val.count(':') == 1:
                return f"{val}:00"
            return val

        for key in ("start_time", "end_time"):
            v = request.GET.get(key)
            nv = normalize_time(v)
            if nv:
                data[key] = nv

        # נעביר גם date ו-instructor אם הגיעו ב-GET
        if "date" in request.GET:
            data["date"] = request.GET["date"]
        if "instructor" in request.GET:
            data["instructor"] = request.GET["instructor"]

        return data

    def has_add_permission(self, request):
        return request.user.has_perm("homePage.can_create_sessions")

    def has_delete_permission(self, request, obj=None):
        if request.user.groups.filter(name="Instructors").exists():
            return False
        return super().has_delete_permission(request, obj)

    def has_view_permission(self, request, obj=None):
        # מדריכים רשאים לראות כל הזמנה (גם אם לא שלהם)
        if request.user.groups.filter(name="Instructors").exists():
            return True
        return super().has_view_permission(request, obj)


    def has_change_permission(self, request, obj=None):
        if request.user.has_perm(DETAILS_ONLY_PERM):
            return True                          # יוכל להיכנס לעמוד שינוי
        return super().has_change_permission(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if request.user.has_perm(DETAILS_ONLY_PERM) and not request.user.has_perm(FULL_CHANGE_PERM):
            fields = [f.name for f in self.model._meta.fields] + \
                     [m.name for m in self.model._meta.many_to_many]
            if "details" in fields:
                fields.remove("details")
            fields.append("instructor_plain")
            if "payment_ref" not in fields:
                fields.append("payment_ref")
            return fields
        base = super().get_readonly_fields(request, obj)
        return tuple(base) + ("payment_ref",)

    def save_model(self, request, obj, form, change):
        # מסלול הרשאה מצומצם – נשאר כמו שהיה
        if request.user.has_perm(DETAILS_ONLY_PERM) and not request.user.has_perm(FULL_CHANGE_PERM):
            original = self.model.objects.get(pk=obj.pk)
            original.details = form.cleaned_data.get("details", original.details)
            original.save(update_fields=["details"])
            return

        # נשמור ערכים ישנים להשוואה (כדי לדעת אם השעה/תאריך השתנו)
        old_date = old_start = old_end = None
        if change:
            try:
                prev = self.model.objects.get(pk=obj.pk)
                old_date, old_start, old_end = prev.date, prev.start_time, prev.end_time
            except self.model.DoesNotExist:
                pass

        # יצירת מזהה אם חסר
        if not obj.payment_ref:
            obj.payment_ref = gen_unique_ref_any()

        # מחיר: override גובר על המחושב (לוגיקה כפי שהייתה)
        override = form.cleaned_data.get("override_amount_nis")
        try:
            calc = get_treatment_amount_nis(obj)
        except NameError:
            calc = _suggest_amount_for_session(obj)
        effective_amount = override if override is not None else calc

        if override is not None:
            from decimal import Decimal
            old = obj.details or ""
            note = f"\nמחיר הוגדר ידנית להזמנה זו: ₪{Decimal(override):}"
            if calc is not None:
                note += f" (במקום המחושב: ₪{Decimal(calc):.2f})"
            obj.details = (old + note).strip()

        # שמירה בפועל
        super().save_model(request, obj, form, change)
        # === שמירת הסכמה: טלפון + IP + User-Agent ===
        try:
            if form.cleaned_data.get("accept_terms"):
                # טלפון מנורמל
                phone = normalize_phone_il((getattr(obj, "customer_phone", "") or "").strip())
                if phone:
                    # שליפת IP מאחורי פרוקסי אם יש
                    xff = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
                    ip = xff or request.META.get("HTTP_CF_CONNECTING_IP") \
                         or request.META.get("HTTP_X_REAL_IP") \
                         or request.META.get("REMOTE_ADDR") \
                         or ""

                    # קיצור ה-UA למניעת גלישה בשדה
                    ua = (request.META.get("HTTP_USER_AGENT") or "")[:400]

                    full_name = (obj.customer_full_name or "").strip()
                    tv = getattr(settings, "TERMS_VERSION", "1.0")
                    pv = getattr(settings, "PRIVACY_VERSION", "1.0")

                    # תנאי שימוש
                    TermsConsent.objects.update_or_create(
                        policy="terms", version=tv, subject_id=phone,
                        defaults={
                            "accepted_at": timezone.now(),
                            "full_name": full_name,
                            "ip": ip,
                            "user_agent": ua,
                        },
                    )
                    # פרטיות
                    TermsConsent.objects.update_or_create(
                        policy="privacy", version=pv, subject_id=phone,
                        defaults={
                            "accepted_at": timezone.now(),
                            "full_name": full_name,
                            "ip": ip,
                            "user_agent": ua,
                        },
                    )
        except Exception:
            pass

        # אם השעה/תאריך השתנו – שולחים עדכון (מייל + SMS) בלי שום פופאפ מיוחד
        if change and (old_date is not None):
            if (old_date != obj.date) or (old_start != obj.start_time) or (old_end != obj.end_time):
                try:
                    notify_time_change(request, obj, old_date, old_start, old_end)
                except Exception:
                    pass

    def _details_short(self, obj):
        if not obj.details:
            return ""
        txt = obj.details.strip().replace("\n", " ")
        return (txt[:60] + "…") if len(txt) > 60 else txt
    _details_short.short_description = "פרטים"

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = dict(extra_context or {})
        extra_context["title"] = "פרטי ההזמנה"
        return super().changeform_view(request, object_id, form_url, extra_context)

    def check_consent(self, request):
        phone_raw = (request.GET.get("phone") or "").strip()
        sid = normalize_phone_il(phone_raw) if phone_raw else ""
        has = bool(has_consent_by_phone(sid)) if sid else False
        return JsonResponse({"has": has})

    # ---- לוח מדריכים באדמין ----
    def get_urls(self):
        urls = super().get_urls()
        extra = [
            path("calendar/", self.admin_site.admin_view(self.calendar_view), name="treatmentsession_calendar"),
            path("list/",     self.admin_site.admin_view(self.force_list_view), name="treatmentsession_list"),
            path("events/",   self.admin_site.admin_view(self.events_api), name="treatmentsession_events"),
            path("events/update/", self.admin_site.admin_view(self.events_update), name="treatmentsession_events_update"),
            path("check-consent/", self.admin_site.admin_view(self.check_consent), name="treatmentsession_check_consent"),

        ]
        return extra + urls

    def calendar_view(self, request):
        instructors = Instructor.objects.filter(active=True).order_by("full_name")
        is_instr = request.user.groups.filter(name="Instructors").exists()
        ctx = dict(
            title="לוח שנה – רכיבה טיפולית",
            opts=TreatmentSession._meta,
            instructors=instructors,
            add_perm=self.has_add_permission(request),
            can_drag=request.user.has_perm("homePage.can_drag_sessions"),
            can_create=request.user.has_perm("homePage.can_create_sessions"),
        )
        return render(request, "admin/homePage/treatmentsession_calendar.html", ctx)

    def events_api(self, request):
        from django.http import JsonResponse

        start = request.GET.get("start")
        end = request.GET.get("end")
        instr_id = request.GET.get("instructor")

        qs = TreatmentSession.objects.all()
        if start and end:
            qs = qs.filter(date__gte=start[:10], date__lte=end[:10])
        if instr_id and instr_id.isdigit():
            qs = qs.filter(instructor_id=int(instr_id))

        # ✅ מיפוי קוד → תווית בעברית
        def lesson_type_label(code: str) -> str:
            return dict(TreatmentSession.LESSON_TYPE_CHOICES).get(code, "")

        def color_for(instructor):
            if instructor:
                c = (instructor.color or "").strip()
                if c and not c.startswith("#"):
                    c = "#" + c
                if len(c) in (4, 7):
                    return c
            return "#607D8B"

        events = []
        for s in qs.select_related("instructor"):
            events.append({
                "id": s.id,
                "title": "",
                "start": f"{s.date}T{s.start_time.strftime('%H:%M:%S')}",
                "end": f"{s.date}T{s.end_time.strftime('%H:%M:%S')}",
                "url": reverse("admin:homePage_treatmentsession_change", args=[s.id]),
                "color": color_for(s.instructor),
                "extendedProps": {
                    "instructorName": s.instructor.full_name if s.instructor else "",
                    "customerName": s.customer_full_name,
                    "customerPhone": s.customer_phone,
                    "timeText": f"{s.start_time.strftime('%H:%M')} – {s.end_time.strftime('%H:%M')}",
                    # ✅ הוספות חשובות:
                    "lessonType": getattr(s, "lesson_type", "") or "",
                    "lessonTypeLabel": lesson_type_label(getattr(s, "lesson_type", "")),
                },
            })
        return JsonResponse(events, safe=False)

    def events_update(self, request):
        if not request.user.has_perm("homePage.can_drag_sessions"):
            return JsonResponse({"ok": False, "error": "אין הרשאה להזיז אירועים"}, status=403)
        if request.method != "POST":
            return HttpResponseBadRequest("invalid method")

        # חסימה מוחלטת למדריכים – אי אפשר להזיז/למתוח אירועים
        if request.user.groups.filter(name="Instructors").exists():
            return JsonResponse({"ok": False, "error": "אין הרשאה להזיז אירועים"}, status=403)

        obj = get_object_or_404(TreatmentSession, pk=request.POST.get("id"))

        # שמירת ישן להשוואה
        old_date, old_start, old_end = obj.date, obj.start_time, obj.end_time

        date_str = request.POST.get("date")
        start_str = request.POST.get("start")
        end_str = request.POST.get("end")

        try:
            new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            new_start = _parse_time_flex(start_str)
            new_end = _parse_time_flex(end_str)
        except Exception:
            return JsonResponse({"ok": False, "error": "פורמט תאריך/שעה שגוי"}, status=400)

        obj.date = new_date
        obj.start_time = new_start
        obj.end_time = new_end

        try:
            obj.full_clean()
            obj.save()
        except ValidationError as e:
            return JsonResponse({"ok": False, "error": e.message_dict}, status=400)

        changed = (old_date != new_date or old_start != new_start or old_end != new_end)
        notified = False
        if changed and request.POST.get("notify") == "1":
            try:
                notified = notify_time_change(request, obj, old_date, old_start, old_end) or False
            except Exception:
                notified = False

        return JsonResponse({"ok": True, "notified": notified})


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
        return render(request, "admin/homePage/schedule/change_list.html", ctx)

@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "activity_type", "duration_minutes")
    search_fields = ("name", "description")
    list_filter = ("activity_type", "duration_minutes")
    ordering = ("name",)
    list_display_links = ("id", "name")

class CustomScheduleForm(forms.ModelForm):
    class Meta:
        from .models import CustomSchedule
        model = CustomSchedule
        fields = "__all__"

    # מציגים את h_day כשדה טקסט כדי שאפשר יהיה להקליד אותיות
    h_day = forms.CharField(
        label="יום בחודש",
        required=False,
        help_text='אפשר לכתוב מספר (1–30) או אותיות (למשל: כ״ה, ט״ו).',
        widget=forms.TextInput(attrs={"placeholder": 'כ״ה / 25', "dir": "rtl"})
    )

    def clean_h_day(self):
        if self.cleaned_data.get("kind") != "HEBREW":
            return None
        val = self.cleaned_data.get("h_day")
        if val in (None, ""):
            return None  # מותר להשאיר ריק כשלא משתמשים בסוג=עברי

        s = str(val).strip()
        if s.isdigit():
            n = int(s)
        else:
            # הסרת גרש/גרשיים ורווחים
            s = s.replace('״','').replace('"','').replace("'", "").replace("׳","").replace(" ", "")
            map_ = {"א":1,"ב":2,"ג":3,"ד":4,"ה":5,"ו":6,"ז":7,"ח":8,"ט":9,"י":10,"כ":20,"ך":20,"ל":30}
            total = 0
            for ch in s:
                if ch not in map_:
                    raise forms.ValidationError("יום עברי לא חוקי. כתבי למשל כ״ה או מספר 25.")
                total += map_[ch]
            n = total

        if not (1 <= n <= 30):
            raise forms.ValidationError("היום חייב להיות בין 1 ל־30.")
        return n

@admin.register(CustomSchedule)
class CustomScheduleAdmin(admin.ModelAdmin):
    form = CustomScheduleForm

    list_display = (
        "kind", "label", "is_active",
        "date", "repeat_every_year",        # ← הוסף כאן אם תרצי לראות בעמוד הרשימה
        "h_month", "h_day", "adar_policy",
        "preview_this_year",
        "start_time", "end_time",
        "allow_sunrise", "allow_night",
    )
    list_filter = ("kind", "is_active", "repeat_every_year", "h_month", "adar_policy")
    ordering = ("-id",)

    fieldsets = (
        ("כללי", {"fields": ("kind", "name", "is_active")}),
        ("הגדרה לועזית", {"fields": ("date", "repeat_every_year")}),  # ← הוסף כאן
        ("הגדרה עברית", {"fields": ("h_month", "h_day", "adar_policy")}),
        ("טווחי שעות", {"fields": ("start_time", "end_time")}),
        ("זריחה/לילה", {"fields": (
            "allow_sunrise", "sunrise_start_time", "sunrise_end_time",
            "allow_night", "night_start_time", "night_end_time",
        )}),
    )
    search_fields = ("name",)
    date_hierarchy = "date"

    @admin.display(description="תאריך השנה (תצוגה)")
    def preview_this_year(self, obj: CustomSchedule):
        g = obj.to_gregorian_for_year(date.today().year)
        return g.strftime("%d.%m.%Y") if g else "—"

@admin.register(Weekday)
class WeekdayAdmin(admin.ModelAdmin):
    list_display = ("code", "name")

@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "duration_minutes", "is_booked", "is_break", "activities_list")
    list_filter  = ("is_booked", "is_break", "date", "activities")  # ← סינון לפי ה-M2M
    date_hierarchy = "date"
    ordering = ("-date", "time")
    search_fields = ("customer_name", "customer_phone")
    filter_horizontal = ("activities",)  # ← נשאר (זה ה-M2M על Appointment)

    @admin.display(description="פעילויות")
    def activities_list(self, obj):
        return ", ".join(obj.activities.values_list("name", flat=True)) or "—"

    # ברירת מחדל: "הוסף תור" פותח את מחולל התורים (השאירי כמו שהיה לך)
    def add_view(self, request, form_url="", extra_context=None):
        if request.GET.get("single") == "1":
            return super().add_view(request, form_url, extra_context)

        if request.method == "POST" and request.POST.get("_generate") == "1":
            form = SlotGeneratorForm(request.POST)
            if form.is_valid():
                totals, err = _generate_slots_from_form(form.cleaned_data)
                if err:
                    messages.error(request, err)
                else:
                    messages.success(
                        request,
                        f"נוצרו {totals['created']} תורים (15 דק׳), "
                        f"דילוג על {totals['skipped']} קיימים."
                    )
                list_url = reverse("admin:homePage_appointment_changelist")
                return redirect(f"{list_url}?date__exact={form.cleaned_data['date'].isoformat()}")
        else:
            form = SlotGeneratorForm()

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "form": form,
            "original_add_url": request.path + "?single=1",
            "title": "יצירת תורים",
        }
        return TemplateResponse(request, "admin/homePage/appointment_generate.html", context)

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
        from .models import Booking  # או הייבוא המתאים אצלך
        model = Booking
        fields = "__all__"
        labels = {
            "activity": "פעילות", "customer_name": "שם לקוח", "customer_phone": "טלפון",
            "customer_email": "אימייל", "participants": "מספר משתתפים",
            "total_price": "סה\"כ לתשלום", "payment_method": "אמצעי תשלום",
            "payment_ref": "אסמכתא/מזהה תשלום", "status": "סטטוס",
            "notes": "פרטים/הערות", "start_dt": "תאריך ושעת התחלה", "end_dt": "תאריך ושעת סיום",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        inst = getattr(self, "instance", None)
        if not (inst and inst.pk and inst.start_dt and inst.end_dt and inst.activity_id):
            return

        f_start = self.fields.get("start_dt")
        f_end = self.fields.get("end_dt")
        if not (f_start and f_end):
            return

        # משך הפעילות בפועל (בלי ההפסקה)
        minutes_real = int((inst.end_dt - inst.start_dt).total_seconds() // 60)
        day = inst.start_dt.date()
        name = inst.activity.name

        # וריאנט לזוגית
        variant = None
        if name == "רכיבה זוגית":
            t = (inst.activity.activity_type or "").lower()
            if ("night" in t) or ("לילה" in t):
                variant = "night"
            elif any(x in t for x in ("sunrise", "זריחה", "שקיעה", "sunset")):
                variant = "sunrise"
            else:
                variant = "day"

        # לבדיקת זמינות – הוספת 15 ד' הפסקה כשמשך>30
        minutes_for_query = minutes_real + (15 if minutes_real > 30 else 0)

        # השעות הפנויות הרגילות (ללא הסלוטים שלך)
        times = find_free_start_times(day, minutes_for_query, name, variant=variant)

        # === הוספת "פנויות עבורך": זמני התחלה שמותרים אם מתייחסים לסלוטים של ההזמנה שלך כאל פנויים ===
        # לא משחרר כלום – רק מוסיף לרשימה של ה-selector.
        try:
            from datetime import datetime, timedelta

            slot_cnt_q = max(1, (minutes_for_query + 14) // 15)

            # כל הסלוטים של אותו יום
            day_rows = list(
                Appointment.objects.filter(date=day)
                .values("time", "is_booked", "is_break", "booking_id")
            )
            slot_map = {r["time"]: r for r in day_rows}
            own_set = {r["time"] for r in day_rows if r["booking_id"] == inst.id}
            all_starts = sorted(slot_map.keys())

            def t_add(t, k=1):
                return (datetime.combine(day, t) + timedelta(minutes=15 * k)).time()

            def chain_ok(start_t):
                """אפשר להתחיל ב-start_t אם כל השרשרת זמינה או שייכת להזמנה הזו,
                   וחייבת להיות חפיפה כלשהי עם סלוטים של ההזמנה הנוכחית."""
                overlap = False
                for i in range(slot_cnt_q):
                    tt = t_add(start_t, i)
                    row = slot_map.get(tt)
                    if not row:
                        return False
                    # סלוט תפוס ע״י מישהו אחר? נפסל.
                    if row["is_booked"] and row["booking_id"] != inst.id:
                        return False
                    # הפסקה של מישהו אחר? נפסל.
                    if row["is_break"] and row["booking_id"] != inst.id:
                        return False
                    if tt in own_set:
                        overlap = True
                return overlap

            extra_times = [t.strftime("%H:%M") for t in all_starts if chain_ok(t)]
            # מאחדים ומסירים כפילויות
            times = sorted(set(times) | set(extra_times))
        except Exception:
            # לא מפיל את הטופס אם יש תקלה – פשוט נסתמך על times המקורי
            pass

        # תמיד להציג גם את שעת ההזמנה הנוכחית
        cur = inst.start_dt.strftime("%H:%M")
        if cur not in times:
            times = [cur] + times

        # --- נעילה ויזואלית של start_dt/end_dt + צריבת ה-data ל-selector ---
        w = f_start.widget
        if hasattr(w, "widgets"):  # AdminSplitDateTime
            w.widgets[0].attrs.update({
                "readonly": "readonly",
                "style": "pointer-events:none;background:#f6f6f6;",
            })
            w.widgets[1].attrs.update({
                "readonly": "readonly",
                "style": "pointer-events:none;background:#f6f6f6;",
                "data-times": json.dumps(times, ensure_ascii=False),
                "data-day": day.isoformat(),
                "data-duration": str(minutes_real),  # משך אמיתי (בלי ההפסקה)
            })
        else:  # שדה יחיד
            w.attrs.update({
                "readonly": "readonly",
                "style": "pointer-events:none;background:#f6f6f6;",
                "data-times": json.dumps(times, ensure_ascii=False),
                "data-day": day.isoformat(),
                "data-duration": str(minutes_real),
            })

        w2 = f_end.widget
        if hasattr(w2, "widgets"):
            for sub in w2.widgets:
                sub.attrs.update({"readonly": "readonly", "style": "pointer-events:none;background:#f6f6f6;"})
        else:
            w2.attrs.update({"readonly": "readonly", "style": "pointer-events:none;background:#f6f6f6;"})


        # מוסיפים selector קטן (ללא טמפלטים), שמעדכן start_dt+end_dt
        self.fields["end_dt"].help_text = mark_safe(r"""
<style>
/* מסתיר את קיצורי "היום/כעת" של שדות השעה */
#id_start_dt_1 + .datetimeshortcuts,
#id_end_dt_1   + .datetimeshortcuts { display: none !important; }
</style>
<script>
(function(){
  function onReady(fn){ if(document.readyState!=='loading') fn(); else document.addEventListener('DOMContentLoaded', fn); }
  onReady(function(){
    var dateStart = document.getElementById("id_start_dt_0");
    var timeStart = document.getElementById("id_start_dt_1") || document.getElementById("id_start_dt");
    var dateEnd   = document.getElementById("id_end_dt_0");
    var timeEnd   = document.getElementById("id_end_dt_1") || document.getElementById("id_end_dt");
    if(!timeStart || !dateStart || !dateEnd || !timeEnd) return;

    var submitRow = document.querySelector(".submit-row");
    var host = submitRow ? submitRow.parentNode : (document.querySelector("form") || document.body);

    var wrap = document.createElement("div");
    wrap.style.margin = "8px 0";
    var label = document.createElement("label");
    label.style.marginInlineStart = "8px";
    label.textContent = "שינוי שעה:";
    var sel = document.createElement("select");
    sel.style.minWidth = "140px";

    wrap.appendChild(label);
    wrap.appendChild(sel);
    if(submitRow) host.insertBefore(wrap, submitRow); else host.appendChild(wrap);

    function needsSeconds(inp){ return /^\d{2}:\d{2}:\d{2}$/.test((inp.value||"").trim()); }
    function pad(n){ return (n<10?'0':'')+n; }
    function toHM(d){ return pad(d.getHours())+":"+pad(d.getMinutes()); }
    function addMinutes(d, m){ return new Date(d.getTime()+m*60000); }
    function roundUp15(d){ var step=15*60000,t=d.getTime(); return new Date(Math.ceil(t/step)*step); }

    (function fill(){
      var times = [];
      try { times = JSON.parse(timeStart.dataset.times || "[]"); } catch(e){}
      var dayStr = dateStart.value || timeStart.dataset.day || "";
      var durMin = parseInt(timeStart.dataset.duration || "0") || 0;
      var wantSeconds = needsSeconds(timeStart);

      // אם זה היום – מתחילים מהשעה הפנויה הקרובה (מעוגל לרבע שעה)
      try{
        var today = new Date(); today.setHours(0,0,0,0);
        var formDay = new Date(dayStr+"T00:00:00");
        if (today.getTime() === formDay.getTime()){
          var nowHM = toHM(roundUp15(new Date()));
          times = times.filter(function(t){ return t >= nowHM; });
        }
      }catch(e){}

      // --- כאן ההוספה של ה-placeholder ושל ברירת המחדל ---
      sel.innerHTML = "";

      var optPlaceholder = document.createElement("option");
      optPlaceholder.value = "";             // נשארת אופציה חוקית
      optPlaceholder.textContent = "------"; // הטקסט שביקשת
      sel.appendChild(optPlaceholder);

      times.forEach(function(t){
        var opt = document.createElement("option");
        opt.value = t;
        opt.textContent = t;
        sel.appendChild(opt);
      });

      sel.value = ""; // ברירת מחדל: ה-placeholder מוצג
      // -----------------------------------------------------

      // שינוי שעה → עדכון start/end (אם בחרו זמן אמיתי)
      sel.addEventListener("change", function(){
        var t = sel.value || "";
        if (!t) return; // אם נבחר "------" לא עושים כלום

        var tWith = wantSeconds ? (t + ":00") : t;

        // עדכון start_dt
        timeStart.value = tWith;

        // חישוב end_dt בהתאם למשך הפעילות (בלי ההפסקה)
        var hh = parseInt(t.split(":")[0]||"0"), mm = parseInt(t.split(":")[1]||"0");
        var startDate = new Date(dayStr+"T00:00:00"); startDate.setHours(hh, mm, 0, 0);
        var endDate = addMinutes(startDate, durMin);

        dateEnd.value = dayStr;
        timeEnd.value = toHM(endDate) + (wantSeconds ? ":00" : "");
      });
    })();
  });
})();
</script>

""")

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    exclude = ("feedback_sms_sent_at", "feedback_token")
    actions = [delete_selected]
    form = BookingAdminForm
    list_display  = ("id", "activity", "start_dt", "end_dt",
                     "customer_name", "customer_phone", "status", "payment_ref", "total_price", "participants", "created_at")
    list_filter   = ("activity", "status","created_at", "start_dt")
    search_fields = ("customer_name", "customer_phone", "customer_email", "payment_ref")
    readonly_fields = ("total_price", "payment_method", "payment_ref", "status")
    inlines = []

    def has_delete_permission(self, request, obj=None):
        return False

    @require_POST
    def hold_api(self, request):
        # מקבלים date,start_time,minutes (+variant/name אם תרצי)
        date_s = (request.POST.get("date") or "").strip()
        start_s = (request.POST.get("start_time") or "").strip()
        minutes_s = (request.POST.get("minutes") or "").strip()

        try:
            d = datetime.fromisoformat(date_s).date()
            t = datetime.strptime(start_s, "%H:%M").time()
            minutes_real = int(minutes_s)
        except Exception:
            return JsonResponse({"ok": False, "reason": "bad_params"}, status=400)

        start_dt = datetime.combine(d, t)

        # שימי לב: אנחנו תופסים גם buffer אם minutes>30 כמו במערכת שלך
        minutes_total = minutes_real + (15 if minutes_real > 30 else 0)

        token = request.session.get("admin_hold_token")
        if not token:
            token = get_random_string(40)
            request.session["admin_hold_token"] = token
            request.session.modified = True

        res = try_hold_chain(
            token=token,
            user=request.user,
            date=d,
            start_dt=start_dt,
            minutes_total_for_hold=minutes_total,
            include_buffer_if_gt30=True,
            ttl_minutes=15,
        )
        if not res.ok:
            return JsonResponse({"ok": False, "reason": res.reason}, status=409)

        return JsonResponse({"ok": True, "token": token, "ttl": 15})

    @require_POST
    def release_hold_api(self, request):
        token = request.session.get("admin_hold_token") or (request.POST.get("token") or "")
        if token:
            release_hold(token)
        # לא חייבים למחוק מה-session, אבל עדיף:
        request.session.pop("admin_hold_token", None)
        return JsonResponse({"ok": True})

    def save_model(self, request, obj, form, change):
        if change and obj.pk:
            old = Booking.objects.get(pk=obj.pk)

            # ⬅️ כאן "שומרים את הישן" לזיכרון (משתנים מקומיים)
            old_start_dt = old.start_dt
            old_end_dt = old.end_dt

            new_start = form.cleaned_data.get("start_dt") or old.start_dt
            if new_start != old.start_dt:
                delta = old.end_dt - old.start_dt
                ok, err = self._move_booking(old, new_start)
                if not ok:
                    messages.error(request, err or "שגיאה בשינוי שעה.")
                    obj.start_dt, obj.end_dt = old.start_dt, old.end_dt
                else:
                    obj.start_dt = new_start
                    obj.end_dt = new_start + delta

                    # ⬅️ משתמשים בערכים הישנים כדי לשלוח “ישן/חדש”
                    try:
                        notify_time_change_booking(request, obj, old_start_dt, old_end_dt)
                    except Exception:
                        pass

        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        send_booking_deleted_email(obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            send_booking_deleted_email(obj)
        super().delete_queryset(request, queryset)

    def _move_booking(self, booking, new_start_dt):
        """
        מזיז את ההזמנה לזמן התחלה חדש:
        - בודק רצף סלוטים פנוי למשך הפעילות
        - אם משך>30 ד' מוסיף סלוט הפסקה של 15 ד' אחרי הפעילות (אם פנוי)
        - משחרר סלוטים ישנים ותופס חדשים
        """
        from django.db import transaction
        from datetime import timedelta

        minutes = int((booking.end_dt - booking.start_dt).total_seconds() // 60)
        day = new_start_dt.date()

        # כמה סלוטים (15 דק') נדרשים לפעילות
        slot_cnt = max(1, (minutes + 14) // 15)
        need_times = [(new_start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_cnt)]
        paid = str(booking.status).strip().lower() in {"paid", "שולם"}

        try:
            with transaction.atomic():
                # מוודאים רצף פנוי
                appts = list(
                    Appointment.objects.select_for_update()
                    .filter(date=day, time__in=need_times).order_by("time")
                )
                if len(appts) != slot_cnt:
                    return False, "אין רצף סלוטים פנוי."

                for a in appts:
                    # מותר להשתמש בכל סלוט ששייך לאותה הזמנה (גם אם הוא הפסקה),
                    # אסור רק אם הוא שייך להזמנה אחרת.
                    if (a.is_booked or a.is_break) and a.booking_id != booking.id:
                        return False, "סלוט תפוס."

                # משחררים את הסלוטים הישנים של ההזמנה (כולל הפסקות)
                for a in Appointment.objects.select_for_update().filter(booking=booking):
                    a.booking = None
                    a.is_booked = False
                    a.is_paid = False
                    a.is_break = False
                    a.payment_reference = ""
                    # אם אצלך FK activity הוא nullable – אפשר גם לנקות:
                    try:
                        a.activity = None
                        upd = ["booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"]
                    except Exception:
                        upd = ["booking", "is_booked", "is_paid", "is_break", "payment_reference"]
                    # ואם יש M2M activities – לנקות:
                    if hasattr(a, "activities"):
                        a.save(update_fields=upd)  # לשמור לפני clear על ה-M2M
                        a.activities.clear()
                        continue

                    a.save(update_fields=upd)

                # תופסים את הסלוטים החדשים
                for a in appts:
                    a.booking = booking
                    a.is_booked = True
                    a.is_paid = paid
                    a.is_break = False
                    a.payment_reference = booking.payment_ref or ""
                    if a.activity_id != booking.activity_id:
                        a.activity = booking.activity
                    a.save(
                        update_fields=["booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"])
                    if hasattr(a, "activities"):
                        a.activities.add(booking.activity)

                # הפסקה של 15 ד' אחרי פעילות אם משך>30 (רק אם פנוי)
                if minutes > 30:
                    buf_time = (new_start_dt + timedelta(minutes=15 * slot_cnt)).time()
                    extra = Appointment.objects.select_for_update().filter(date=day, time=buf_time).first()
                    if extra and not (extra.is_booked and extra.booking_id != booking.id):
                        extra.booking = booking
                        extra.is_booked = True
                        extra.is_break = True
                        extra.is_paid = False
                        extra.payment_reference = booking.payment_ref or ""
                        if extra.activity_id != booking.activity_id:
                            extra.activity = booking.activity
                        extra.save(update_fields=["booking", "is_booked", "is_break", "is_paid", "payment_reference",
                                                  "activity"])

                # עדכון זמני ההזמנה עצמם
                booking.start_dt = new_start_dt
                booking.end_dt = new_start_dt + timedelta(minutes=minutes)
                booking.save(update_fields=["start_dt", "end_dt"])

            return True, None
        except Exception as e:
            return False, f"שגיאה: {e}"

    def has_add_permission(self, request):
        return True

    def add_view(self, request, form_url="", extra_context=None):
        return redirect(reverse("admin:homePage_appointment_book"))

    def get_urls(self):
        urls = super().get_urls()
        extra = [
            # טופס הוויזארד + AJAX לזמני התחלה (אותו URL, עם ?ajax=times)
            path("book/", self.admin_site.admin_view(self.book_wizard), name="homePage_appointment_book"),
            path("pay/", self.admin_site.admin_view(admin_pay_stub), name="homePage_admin_pay_stub"),
            path("hold/", self.admin_site.admin_view(self.hold_api), name="homePage_appointment_hold"),
            path("hold/release/", self.admin_site.admin_view(self.release_hold_api), name="homePage_appointment_hold_release"),
        ]
        return extra + urls

    def book_wizard(self, request):
        """
        GET (ללא ajax): מציג את טופס יצירת ההזמנה הידנית.
        GET (?ajax=times): מחזיר JSON של שעות התחלה פנויות לתאריך/אורך/פעילות.
        GET (?ajax=quote): מחזיר הצעת מחיר לפי variant מתאים.
        POST: יוצר הזמנה, תופס סלוטים, נותן payment_ref ייחודי, מחשב מחיר ושולח מייל.
        """
        HIDE_RE = re.compile(r'^\s*שיעורי\s*רכיבה\s*/\s*טיפולית\s*$', re.U)
        # ---- AJAX: זמני התחלה פנויות ----
        if request.method == "GET" and request.GET.get("ajax") == "times":
            name = (request.GET.get("name") or "").strip()
            minutes_s = (request.GET.get("minutes") or "").strip()
            date_s = (request.GET.get("date") or "").strip()
            variant = (request.GET.get("variant") or "").strip().lower()  # 'day'|'night'|'sunrise'|''

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

            v = variant if (name == "רכיבה זוגית" and variant in ("day", "night", "sunrise")) else None
            times = find_free_start_times(picked_date, minutes, name, variant=v)
            return JsonResponse({"times": times})

        # ---- AJAX: הצעת מחיר ----
        elif request.method == "GET" and request.GET.get("ajax") == "quote":

            name = (request.GET.get("name") or "").strip()
            minutes = int((request.GET.get("minutes") or "0").strip() or 0)
            variant = (request.GET.get("variant") or "").strip().lower()
            participants = int((request.GET.get("participants") or "1").strip() or 1)

            if not (name and minutes > 0 and participants > 0):
                return JsonResponse({"ok": False, "reason": "bad_params"})

            # בחירת Activity מדויקת לפי variant ו-activity_type
            def _qs_for_couple_day():
                # "יום" = activity_type ריק/None/לא מכיל לילה/זריחה/שקיעה
                return Q(activity_type__isnull=True) | Q(activity_type__exact="") | (
                    ~Q(activity_type__icontains="night")
                    & ~Q(activity_type__icontains="לילה")
                    & ~Q(activity_type__icontains="sunrise")
                    & ~Q(activity_type__icontains="זריחה")
                    & ~Q(activity_type__icontains="שקיעה")
                    & ~Q(activity_type__icontains="sunset")
                    & ~Q(activity_type__icontains="picnic")
                    & ~Q(activity_type__icontains="פיקניק")
                )

            if name == "רכיבה זוגית":
                qs = Activity.objects.filter(name="רכיבה זוגית", duration_minutes=minutes)
                if variant == "night":
                    qs = qs.filter(Q(activity_type__icontains="night") | Q(activity_type__icontains="לילה"))
                elif variant in ("sunrise", "sunset"):
                    qs = qs.filter(
                        Q(activity_type__icontains="sunrise")
                        | Q(activity_type__icontains="זריחה")
                        | Q(activity_type__icontains="שקיעה")
                        | Q(activity_type__icontains="sunset")
                    )
                else:  # day
                    qs = qs.filter(_qs_for_couple_day())
                act = qs.first() or Activity.objects.filter(name=name, duration_minutes=minutes).first()
            else:
                act = Activity.objects.filter(name=name, duration_minutes=minutes).first()

            if not act or act.price is None:
                return JsonResponse({"ok": False, "reason": "no_price"})

            unit = Decimal(act.price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # כמו ב-POST: בזוגית/כרכרה המחיר הוא פר-הזמנה (לא כפול משתתפים)
            if act.name == "טיול כרכרה":
                total = unit
                breakdown = ""
            else:
                total = (unit * Decimal(participants)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                breakdown = f"{participants}×{_fmt_ils(unit)}"

            t = (act.activity_type or "").strip().lower() if act else ""
            allow_wine = ("picnic" in t)
            return JsonResponse({
                "ok": True,
                "unit": str(unit),
                "participants": participants,
                "total": str(total),
                "total_display": _fmt_ils(total),
                "breakdown_display": breakdown,
                "allow_wine": allow_wine,
            })

        # ---- AJAX: HOLD זמני של תור ----
        elif request.method == "GET" and request.GET.get("ajax") == "hold":


            name = (request.GET.get("name") or "").strip()
            minutes = int((request.GET.get("minutes") or "0").strip() or 0)
            date_s = (request.GET.get("date") or "").strip()
            start_s = (request.GET.get("start_time") or "").strip()

            if not (name and minutes and date_s and start_s):
                return JsonResponse({"ok": False, "msg": "חסרים נתונים."})

            try:
                d = datetime.fromisoformat(date_s).date()
                t = datetime.strptime(start_s, "%H:%M").time()
            except Exception:
                return JsonResponse({"ok": False, "msg": "תאריך/שעה לא תקינים."})

            start_dt = datetime.combine(d, t)
            slot_count = max(1, (minutes + 14) // 15)
            times_needed = [
                (start_dt + timedelta(minutes=15 * i)).time()
                for i in range(slot_count)
            ]

            now = timezone.now()
            hold_token = str(uuid.uuid4())
            hold_until = now + timedelta(minutes=10)  # נעילה ל־10 דקות

            with transaction.atomic():

                qs = Appointment.objects.select_for_update().filter(
                    date=d,
                    time__in=times_needed
                )

                # בדיקה אם אחד הסלוטים כבר תפוס או מוחזק
                conflict = qs.filter(
                    Q(is_booked=True) |
                    (Q(hold_until__isnull=False) & Q(hold_until__gt=now))
                ).exists()

                if conflict:
                    return JsonResponse({
                        "ok": False,
                        "msg": "התור נתפס הרגע. בחרי שעה אחרת."
                    })

                # נעילה זמנית
                qs.update(
                    hold_until=hold_until,
                    hold_token=hold_token,
                    hold_created_at=now
                )

            return JsonResponse({
                "ok": True,
                "token": hold_token
            })

        # ---- GET רגיל: תצוגת הוויזארד ----
        elif request.method == "GET":
            all_names = distinct_activity_names()
            names = [n for n in all_names if not HIDE_RE.match((n or "").strip())]

            durations_map = {n: durations_for_name(n) for n in names}

            # בנייה חסינה של אורכי "רכיבה זוגית" לפי activity_type בפועל
            couple_qs = Activity.objects.filter(name="רכיבה זוגית").values("duration_minutes", "activity_type")
            couple_day, couple_night, couple_sunrise, couple_picnic = set(), set(), set(), set()

            for row in couple_qs:
                m = int(row["duration_minutes"] or 0)
                t = (row["activity_type"] or "").strip().lower()

                if not m:
                    continue

                if ("night" in t) or ("לילה" in t):
                    couple_night.add(m)

                elif ("sunrise" in t) or ("זריחה" in t) or ("שקיעה" in t) or ("sunset" in t):
                    couple_sunrise.add(m)

                elif ("picnic" in t) or ("פיקניק" in t):
                    couple_picnic.add(m)

                else:
                    couple_day.add(m)

            fallback_all = set(durations_map.get("רכיבה זוגית", []))
            if not couple_day:
                couple_day = set(fallback_all)

            durations_couple = {
                "day": sorted(couple_day),
                "night": sorted(couple_night),
                "sunrise": sorted(couple_sunrise),
                "picnic": sorted(couple_picnic),
            }

            ctx = {
                "opts": self.model._meta,
                "media": self.media,
                "app_label": self.model._meta.app_label,
                "activity_names": names,
                "durations_map_json": json.dumps(durations_map, ensure_ascii=False),
                "durations_couple_json": json.dumps(durations_couple, ensure_ascii=False),
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
        activity_id = (request.POST.get("activity_id") or "").strip()
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
        couple_variant = (request.POST.get("couple_variant") or "").strip().lower()  # 'day'|'night'|'sunrise'

        # סכום ידני (אופציונלי)
        manual_total_s = (request.POST.get("manual_total") or "").strip()
        manual_total = None

        if name == "רכיבה זוגית":
            participants = 2

        try:
            minutes_override = int(request.POST.get("duration_override") or 0)
        except ValueError:
            minutes_override = 0
        if minutes_override > 0:
            minutes = minutes_override

        try:
            if manual_total_s != "":
                manual_total = Decimal(manual_total_s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if manual_total < 0:
                    manual_total = None
        except Exception:
            manual_total = None

        # הסכמה לתנאים/פרטיות
        sid = normalize_phone_il(phone)
        has_consent = has_consent_by_phone(sid)
        if (not has_consent) and (not request.POST.get("accept_terms")):
            messages.error(request, "יש לאשר את תנאי השימוש ומדיניות הפרטיות לפני יצירת הזמנה.")
            return redirect(reverse("admin:homePage_appointment_book"))

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

        # שדות מיוחדים
        wine = (request.POST.get("wine") or "").strip().lower()
        if wine not in ("white", "red", "none", ""):
            wine = ""
        horse_color = (request.POST.get("horse_color") or "").strip().lower()

        if not (name and minutes and date_str and start_str and first_name and last_name and phone and email):
            messages.error(request, "חסרים שדות חובה.")
            return redirect(reverse("admin:homePage_appointment_book"))

        if name == "צילומים" and not horse_color:
            messages.error(request, "בחרי צבע סוס לצילומים.")
            return redirect(reverse("admin:homePage_appointment_book"))

        # בחירת Activity לפי variant ו-activity_type (כמו ב-quote)
        def _qs_for_couple_day():
            return Q(activity_type__isnull=True) | Q(activity_type__exact="") | (
                ~Q(activity_type__icontains="night")
                & ~Q(activity_type__icontains="לילה")
                & ~Q(activity_type__icontains="sunrise")
                & ~Q(activity_type__icontains="זריחה")
                & ~Q(activity_type__icontains="שקיעה")
                & ~Q(activity_type__icontains="sunset")
                & ~Q(activity_type__icontains="picnic")
                & ~Q(activity_type__icontains="פיקניק")
            )

        if name == "רכיבה זוגית":
            base_qs = Activity.objects.filter(name="רכיבה זוגית")
            if couple_variant == "night":
              base_qs = base_qs.filter(Q(activity_type__icontains="night") | Q(activity_type__icontains="לילה"))

            elif couple_variant in ("sunrise", "sunset"):
              base_qs = base_qs.filter(
                Q(activity_type__icontains="sunrise")
                | Q(activity_type__icontains="זריחה")
                | Q(activity_type__icontains="שקיעה")
                | Q(activity_type__icontains="sunset")
              )

            elif couple_variant == "picnic":
              base_qs = base_qs.filter(Q(activity_type__icontains="picnic") | Q(activity_type__icontains="פיקניק"))

            else:  # day
              base_qs = base_qs.filter(_qs_for_couple_day())

            # נסה התאמה מלאה (שם+משך), ואם אין — קח פעילות בסיסית לאותו variant
            activity = (
                base_qs.filter(duration_minutes=minutes).order_by("id").first()
                or base_qs.order_by("duration_minutes", "id").first()
            )
        else:
            base_qs = Activity.objects.filter(name=name)
            activity = (
                base_qs.filter(duration_minutes=minutes).order_by("id").first()
                or base_qs.order_by("duration_minutes", "id").first()
            )

        if not activity:
            messages.error(request, "לא נמצאה פעילות מתאימה למשך/סוג שנבחר.")
            return redirect(reverse("admin:homePage_appointment_book"))

        # פרסינג של תאריך/שעה
        try:
            d = datetime.fromisoformat(date_str).date()
            t = datetime.strptime(start_str, "%H:%M").time()
        except Exception:
            messages.error(request, "תאריך/שעה לא תקינים.")
            return redirect(reverse("admin:homePage_appointment_book"))

        start_dt = datetime.combine(d, t)
        slot_count = max(1, (minutes + 14) // 15)
        times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

        # חייב להיות hold_token מה-GET ajax=hold (ה-JS שם אותו בשדה hidden בשם hold_token)
        hold_token = (request.POST.get("hold_token") or "").strip()
        if not hold_token:
            messages.error(request, "חסרים נתונים: צריך לבחור שעה ולהמתין לתפיסת התור (HOLD).")
            return redirect(reverse("admin:homePage_appointment_book"))

        with transaction.atomic():
            now = timezone.now()

            qs = Appointment.objects.select_for_update().filter(
                date=d,
                time__in=times_needed,
            )

            # חייבים שכל הסלוטים קיימים
            if qs.count() != len(times_needed):
                messages.error(request, "אין רצף סלוטים פנוי.")
                return redirect(reverse("admin:homePage_appointment_book"))

            # חייבים שכל הסלוטים מוחזקים ע"י אותו hold_token ועדיין לא פג תוקף
            bad = qs.filter(
                Q(is_booked=True) |
                Q(hold_until__isnull=True) |
                Q(hold_until__lte=now) |
                ~Q(hold_token=hold_token)
            ).exists()

            if bad:
                messages.error(request, "התור נתפס/פג תוקף. בחרי שעה אחרת ובצעי HOLD מחדש.")
                return redirect(reverse("admin:homePage_appointment_book"))

        t = (activity.activity_type or "").strip().lower()
        allow_wine = ("picnic" in t)

        if not allow_wine:
            wine = ""

        # יצירה ותפיסה אטומית
        # --- במקום כל ה-with transaction.atomic() והיצירה בפועל ---

        # טקסט פרטים (כמו שהיה)
        details_txt = ""
        if allow_wine and wine:
            details_txt = "יין: " + {"white": "יין לבן", "red": "יין אדום", "none": "בלי יין"}.get(wine, "")
        if name == "צילומים" and horse_color:
            color_map = {"white": "סוס לבן", "brown": "סוס חום"}
            details_txt = (
                              details_txt + " | " if details_txt else "") + f"צבע סוס: {color_map.get(horse_color, horse_color)}"

        # חישוב מחיר (כמו שהיה אצלך)
        unit_price = activity.price
        total_price = None
        if unit_price is not None:
            if activity.name in ("טיול כרכרה", "רכיבה זוגית"):
                total_price = unit_price
            else:
                total_price = unit_price * max(1, participants)
        if manual_total is not None:
            total_price = manual_total

        # שומר טיוטה ב-session
        draft = {
            "activity_id": activity.id,
            "minutes": minutes,
            "date": d.isoformat(),
            "start": start_dt.strftime("%H:%M:%S"),
            "participants": participants,
            "customer": {
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "email": email,
            },
            "details": details_txt or "נקבע באדמין",
            "total_price": str(total_price) if total_price is not None else None,
            "manual_total": str(manual_total) if manual_total is not None else None,
            "couple_variant": couple_variant,
            "wine": wine,
            "horse_color": horse_color,
        }
        request.session["booking_draft"] = draft
        request.session.modified = True

        messages.success(request, "פרטי ההזמנה מוכנים. המשיכי לתשלום.")
        return redirect(f"{reverse('admin:homePage_admin_pay_stub')}?kind=draft")

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
        # נדרש סלוט פתיחה יחיד
        if queryset.count() != 1:
            self.message_user(request, "בחרי סלוט התחלה אחד בלבד.", level=messages.ERROR)
            return

        base_slot: Appointment = queryset.first()

        # פרמטרים מהטופס
        try:
            duration = int(request.POST.get("duration_minutes") or 60)
            participants = int(request.POST.get("participants") or 1)
        except ValueError:
            self.message_user(request, "משך/משתתפים לא תקינים.", level=messages.ERROR)
            return

        activity_id = request.POST.get("activity") or None
        activity = Activity.objects.filter(pk=activity_id).first() if activity_id else None
        capture_buffer = bool(request.POST.get("capture_buffer"))
        mark_paid = str(request.POST.get("mark_paid", "")).lower() in {"1", "true", "on", "yes"}

        # אם לא נבחרה פעילות – ננסה מהסלוט עצמו/ה־M2M
        if not activity:
            activity = getattr(base_slot, "activity", None)
            if not activity and hasattr(base_slot, "activities"):
                activity = base_slot.activities.first()
        if not activity:
            self.message_user(request, "לא נמצאה פעילות מתאימה לסלוט.", level=messages.ERROR)
            return

        # האם לדחות יצירה/תפיסה עד מסך התשלום?
        # דוחים אם: (א) הפעילות טיפולית/שיעור   או   (ב) המשתמש לא סימן mark_paid
        defer_now = _defer_capture_for_activity(activity) or (not mark_paid)

        if defer_now:
            # --- מצב טיוטה בלבד: לא נוצרת רשומה בבסיס הנתונים, לא נתפסים סלוטים ---
            unit_price = getattr(activity, "price", None)
            total_price = None
            if unit_price is not None:
                if activity.name in ("טיול כרכרה", "רכיבה זוגית"):
                    total_price = unit_price
                else:
                    total_price = unit_price * max(1, participants)

            start_dt = datetime.combine(base_slot.date, base_slot.time)

            # פיצול שם הלקוח מהסלוט אם יש
            full_name = (base_slot.customer_name or "").strip()
            fn, ln = (full_name.split(" ", 1) + [""])[:2]

            draft = {
                "activity_id": activity.id,
                "minutes": duration,
                "date": base_slot.date.isoformat(),
                "start": start_dt.strftime("%H:%M:%S"),
                "participants": max(1, participants),
                "customer": {
                    "first_name": fn,
                    "last_name": ln,
                    "phone": (base_slot.customer_phone or "").strip(),
                    "email": "",
                },
                "details": "נקבע באדמין",
                "total_price": str(total_price) if total_price is not None else None,
                "manual_total": None,
                "couple_variant": "",
                "wine": "",
                "horse_color": "",
            }
            request.session["booking_draft"] = draft
            request.session.modified = True

            self.message_user(
                request,
                "נוצרה טיוטה בלבד. לא נוצרה הזמנה בבסיס הנתונים והסלוטים לא נתפסו. המשיכי לתשלום.",
                level=messages.SUCCESS,
            )
            return redirect(f"{reverse('admin:homePage_admin_pay_stub')}?kind=draft")

        # --- מסלול מיידי (למי שאינו טיפולית/שיעור ובמקרה שסומן mark_paid) ---
        try:
            with transaction.atomic():
                base = Appointment.objects.select_for_update().get(pk=base_slot.pk)
                if base.is_booked or base.is_break:
                    raise ValueError("סלוט ההתחלה תפוס או מסומן כהפסקה.")

                start_dt = datetime.combine(base.date, base.time)
                slot_count = max(1, (duration + 14) // 15)
                times_needed = [(start_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

                appts = list(
                    Appointment.objects.select_for_update().filter(
                        date=base.date, time__in=times_needed, is_booked=False, is_break=False
                    ).order_by("time")
                )
                if len(appts) != slot_count:
                    raise ValueError("אין רצף סלוטים פנוי לכל המשך שנבחר.")

                booking = Booking.objects.create(
                    activity=activity,
                    customer_name=(base.customer_name or "").strip(),
                    customer_phone=(base.customer_phone or "").strip(),
                    customer_email="",
                    participants=max(1, participants),
                    total_price=None,
                    payment_method="admin",
                    status="pending",  # יישאר ממתין עד התשלום במסך ה-Pay
                    start_dt=start_dt,
                    end_dt=start_dt + timedelta(minutes=duration),
                    details="נקבע באדמין",
                )

                # תפיסת סלוטים (ללא תשלום)
                for a in appts:
                    a.booking = booking
                    a.is_booked = True
                    a.is_paid = False
                    a.is_break = False
                    a.payment_reference = ""  # אסמכתא רק לאחר תשלום
                    if a.activity_id != activity.id:
                        a.activity = activity
                    a.save(update_fields=[
                        "booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"
                    ])
                    if hasattr(a, "activities"):
                        a.activities.add(activity)

                # הפסקת 15 דקות אם ביקשו ומשך>30 (רק אם פנוי)
                if capture_buffer and duration > 30:
                    extra_start = start_dt + timedelta(minutes=15 * slot_count)
                    extra = Appointment.objects.select_for_update().filter(
                        date=base.date, time=extra_start.time(), is_booked=False, is_break=False
                    ).first()
                    if extra:
                        extra.booking = booking
                        extra.is_booked = True
                        extra.is_paid = False
                        extra.is_break = True
                        extra.payment_reference = ""
                        if extra.activity_id != activity.id:
                            extra.activity = activity
                        extra.save(update_fields=[
                            "booking", "is_booked", "is_paid", "is_break", "payment_reference", "activity"
                        ])

                # חישוב מחיר להזמנה (אופציונלי)
                minutes_len = int((booking.end_dt - booking.start_dt).total_seconds() // 60)
                unit_price = (
                                 Activity.objects
                                 .filter(name=booking.activity.name, duration_minutes=minutes_len)
                                 .exclude(price__isnull=True)
                                 .values_list("price", flat=True)
                                 .first()
                             ) or booking.activity.price

                if unit_price is not None:
                    total_price = (
                        unit_price if booking.activity.name in ("טיול כרכרה", "רכיבה זוגית")
                        else unit_price * max(1, booking.participants)
                    )
                    booking.total_price = total_price
                    try:
                        booking.save(update_fields=["total_price"])
                    except Exception:
                        booking.save()

        except Exception as e:
            self.message_user(request, f"שגיאה: {e}", level=messages.ERROR)
            return

        self.message_user(request, f"הזמנה #{booking.id} נוצרה. המשיכי לתשלום.", level=messages.SUCCESS)
        return redirect(f"{reverse('admin:homePage_admin_pay_stub')}?kind=booking&id={booking.id}")

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
    actions = ("approve_refund_and_cancel",)

    #  מבטלים מחיקה של בקשות ביטול (שלא ייעלמו תיעודים)
    def has_delete_permission(self, request, obj=None):
        return False

    def release_booking_slots(self, booking):
        """
        משחרר את כל ה-Appointments ששייכים להזמנה:
        - booking=NULL
        - is_booked=False
        - is_break=False
        - is_paid=False
        - payment_reference=""
        - מנקה M2M activities אם קיים
        """
        from .models import Appointment

        with transaction.atomic():
            qs = Appointment.objects.select_for_update().filter(booking=booking)
            for a in qs:
                a.booking = None
                a.is_booked = False
                a.is_break = False
                a.is_paid = False
                a.payment_reference = ""
                # אם יש FK activity ורוצים לנקות אותו:
                try:
                    a.activity = None
                    upd = ["booking", "is_booked", "is_break", "is_paid", "payment_reference", "activity"]
                except Exception:
                    upd = ["booking", "is_booked", "is_break", "is_paid", "payment_reference"]

                a.save(update_fields=upd)

                if hasattr(a, "activities"):
                    a.activities.clear()


    @admin.action(description="בצע החזר + בטל הזמנה (שחרור סלוטים + מייל)")
    def approve_refund_and_cancel(self, request, queryset):
        done = 0
        for cr in queryset.select_related("booking"):
            booking = cr.booking

            if not booking and cr.order_id:
                booking = Booking.objects.filter(payment_ref__iexact=cr.order_id.strip()).first()
                if booking:
                    cr.booking = booking

            if not booking:
                self.message_user(request, f"בקשה #{cr.id}: לא נמצאה הזמנה לשיוך.", level=messages.ERROR)
                continue

            try:
                with transaction.atomic():
                    # 1) שחרור סלוטים
                    self.release_booking_slots(booking)

                    # 2) עדכון הזמנה (לא למחוק!)
                    now = timezone.now()
                    note = f"\n[{now:%d.%m.%Y %H:%M}] בוטל + בוצע החזר כספי (טופל דרך בקשת ביטול #{cr.id})."
                    booking.details = (booking.details or "") + note  # אם אצלך זה notes, החליפי לשם השדה
                    booking.status = "refunded"  # או "cancelled" לפי מה שיש אצלך
                    booking.save(update_fields=["details", "status"])

                    # 3) עדכון בקשת ביטול
                    cr.status = "approved"
                    cr.handled_at = now
                    cr.handled_by = request.user
                    cr.save()

                # 4) שליחת מייל ללקוח (אחרי ה-commit)
                try:
                    send_booking_deleted_email(booking)  # את יכולה לשנות שם לפונקציה "cancelled/refunded"
                except Exception:
                    pass

                done += 1

            except Exception as e:
                self.message_user(request, f"שגיאה בבקשה #{cr.id}: {e}", level=messages.ERROR)

        self.message_user(request, f"טופלו {done} בקשות.", level=messages.SUCCESS)


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

@admin.register(MonthlySummary)
class MonthlySummaryAdmin(admin.ModelAdmin):
    # לא רוצים "הוספה/מחיקה/עדכון" – כרטיס דוח בלבד
    def has_add_permission(self, request): return False
    def has_delete_permission(self, request, obj=None): return False
    def has_change_permission(self, request, obj=None): return False

    def changelist_view(self, request, extra_context=None):
        """
        סיכום חודשי מאוחד:
        - Booking: ספירה לכל הסטטוסים; הכנסות לפי status=paid/שולם; פירוק לפי payment_method
        - TreatmentSession: ספירה לפי lesson_type; הכנסות למי שיש payment_ref
          (override מתוך details אם קיים; אחרת לפי משך עם _suggest_amount_for_session)
        """
        from decimal import Decimal
        from collections import defaultdict
        import re

        tz = ZoneInfo("Asia/Jerusalem")
        now_local = timezone.now().astimezone(tz)
        y = int(request.GET.get("year") or now_local.year)
        m = int(request.GET.get("month") or now_local.month)
        if not (1 <= m <= 12):
            m = now_local.month; y = now_local.year

        start = datetime(y, m, 1, 0, 0, tzinfo=tz)
        next_start = datetime(y + (1 if m == 12 else 0), (1 if m == 12 else m + 1), 1, 0, 0, tzinfo=tz)
        is_current_month = (y == now_local.year and m == now_local.month)
        end = now_local if is_current_month else next_start

        # ===== BOOKINGS =====
        base_qs = Booking.objects.filter(start_dt__gte=start, start_dt__lt=end)
        paid_qs = base_qs.filter(Q(status__iexact="paid") | Q(status__iexact="שולם"))

        counts_map = defaultdict(int)
        for row in base_qs.values("activity__name").annotate(total=Count("id")):
            counts_map[row["activity__name"] or "—"] += int(row["total"] or 0)

        rev_map = {}
        DEC0 = Value(0, output_field=DecimalField(max_digits=12, decimal_places=2))

        for r in (paid_qs.values("activity__name")
                          .annotate(revenue=Coalesce(Sum("total_price"), DEC0), count_paid=Count("id"))):
            rev_map[r["activity__name"] or "—"] = {
                "revenue": Decimal(r["revenue"] or 0),
                "count_paid": int(r["count_paid"] or 0),
            }

        payment_rows = []
        for row in (paid_qs.values("payment_method")
                          .annotate(revenue=Coalesce(Sum("total_price"), DEC0), count=Count("id"))
                          .order_by("-revenue")):
            revenue = Decimal(row["revenue"] or 0)
            payment_rows.append({
                "method": (row["payment_method"] or "").strip() or "—",
                "count": int(row["count"] or 0),
                "revenue": revenue,
                "revenue_display": _fmt_ils(revenue),
            })

        # ===== TREATMENT SESSIONS =====
        if is_current_month:
            sess_q = TreatmentSession.objects.filter(
                Q(date__gte=start.date(), date__lt=now_local.date()) |
                Q(date=now_local.date(), start_time__lt=now_local.time())
            )
        else:
            sess_q = TreatmentSession.objects.filter(date__gte=start.date(), date__lt=next_start.date())

        def lesson_type_label(code: str) -> str:
            return dict(TreatmentSession.LESSON_TYPE_CHOICES).get(code or "", "")

        for row in sess_q.values("lesson_type").annotate(total=Count("id")):
            lbl = lesson_type_label(row["lesson_type"]) or "מפגש"
            counts_map[lbl] += int(row["total"] or 0)

        sess_paid_q = sess_q.filter(payment_ref__isnull=False).exclude(payment_ref__exact="")
        ov_re = re.compile(r"מחיר\s+הוגדר\s+ידנית.*?₪\s*([0-9]+(?:\.[0-9]{1,2})?)")

        def _override_from_details(txt: str):
            if not txt: return None
            m_ = ov_re.search(txt)
            if not m_: return None
            try: return Decimal(m_.group(1))
            except Exception: return None

        sess_rev_map = defaultdict(Decimal)
        sess_paid_count_map = defaultdict(int)
        for s in sess_paid_q.iterator():
            lbl = lesson_type_label(getattr(s, "lesson_type", "")) or "מפגש"
            amount = _override_from_details(getattr(s, "details", "")) or _suggest_amount_for_session(s)
            if amount is None:
                continue
            amount = Decimal(amount)
            sess_rev_map[lbl] += amount
            sess_paid_count_map[lbl] += 1

        # ===== MERGE =====
        names = sorted(set(counts_map.keys()) | set(rev_map.keys()) | set(sess_rev_map.keys()))
        activity_rows = []
        total_paid_count = Decimal("0")
        total_revenue = Decimal("0")

        for nm in names:
            paid_count = int((rev_map.get(nm, {}).get("count_paid", 0) or 0)) + int(sess_paid_count_map.get(nm, 0))
            revenue = (rev_map.get(nm, {}).get("revenue", Decimal("0")) or Decimal("0")) + (sess_rev_map.get(nm, Decimal("0")) or Decimal("0"))
            total_paid_count += paid_count
            total_revenue += revenue
            activity_rows.append({
                "name": nm,
                "count_total": int(counts_map.get(nm, 0) or 0),
                "count_paid": paid_count,
                "revenue": revenue,
                "revenue_display": _fmt_ils(revenue),
            })

        activity_rows.sort(key=lambda r: r["revenue"], reverse=True)

        total_bookings = base_qs.count() + sess_q.count()
        sessions_revenue = sum(sess_rev_map.values(), Decimal("0"))

        prev_y, prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
        next_y, next_m = (y, m + 1) if m < 12 else (y + 1, 1)

        def month_name_he(mi: int) -> str:
            return {1:"ינואר",2:"פברואר",3:"מרץ",4:"אפריל",5:"מאי",6:"יוני",7:"יולי",8:"אוגוסט",9:"ספטמבר",10:"אוקטובר",11:"נובמבר",12:"דצמבר"}.get(mi, str(mi))

        ctx = {
            "title": "סיכום חודש",
            "opts": self.model._meta,
            "year": y, "month": m,
            "month_label": f"{month_name_he(m)} {y}",
            "period_start": start, "period_end": end, "is_current_month": is_current_month,
            "activity_rows": activity_rows,
            "payment_rows": payment_rows,  # Booking בלבד
            "summary": {
                "total_bookings": total_bookings,
                "total_paid_bookings": int(total_paid_count),
                "total_revenue": total_revenue,
                "total_revenue_display": _fmt_ils(total_revenue),
                "sessions_revenue_display": _fmt_ils(sessions_revenue),
            },
            "nav": {
                "prev_url": f"?year={prev_y}&month={prev_m}",
                "next_url": f"?year={next_y}&month={next_m}",
                "this_month_url": f"?year={now_local.year}&month={now_local.month}",
                "value_for_input": f"{y}-{m:02d}",
            },
        }
        return render(request, "admin/homePage/monthly_summary.html", ctx)

class SlotGeneratorForm(forms.Form):
    MODE_CHOICES = (("full", "יום מלא לפי שעות עבודה"), ("window", "טווח שעות ידני"))

    date = forms.DateField(
        label="תאריך",
        widget=AdminDateWidget(attrs={"autocomplete": "off"}),
    )
    mode = forms.ChoiceField(label="אופן יצירה", choices=MODE_CHOICES, initial="full")

    start_time = forms.TimeField(label="שעת התחלה", widget=AdminTimeWidget, required=False)
    end_time   = forms.TimeField(label="שעת סיום",    widget=AdminTimeWidget, required=False)

    # ⬇️ חדש: בחירה מרובה ל-M2M (אפשר גם ריק)
    activities = forms.ModelMultipleChoiceField(
        label="שיוך לפעילויות (לילה/זריחה)",
        queryset=Activity.objects.none(),   # נטען ב-__init__
        required=False,
        widget=FilteredSelectMultiple("פעילויות", is_stacked=False),
        help_text="ניתן לבחור כמה פעילויות או להשאיר ריק. רק לא לשכוח שבמקרה של בחירה חייב להגדיר שעת התחלה וסוף",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        qs = Activity.objects.all()
        # אם יש לך activity_type – נעדיף אותו:
        try:
            qs = qs.filter(activity_type__in=["night", "sunrise"])
        except Exception:
            qs = Activity.objects.all()

        # גיבוי לפי שם אם אין תוצאות מספקות:
        if not qs.exists():
            qs = Activity.objects.filter(
                Q(name__icontains="לילה")   | Q(name__icontains="night") |
                Q(name__icontains="זריחה") | Q(name__icontains="sunrise")
            )

        self.fields["activities"].queryset = qs.order_by("name")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("mode") == "window":
            st, et = cleaned.get("start_time"), cleaned.get("end_time")
            if not st or not et:
                raise forms.ValidationError("במצב טווח שעות חובה למלא גם התחלה וגם סוף.")
            if et <= st:
                raise forms.ValidationError("שעת הסיום חייבת להיות אחרי שעת ההתחלה.")
        return cleaned



@admin.register(MarketingConsent)
class MarketingConsentAdmin(admin.ModelAdmin):
    # עמודות שמופיעות ברשימה
    list_display = (
        "channel_badge",
        "subject_id",
        "email_display",
        "full_name",
        "version",
        "accepted_at",
        "ip",
        "ua_short",
    )

    # סינונים בצד
    list_filter = ("channel", "version", "accepted_at")

    # חיפוש
    search_fields = ("subject_id", "customer_email", "full_name", "ip", "user_agent")

    # פירורי תאריך בחלק העליון
    date_hierarchy = "accepted_at"

    # שדות לקריאה בלבד בטופס
    readonly_fields = ("accepted_at",)

    # סדר ברירת מחדל
    ordering = ("-accepted_at",)

    # כמה שורות בעמוד
    list_per_page = 50

    # פעולות (Actions) לבחירה מרובות
    actions = ("export_as_csv",)

    # תצוגה ידידותית של הערוץ
    def channel_badge(self, obj):
        label = dict(MarketingConsent.CHANNEL_CHOICES).get(obj.channel, obj.channel)
        color_map = {
            "sms": "#1f7a1f",       # ירוק
            "email": "#1f4b7a",     # כחול
            "whatsapp": "#128C7E",  # ירוק-ווטסאפ
        }
        color = color_map.get(obj.channel, "#555")
        return format_html(
            '<span style="display:inline-block;padding:2px 8px;border-radius:12px;'
            'color:#fff;font-size:12px;background:{}">{}</span>',
            color, label
        )
    channel_badge.short_description = "ערוץ"

    # קיצור של ה-User-Agent כדי שלא ישבור שורות
    def ua_short(self, obj):
        ua = (obj.user_agent or "").strip()
        return (ua[:70] + "…") if len(ua) > 70 else ua
    ua_short.short_description = "User-Agent"

    # מייל כלינק קליקבילי (או מקף אם אין)
    def email_display(self, obj):
        e = (obj.customer_email or "").strip()
        if not e:
            return "—"
        return format_html('<a href="mailto:{}">{}</a>', e, e)
    email_display.short_description = "מייל"

    # נרמול אוטומטי בעת שמירה ידנית מהאדמין
    def save_model(self, request, obj, form, change):
        # נרמול subject_id לפי הערוץ
        if obj.channel == "sms":
            obj.subject_id = normalize_phone_il(obj.subject_id or "")
        elif obj.channel == "email":
            # אם לא הוזן subject_id – נשתמש במייל
            base = (obj.subject_id or obj.customer_email or "")
            obj.subject_id = base.strip().lower()
            # אם אין customer_email אבל יש subject_id – נשלים אותו
            if not (obj.customer_email or "").strip():
                obj.customer_email = obj.subject_id

        # הקפדה על lowercase למייל
        if obj.customer_email:
            obj.customer_email = obj.customer_email.strip().lower()

        super().save_model(request, obj, form, change)

    # יצוא CSV (תואם אקסל – כולל BOM)
    def export_as_csv(self, request, queryset):
        filename = "marketing_consents.csv"
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        # BOM ל-Excel
        response.write("\ufeff")

        writer = csv.writer(response)
        writer.writerow([
            "channel",
            "subject_id",
            "customer_email",
            "full_name",
            "version",
            "accepted_at",
            "ip",
            "user_agent",
        ])
        for obj in queryset.iterator():
            writer.writerow([
                obj.channel,
                obj.subject_id,
                obj.customer_email or "",
                obj.full_name or "",
                obj.version or "",
                obj.accepted_at.strftime("%Y-%m-%d %H:%M:%S") if obj.accepted_at else "",
                obj.ip or "",
                obj.user_agent or "",
            ])
        return response
    export_as_csv.short_description = "ייצוא בחירה ל-CSV"


# ---------- עזרי זמן/חלונות ----------
def _iter_slot_times(date_obj, start_t: dtime, end_t: dtime, minutes: int = MINUTES_PER_SLOT):
    """יוצר זמני התחלה במרווח קבוע. start כולל, end אקסקלוסיבי."""
    step = timedelta(minutes=minutes)
    cur = datetime.combine(date_obj, start_t)
    end_dt = datetime.combine(date_obj, end_t)
    while cur + step <= end_dt:
        yield cur.time()
        cur += step

def _business_windows_for_date(g_date):
    """
    מחזיר רשימת חלונות [(start_time, end_time)] עבור היום הנתון לפי BusinessHours.
    אם קיימות מספר רשומות לאותו יום/עונה – נעבור על כולן.
    ננסה (אם קיים אצלך) לסנן לפי יום בשבוע (days__number=0..6), אחרת ניקח את כל הרשומות לעונה.
    """
    qs = BusinessHours.active_for_date(g_date)
    try:
        wd = g_date.weekday()  # Monday=0 ... Sunday=6
        qs_day = qs.filter(days__number=wd)
        if qs_day.exists():
            qs = qs_day
    except Exception:
        pass

    return [(bh.start_time, bh.end_time) for bh in qs]

MINUTES_PER_SLOT = 15

def _create_slots_for_range(date_obj, start_t, end_t, activities_qs):
    created = skipped = 0

    for t in _iter_slot_times(date_obj, start_t, end_t):
        if Appointment.objects.filter(date=date_obj, time=t).exists():
            skipped += 1
            continue

        new_obj = Appointment.objects.create(
            date=date_obj,
            time=t,
            duration_minutes=MINUTES_PER_SLOT,
            is_booked=False,
            is_break=False,
            activity=None,  # ← לא משתמשים ב-FK הבודד
        )

        # שיוך ל-M2M אם נבחר משהו
        if activities_qs:
            new_obj.activities.add(*activities_qs)

        created += 1

    return created, skipped, 0  # booked_skipped=0 כאן

def _generate_slots_from_form(cleaned):
    date_obj   = cleaned["date"]
    mode       = cleaned["mode"]
    activities = cleaned["activities"]  # ← ה-QuerySet שנבחר בטופס

    totals = dict(created=0, skipped=0, booked_skipped=0)

    if mode == "full":
        windows = _business_windows_for_date(date_obj)
        if not windows:
            return totals, "לא נמצאו שעות עבודה ליום הזה (בדקי BusinessHours והשיוך לימי השבוע)."
        for (st, et) in windows:
            c, s, b = _create_slots_for_range(date_obj, st, et, activities)
            totals["created"] += c
            totals["skipped"] += s
        return totals, None

    # window
    st, et = cleaned["start_time"], cleaned["end_time"]
    c, s, b = _create_slots_for_range(date_obj, st, et, activities)
    totals["created"] += c
    totals["skipped"] += s
    return totals, None

def _weekday_number(py_date) -> int:
    # Python: Monday=0 ... Sunday=6
    return py_date.weekday()


