from homePage.models import Activity, Appointment, Booking, Payment, MarketingConsent
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
import secrets
import time as time_mod
from django.contrib import messages
from homePage.utils import assign_unique_ref
from homePage.services.ntfy_gateway import normalize_phone_il, notify_booking_success
from ..services.booking_service import _finalize_booking_after_payment, _pick_booking_status
from ..views.consent import _has_consent_by_phone, _save_consent_by_phone
import logging

logger = logging.getLogger(__name__)
#--------------------------פונקציות עזר--------------------------
def _build_tranzila_url(request, payment) -> str:
    """בונה URL להעברת הלקוח לדף הסליקה של טרנזילה."""
    supplier    = settings.TRANZILA_SUPPLIER
    amount_nis  = str(Decimal(payment.amount_agorot) / 100)
    success_url = request.build_absolute_uri(reverse("pay_return"))
    fail_url    = request.build_absolute_uri(reverse("pay_return"))
    notify_url  = request.build_absolute_uri(reverse("pay_webhook"))

    params = {
        "supplier":    supplier,
        "sum":         amount_nis,
        "currency":    "1",       # 1 = שקל
        "tranmode":    "A",       # A = חיוב מיידי
        "cred_type":   "1",       # 1 = רגיל
        "lang":        "he",
        "contact":     payment.customer_name,
        "email":       payment.email,
        "phone":       payment.phone,
        "myid":        str(payment.id),
        "success_url": success_url,
        "fail_url":    fail_url,
        "notify_url":  notify_url,
    }
    if settings.TRANZILA_TERMINAL_PASSWORD:
        params["TranzilaPW"] = settings.TRANZILA_TERMINAL_PASSWORD

    base = f"https://direct.tranzila.com/{supplier}/iframenew.php"
    return f"{base}?{urlencode(params)}"


def _append_qs(url, **params):
    logger.debug("_append_qs called with url: %s, params: %s", url, params)

    s, n, p, q, f = urlsplit(url)
    data = dict(parse_qsl(q))
    data.update({k: v for k, v in params.items() if v is not None})
    return urlunsplit((s, n, p, urlencode(data), f))
#----------------------------------------------------------------

def pay_start(request):
    """
    מקבל POST מטופס מילוי פרטים.
    בודק הסכמה לתנאי שימוש — אם חסרה, מחזיר לטופס עם הודעת שגיאה.
    מחשב מחיר לפי DB, יוצר Payment בסטטוס 'pending',
    שומר הסכמת שיווק אם סומנה, ומפנה לדף הסליקה.
    """
    logger.debug("pay_start called with method: %s", request.method)

    if request.method != "POST":
        return HttpResponseBadRequest("Method not allowed. Expected POST.")

    appointment_id   = request.POST.get("appointment_id")
    activity_id      = request.POST.get("activity_id")
    duration_minutes = int(request.POST.get("duration_minutes", "60"))
    participants     = int(request.POST.get("participants", "1"))
    first_name       = request.POST.get("first_name","")
    last_name        = request.POST.get("last_name","")
    phone            = request.POST.get("phone","")
    email            = request.POST.get("email","")

    #אכיפת/שמירת הסכמה לפני המשך לתשלום
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


    # מחיר מחושב בשרת לפי מחיר הפעילות ב-DB
    activity = get_object_or_404(Activity, id=activity_id)
    full_amount = float(activity.price)
    if activity.name != "טיול כרכרה":
        full_amount =  full_amount * int(participants)
    amount_agorot = int((Decimal(str(full_amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    # אם המשתמש סימן וי – נשמור רשומת Marketing
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

    activity_type = (request.POST.get("activity_type") or "").strip().lower()
    wine = (request.POST.get("wine") or "").strip().lower()

    request.session["pending_booking_extra"] = {
        "activity_type": activity_type,
        "wine": wine,
    }
    request.session.modified = True
    payment = Payment.objects.create(
        provider=settings.PAYMENT_PROVIDER,
        amount_agorot=amount_agorot,
        currency="ILS",
        status="pending",
        appointment_id=appointment_id,
        activity_id=activity_id,
        duration_minutes=duration_minutes,
        participants=participants,
        customer_name=full_name,
        phone=phone.strip(),
        email=email.strip().lower(),
    )
    request.session['last_payment_id'] = payment.id

    if settings.PAYMENT_PROVIDER == "tranzila":
        return redirect(_build_tranzila_url(request, payment))
    return redirect(reverse("mock_checkout", kwargs={"payment_id": payment.id}))

# 2) חזרה מהסליקה — מחליטים על הודעה, ומפנים לדף הבית
def pay_return(request):
    """נקרא אחרי הסליקה (או אם המשתמש חזר באמצע) עם ?payment_id=XXX."""
    logger.debug("pay_return called with method: %s", request.method)

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

# Webhook — כאן קובעים סטטוס סופי, יוצרים Booking, ושולחים מייל+SMS (3
@csrf_exempt
def pay_webhook(request):
    """
    Webhook מאוחד — תומך במוק ובטרנזילה.

    מוק (DEBUG בלבד): POST עם payment_id + outcome=success|fail|cancel
    טרנזילה:          POST עם myid + Response ("000"=הצלחה) + TranzilaTK
    """
    logger.debug("pay_webhook called with method: %s", request.method)

    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    if request.POST.get("TranzilaTK"):
        # פורמט טרנזילה
        try:
            pid = int(request.POST.get("myid"))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("missing myid")
        tranzila_response = (request.POST.get("Response") or "").strip()
        new_status  = "succeeded" if tranzila_response == "000" else "failed"
        tranzila_tk = request.POST.get("TranzilaTK", "")
    else:
        # פורמט מוק — מותר רק בפיתוח
        if not settings.DEBUG:
            return HttpResponseBadRequest("Invalid request")
        try:
            pid = int(request.POST.get("payment_id"))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("missing payment_id")
        outcome    = (request.POST.get("outcome") or "").lower()
        status_map = {"success": "succeeded", "fail": "failed", "cancel": "canceled"}
        new_status  = status_map.get(outcome, "failed")
        tranzila_tk = None

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
            extra = request.session.pop("pending_booking_extra", {})
            request.session.modified = True
            booking = _finalize_booking_after_payment(payment, pending_extra=extra)

            # 2) הקצאת מזהה עסקה ייחודי בפורמט MZ-XXXXXXXX לשני הצדדים (Booking & Payment)
            ref = assign_unique_ref(booking, payment, digits=8)

            # 3) סימון סטטוס וסימון זמן קליטת ה-webhook
            payment.status = "succeeded"
            payment.webhook_received_at = timezone.now()
            # שמירת מזהה עסקה מטרנזילה אם קיים
            if tranzila_tk and not payment.charge_id:
                payment.charge_id = tranzila_tk
            payment.save(update_fields=["status", "webhook_received_at", "charge_id"])

            # 4) עדכון סטטוס ההזמנה
            new_bstatus = _pick_booking_status(Booking, "confirmed", "paid", "succeeded")
            if getattr(booking, "status", None) != new_bstatus:
                try:
                    booking.status = new_bstatus
                    booking.save(update_fields=["status"])
                except Exception:
                    booking.save()

            notify_booking_success(payment, booking)




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
    # מותרות רק URLs פנימיות למניעת Open Redirect
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and next_url.startswith("/"):
        if "payment_id=" not in next_url:
            next_url = _append_qs(next_url, payment_id=payment.id)
        return redirect(next_url)
    return JsonResponse({"ok": True, "status": payment.status, "charge_id": payment.charge_id})

#לפיתוח
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
    logger.debug("mock_payment_success called with method: %s", request.method)

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

# לפיתוח
def mock_checkout(request, payment_id: int):
    """דף בדיקה שמציג פרטי תשלום ומאפשר סימולציה של תרחישי הצלחה/כשל/ביטול ע"י קריאה ל-webhook עם פרמטרים שונים."""
    logger.debug("mock_checkout called with payment_id: %s", payment_id)

    payment = get_object_or_404(Payment, id=payment_id)
    amount_nis = payment.amount_agorot / 100.0
    return render(request, "homePage/mock_checkout.html",
                  {"payment": payment, "amount_nis": amount_nis})