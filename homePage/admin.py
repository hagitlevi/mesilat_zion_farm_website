from django.contrib import admin
import json
from .models import (
    Activity, Appointment, CustomSchedule, Booking, SiteReview,
    CancellationRequest, TermsConsent, ScheduleBoard, Weekday,
    BusinessHours, ActivityRule, Instructor, TreatmentSession
)

from django import forms
from django.contrib import messages
from django.shortcuts import redirect
from zoneinfo import ZoneInfo
from django.db import transaction
import secrets
from types import SimpleNamespace
from django.db.models import Q
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
from decimal import Decimal , ROUND_HALF_UP
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError
from datetime import datetime
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from datetime import datetime, timedelta
from django import forms
from django.contrib import admin
from django.shortcuts import render
import re
from collections import defaultdict
from homePage.services.ntfy_gateway import send_sms_via_ntfy
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from decimal import Decimal  # אם לא קיים למעלה כבר
import re, sys
from django.template.loader import render_to_string  # אם לא קיים

delete_selected.short_description = "מחיקה"

DETAILS_ONLY_PERM = "homePage.change_details_only"
FULL_CHANGE_PERM  = "homePage.change_treatmentsession"

NIGHT_RE    = r"(night|לילה)"
SUNRISE_RE  = r"(sunrise|זריחה|שקיעה|sunset)"   # כולל "שקיעה"
NON_DAY_RE  = r"(night|לילה|sunrise|זריחה|שקיעה|sunset)"
# סימני bidi מבודדים – הכי יציב ל-SMS/טקסט


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

def _gen_unique_ref_any(digits: int = 8) -> str:
    for _ in range(25):
        cand = "MZ-" + "".join(secrets.choice("0123456789") for _ in range(digits))
        if (not Booking.objects.filter(payment_ref=cand).exists()
                and not TreatmentSession.objects.filter(payment_ref=cand).exists()):
            return cand
    return "MZ-" + timezone.now().strftime("%y%m%d%H%M%S")

def _day_range(base: ddate):
    """7 ימים החל מ-base (כולל)."""
    return [base + timedelta(days=i) for i in range(7)]

def _ltr_text(s: str) -> str:
    # מציג s משמאל-לימין בתוך טקסט RTL (ל־SMS זה פותר היפוך "HH:MM–HH:MM")
    return "\u2066" + s + "\u2069"  # LRI ... PDI

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
        .filter(is_booked=False, is_break=False, date=chosen_date)
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
                if t not in base_free_set:
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
            if buffer_start_dt.time() not in base_free_set:
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

def _normalize_phone_il(phone: str) -> str:
    p = "".join(ch for ch in (phone or "") if ch.isdigit())
    if p.startswith("972") and len(p) >= 11:
        p = "0" + p[3:]
    return p

def _format_session_time_range(date_obj, start_time, end_time) -> str:
    def hhmm(t): return t.strftime("%H:%M") if t else ""
    if date_obj and start_time and end_time:
        rng = f"{hhmm(start_time)}–{hhmm(end_time)}"
        return f"{date_obj:%d.%m.%Y} בשעה {_ltr_text(rng)}"
    if date_obj and start_time:
        return f"{date_obj:%d.%m.%Y} בשעה {_ltr_text(hhmm(start_time))}"
    if date_obj:
        return f"{date_obj:%d.%m.%Y}"
    return ""

def _get_treatment_amount_nis(session) -> Decimal | None:
    """
    מחזיר מחיר ב-₪ לפי Activity בשם 'שיעורי רכיבה/ טיפולית'.
    אם יש start_time/end_time – מנסה התאמה מדויקת ל-duration_minutes.
    אחרת נופל לפעילות הראשונה עם price לא-ריק.
    """
    try:
        qs = Activity.objects.filter(name="שיעורי רכיבה/ טיפולית").exclude(price__isnull=True)
        if not qs.exists():
            return None

        minutes = None
        st = getattr(session, "start_time", None)
        et = getattr(session, "end_time", None)
        base_date = getattr(session, "date", None) or timezone.localdate()

        if st and et:
            start_dt = datetime.combine(base_date, st)
            end_dt   = datetime.combine(base_date, et)
            if end_dt <= start_dt:  # ביטחון למקרה חריג
                end_dt += timedelta(days=1)
            minutes = int((end_dt - start_dt).total_seconds() // 60)

        act = None
        if minutes is not None:
            act = qs.filter(duration_minutes=minutes).order_by("id").first()
        if not act:
            act = qs.order_by("duration_minutes", "id").first()

        if act and act.price is not None:
            return Decimal(act.price)

    except Exception:
        pass
    return None

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
                    booking.payment_ref = _gen_unique_ref_any()
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
                _send_booking_email(payment_like, booking)
                if getattr(settings, "SEND_SMS", False) and (booking.customer_phone or "").strip():
                    sms_text = _format_booking_sms(payment_like, booking)
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
                        obj.payment_ref = _gen_unique_ref_any()

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
                _send_booking_email(payment_like, obj)
                if getattr(settings, "SEND_SMS", False) and (obj.customer_phone or "").strip():
                    sms_text = _format_booking_sms(payment_like, obj)
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
                        payment_ref=_gen_unique_ref_any(),
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
                _send_treatment_email(obj, amount=amount)
            except Exception:
                pass
            try:
                if getattr(settings, "SEND_SMS", False) and (obj.customer_phone or "").strip():
                    phone = _normalize_phone_il(obj.customer_phone)
                    sms_text = _format_session_sms(obj, amount=amount)
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
                obj.payment_ref = _gen_unique_ref_any()
                obj.save(update_fields=["payment_ref"])

            try:
                _send_treatment_email(obj)
            except Exception:
                pass
            try:
                if getattr(settings, "SEND_SMS", False) and (obj.customer_phone or "").strip():
                    phone = _normalize_phone_il(obj.customer_phone)
                    sms_text = _format_session_sms(obj)
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

def _format_session_sms(session, amount: Decimal | None = None, header: str | None = None) -> str:
    # אם לא קיבלנו override – נחשב מה-Activity "שיעורי רכיבה/ טיפולית"
    if amount is None:
        try:
            amount = _get_treatment_amount_nis(session)
        except NameError:
            amount = None

    name  = (session.customer_full_name or "לקוח/ה").strip()
    instr = getattr(session.instructor, "full_name", "") if getattr(session, "instructor", None) else ""
    ref   = session.payment_ref or "—"

    if getattr(session, "date", None):
        if session.start_time and session.end_time:
            times = f"{session.start_time.strftime('%H:%M')}–{session.end_time.strftime('%H:%M')}"
            when = f"{session.date:%d.%m.%Y} בשעה {_ltr_text(times)}"
        elif session.start_time:
            t = session.start_time.strftime("%H:%M")
            when = f"{session.date:%d.%m.%Y} בשעה {_ltr_text(t)}"
        else:
            when = f"{session.date:%d.%m.%Y}"
    else:
        when = ""

    # אם header לא סופק – נשמור על ההתנהגות הישנה (ניסוח "נקבעה בהצלחה")
    top_line = header or "ההזמנה נקבעה בהצלחה בחוות מסילת ציון."

    lines = [
        f"היי {name},",
        top_line,
        f"מס' הזמנה: {ref}",
    ]
    if when:  lines.append(f"תאריך: {when}")
    if instr: lines.append(f"מדריך/ה: {instr}")
    if amount is not None:
        lines.append(f"סכום ששולם: ₪{amount:}")  # שומר כמו שהיה אצלך
    lines.append("מיקום: חוות מסילת ציון (Waze: חוות מסילת ציון)")
    lines.append("יש להגיע עם מכנס ארוך ונעליים סגורות.")
    lines.append(" ")
    lines.append("מחכים לראותכם!")
    lines.append("(יש לשמור את מספר ההזמנה לביטולים והחזרים כספיים)")
    return "\n".join(lines)

def _send_treatment_email(session, amount: Decimal | None = None) -> bool:
    # ייבוא מקומי כדי שלא תצטרכי לשנות ייבואים בראש הקובץ
    from django.core.mail import EmailMultiAlternatives
    from django.conf import settings

    if not (session.customer_email or "").strip():
        return False

    # חישוב סכום אם לא הועבר
    if amount is None:
        amount = _get_treatment_amount_nis(session)

    subject_ref = session.payment_ref or "—"
    base_subject = f"אישור הזמנה – חוות מסילת ציון · {subject_ref}"

    # --- כיווניות נכונה לזמנים ---
    LRI, PDI = "\u2066", "\u2069"  # isolate LTR לחלק של השעה בטקסט
    # טקסט (SMS/Plain): נעטוף רק את החלק של השעה ב-LTR isolate
    if getattr(session, "date", None):
        if session.start_time and session.end_time:
            times = f"{session.start_time.strftime('%H:%M')}–{session.end_time.strftime('%H:%M')}"
            when_text = f"{session.date:%d.%m.%Y} בשעה {LRI}{times}{PDI}"
            when_html = f"""{session.date:%d.%m.%Y} בשעה <span dir="ltr" style="unicode-bidi:isolate">{times}</span>"""
        elif session.start_time:
            t = session.start_time.strftime("%H:%M")
            when_text = f"{session.date:%d.%m.%Y} בשעה {LRI}{t}{PDI}"
            when_html = f"""{session.date:%d.%m.%Y} בשעה <span dir="ltr" style="unicode-bidi:isolate">{t}</span>"""
        else:
            when_text = f"{session.date:%d.%m.%Y}"
            when_html = when_text
    else:
        when_text = ""
        when_html = ""

    instr = getattr(session.instructor, "full_name", "") if getattr(session, "instructor", None) else ""
    name  = (session.customer_full_name or "").strip() or "לקוח/ה"

    # --- גוף טקסטואלי (Plain) ---
    rlm = "\u200F"  # שומר RTL כללי
    text_lines = [
        f"שלום {name},", "",
        "ההזמנה שלך בחוות מסילת ציון נקבעה בהצלחה!",
        f"מספר הזמנה: {subject_ref}",
    ]
    if amount is not None:
        text_lines.append(f"סכום ששולם: ₪{amount:.2f}")
    if when_text:
        text_lines.append(f"תאריך ושעה: {when_text}")
    if instr:
        text_lines.append(f"מדריך/ה: {instr}")
    text_lines += [
        "", "מיקום: חוות מסילת ציון (Waze: חוות מסילת ציון)",
        "אנא הגיעו עם מכנס ארוך ונעליים סגורות.", "",
        "שמרו מייל זה. לביטול/החזר תזדקקו למספר ההזמנה.",
        "זהו מייל אוטומטי – אין להשיב אליו.",
    ]
    text_body = rlm + "\n".join(text_lines)

    # --- טבלת HTML ---
    amount_row = f"""
      <tr>
        <td style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">סכום ששולם</td>
        <td style="padding:12px 16px;font-size:14px;color:#111;text-align:right">₪{amount:.2f}</td>
      </tr>
    """ if amount is not None else ""
    time_row = f"""
      <tr>
        <td style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">תאריך ושעה</td>
        <td style="padding:12px 16px;font-size:14px;color:#111;text-align:right">{when_html}</td>
      </tr>
    """ if when_html else ""
    instr_row = f"""
      <tr>
        <td style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">מדריך/ה</td>
        <td style="padding:12px 16px;font-size:14px;color:#111;text-align:right">{instr}</td>
      </tr>
    """ if instr else ""

    html_body = f"""<!doctype html>
<html lang="he" dir="rtl">
  <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{base_subject}</title></head>
  <body style="margin:0;background:#f6f7f9;font-family:Arial,Helvetica,'Segoe UI',sans-serif;direction:rtl;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
               style="width:100%;max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;table-layout:fixed;">
          <tr><td style="padding:18px 24px;background:#2f2a27;color:#fff;font-weight:700;font-size:18px;text-align:right;">
            אישור הזמנה – חוות מסילת ציון</td></tr>
          <tr><td style="padding:22px;word-break:break-word;overflow-wrap:anywhere;">
            <h1 style="margin:0 0 12px 0;font-size:20px;color:#222;line-height:1.35;">שלום {name},</h1>
            <p style="margin:0 0 16px 0;font-size:15px;color:#333;line-height:1.6;">ההזמנה נקבעה בהצלחה!</p>
            <table role="presentation" cellpadding="0" cellspacing="0"
                   style="width:100%;border:1px solid #eee;border-radius:10px;overflow:hidden;border-collapse:separate;">
              <tr>
                <td style="padding:12px 16px;background:#fafafa;font-size:14px;color:#666;text-align:right">מספר הזמנה</td>
                <td style="padding:12px 16px;font-size:14px;color:#111;text-align:right;word-break:break-word;overflow-wrap:anywhere;">{subject_ref}</td>
              </tr>
              {amount_row}{time_row}{instr_row}
            </table>
            <p style="margin:10px 0 12px 0;font-size:13.5px;color:#555;line-height:1.6;">
              שמרו מייל זה. לביטול/החזר תזדקקו למספר ההזמנה.<br>
              זהו מייל אוטומטי – אין להשיב אליו.
            </p>
            <hr style="border:none;border-top:1px solid #eee;margin:18px 0">
            <p style="margin:0;font-size:12px;color:#999;line-height:1.5;">© חוות מסילת ציון · כל הזכויות שמורות</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""

    # --- Message-ID קבוע לשרשור עתידי ---
    from_addr = getattr(settings, "DEFAULT_FROM_EMAIL", None)
    domain = from_addr.split("@")[-1] if (from_addr and "@" in from_addr) else "mesilat-zion.local"
    stable_msgid = f"<session-{str(subject_ref).replace(' ', '')}@{domain}>"

    # --- שליחה עם EmailMultiAlternatives ---
    email = EmailMultiAlternatives(
        base_subject,
        text_body,
        from_addr,
        [session.customer_email],
        headers={
            "Message-ID": stable_msgid,
            "References": stable_msgid,
        },
    )

    email.attach_alternative(html_body, "text/html")
    sent = email.send(fail_silently=True)
    return sent >= 1

from django.core.mail import EmailMultiAlternatives

def _session_msgid(session) -> str:
    """
    יוצר Message-ID דטרמיניסטי לפי payment_ref כדי שכל הודעות ההמשך יתייחסו אליו.
    """
    ref = (session.payment_ref or _gen_unique_ref_any()).replace(" ", "")
    # דומיין ל-Message-ID: מנסה לקחת מה-DEFAULT_FROM_EMAIL, ואם לא – נופל לברירת מחדל.
    from_domain = getattr(settings, "DEFAULT_FROM_EMAIL", "")
    domain = from_domain.split("@")[-1] if "@" in from_domain else "mesilat-zion.local"
    return f"<session-{ref}@{domain}>"

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
    # שדות אדמין בלבד (לא נשמרים במודל)
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

    #list_filter = ("date", "instructor")
    list_filter = ("date",)
    search_fields = ("customer_full_name", "customer_phone", "instructor__full_name", "details", "payment_ref")
    ordering = ("-date", "start_time")

    fieldsets = (
        ("פרטי מפגש", {"fields": ("date", "start_time", "end_time", "instructor", "payment_ref")}),
        ("פרטי לקוח", {"fields": ("customer_full_name", "customer_phone", "customer_email")}),
        ("סוג ומחיר", {"fields": ("lesson_type", "calc_amount_nis", "override_amount_nis")}),  # ⬅️ הוספה כאן
        ("פרטים/הערות", {"fields": ("details",)}),
    )

    @admin.display(description="מדריך/ה")
    def instructor_plain(self, obj):
        return obj.instructor.full_name if obj and obj.instructor else "-"

    from django.conf import settings
    from django.template.loader import render_to_string

    def _notify_time_change(self, request, session, old_date, old_start, old_end):
        """
        שולח מייל + SMS על שינוי שעה.
        משתמש ב-_send_booking_email למייל וב-send_sms_via_ntfy ל-SMS (אם קיים במודול וה-SEND_SMS=True).
        """
        # ישן → חדש
        try:
            old_txt = f"{old_date.strftime('%d.%m.%Y')} {old_start.strftime('%H:%M')}–{old_end.strftime('%H:%M')}"
        except Exception:
            old_txt = f"{old_date} {old_start}–{old_end}"
        new_txt = f"{session.date.strftime('%d.%m.%Y')} {session.start_time.strftime('%H:%M')}–{session.end_time.strftime('%H:%M')}"

        sent_any = False

        # ===== מייל =====
        try:
            if (session.customer_email or "").strip():
                subject_ref = session.payment_ref or "—"
                base_subject = f"אישור הזמנה – חוות מסילת ציון · {subject_ref}"
                subject = base_subject
                plain = (
                    f"שלום {session.customer_full_name or ''},\n"
                    f"עודכנה שעת המפגש שלך בחוות מסילת ציון לתאריך:\n{new_txt}\n"
                )

                # HTML אם יש תבנית; אם אין – נשלח טקסט בלבד
                try:
                    html_body = render_to_string("emails/session_time_change.html", {
                        "customer_name": session.customer_full_name or "",
                        "instructor": session.instructor.full_name if session.instructor else "",
                        "old_time": old_txt,
                        "new_time": new_txt,
                        "payment_ref": session.payment_ref or "",
                        "phone": session.customer_phone or "",
                        "email": session.customer_email or "",
                        "details": session.details or "",
                    })
                except Exception:
                    html_body = None

                # קשר לשרשור המקורי
                root_msgid = _session_msgid(session)
                email = EmailMultiAlternatives(
                    subject,
                    plain,
                    getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    [session.customer_email],
                    headers={
                        "In-Reply-To": root_msgid,
                        "References": root_msgid,
                    },
                )
                if html_body:
                    email.attach_alternative(html_body, "text/html")

                email.send(fail_silently=True)
                sent_any = True
        except Exception as ex:
            print(f"[notify_time_change] email failed: {ex}")

        # ===== SMS =====
        try:
            phone_norm = _normalize_phone_il(session.customer_phone or "")
        except Exception:
            phone_norm = (session.customer_phone or "").strip()

        if getattr(settings, "SEND_SMS", False) and phone_norm:
            # טקסט SMS דרך הפורמט הקיים שלך, עם כותרת לעדכון שעה
            sms_text = _format_session_sms(
                session,
                amount=None,
                header=f"עודכנה שעת המפגש שלך בחוות מסילת ציון:\n",
            )

            # מחפשים פונקציית שליחה שמוגדרת באותו מודול (בלי ייבוא חיצוני)
            sms_fn = None
            mod = sys.modules[__name__]
            for name in ("send_sms_via_ntfy", "_send_booking_sms", "send_booking_sms", "_send_sms", "send_sms"):
                fn = getattr(mod, name, None)
                if callable(fn):
                    sms_fn = fn
                    break

            if sms_fn:
                try:
                    sms_fn(phone_norm, sms_text)  # חתימה: (phone, text)
                    sent_any = True
                except Exception as ex:
                    print(f"[notify_time_change] SMS send failed: {ex}")
            else:
                print("[notify_time_change] No SMS sender function found (e.g., send_sms_via_ntfy); SMS not sent")

        return sent_any

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
            obj.payment_ref = _gen_unique_ref_any()

        # מחיר: override גובר על המחושב (לוגיקה כפי שהייתה)
        override = form.cleaned_data.get("override_amount_nis")
        try:
            calc = _get_treatment_amount_nis(obj)
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

        # אם השעה/תאריך השתנו – שולחים עדכון (מייל + SMS) בלי שום פופאפ מיוחד
        if change and (old_date is not None):
            if (old_date != obj.date) or (old_start != obj.start_time) or (old_end != obj.end_time):
                try:
                    self._notify_time_change(request, obj, old_date, old_start, old_end)
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

    # ---- לוח מדריכים באדמין ----
    def get_urls(self):
        urls = super().get_urls()
        extra = [
            path("calendar/", self.admin_site.admin_view(self.calendar_view), name="treatmentsession_calendar"),
            path("list/",     self.admin_site.admin_view(self.force_list_view), name="treatmentsession_list"),
            path("events/",   self.admin_site.admin_view(self.events_api), name="treatmentsession_events"),
            path("events/update/", self.admin_site.admin_view(self.events_update), name="treatmentsession_events_update"),
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
                notified = self._notify_time_change(request, obj, old_date, old_start, old_end) or False
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
            path("book/", self.admin_site.admin_view(self.book_wizard), name="homePage_appointment_book"),
            path("pay/", self.admin_site.admin_view(admin_pay_stub), name="homePage_admin_pay_stub"),
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
            from django.db.models import Q

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
            if act.name in ("טיול כרכרה", "רכיבה זוגית"):
                total = unit
                breakdown = ""  # אין טעם להציג 2×
            else:
                total = (unit * Decimal(participants)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                breakdown = f"{participants}×{_fmt_ils(unit)}"

            return JsonResponse({
                "ok": True,
                "unit": str(unit),
                "participants": participants,
                "total": str(total),
                "total_display": _fmt_ils(total),
                "breakdown_display": breakdown,
            })

        # ---- GET רגיל: תצוגת הוויזארד ----
        elif request.method == "GET":
            all_names = distinct_activity_names()
            names = [n for n in all_names if not HIDE_RE.match((n or "").strip())]

            durations_map = {n: durations_for_name(n) for n in names}

            # בנייה חסינה של אורכי "רכיבה זוגית" לפי activity_type בפועל
            couple_qs = Activity.objects.filter(name="רכיבה זוגית").values("duration_minutes", "activity_type")
            couple_day, couple_night, couple_sunrise = set(), set(), set()
            for row in couple_qs:
                m = int(row["duration_minutes"] or 0)
                t = (row["activity_type"] or "").strip().lower()
                if not m:
                    continue
                if ("night" in t) or ("לילה" in t):
                    couple_night.add(m)
                elif ("sunrise" in t) or ("זריחה" in t) or ("שקיעה" in t) or ("sunset" in t):
                    couple_sunrise.add(m)
                else:
                    couple_day.add(m)

            fallback_all = set(durations_map.get("רכיבה זוגית", []))
            if not couple_day:
                couple_day = set(fallback_all)

            durations_couple = {
                "day": sorted(couple_day),
                "night": sorted(couple_night),
                "sunrise": sorted(couple_sunrise),
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
        from django.db.models import Q

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

        # מותר יין? (רק זוגית-יום ו-≥90 דק׳)
        allow_wine = (name == "רכיבה זוגית" and couple_variant == "day" and minutes >= 90)
        if not allow_wine:
            wine = ""

        # יצירה ותפיסה אטומית
        # --- במקום כל ה-with transaction.atomic() והיצירה בפועל ---

        # טקסט פרטים (כמו שהיה)
        details_txt = ""
        if name == "רכיבה זוגית" and wine:
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


