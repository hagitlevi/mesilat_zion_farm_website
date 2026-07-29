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
import base64
import hashlib
import hmac
import json
import requests
from django.contrib import messages
from django_ratelimit.decorators import ratelimit
from homePage.utils import assign_unique_ref
from homePage.services.ntfy_gateway import normalize_phone_il, notify_booking_success
from ..services.booking_service import _finalize_booking_after_payment, _pick_booking_status
from ..views.consent import _has_consent_by_phone, _save_consent_by_phone
import logging

logger = logging.getLogger(__name__)
#--------------------------פונקציות עזר--------------------------
def _create_payplus_payment_link(request, payment) -> str:
    """יוצר קישור לדף הסליקה המתארח של PayPlus ושומר את מזהה בקשת התשלום."""
    return_url   = _append_qs(request.build_absolute_uri(reverse("pay_return")), payment_id=payment.id)
    callback_url = request.build_absolute_uri(reverse("pay_webhook"))

    payload = {
        "payment_page_uid": settings.PAYPLUS_PAYMENT_PAGE_UID,
        "amount":           float(Decimal(payment.amount_agorot) / 100),
        "currency_code":    payment.currency,
        "more_info":        str(payment.id),
        "refURL_success":   return_url,
        "refURL_failure":   return_url,
        "refURL_cancel":    return_url,
        "refURL_callback":  callback_url,
        "sendEmailApproval": False,
        "sendEmailFailure":  False,
    }
    headers = {
        "api-key":    settings.PAYPLUS_API_KEY,
        "secret-key": settings.PAYPLUS_SECRET_KEY,
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{settings.PAYPLUS_BASE_URL}/PaymentPages/generateLink",
        json=payload, headers=headers, timeout=10,
    )
    if not resp.ok:
        raise ValueError(f"PayPlus generateLink HTTP {resp.status_code}: {resp.text}")
    body = resp.json()
    if (body.get("results") or {}).get("code") != 0:
        raise ValueError(f"PayPlus generateLink error: {body.get('results')}")

    data = body["data"]
    payment.provider_session_id = data.get("page_request_uid", "")
    payment.save(update_fields=["provider_session_id"])
    return data["payment_page_link"]


def _query_payplus_transaction_status(payment, transaction_uid: str | None = None) -> tuple[str, str] | None:
    """
    שואלת את PayPlus ישירות (Transactions/View) מהו הסטטוס האמיתי של העסקה — לפי transaction_uid
    אם יש (מדויק יותר; מגיע מ-query params בחזרה מהסליקה), אחרת לפי more_info=payment.id.
    ברירת מחדל היא לחכות ל-webhook, אבל בסביבת פיתוח מקומית (localhost) PayPlus לא מצליחה
    להגיע לכתובת ה-callback שלנו, אז ה-webhook אף פעם לא מגיע. זו רשת ביטחון: בודקת ישירות
    מול PayPlus (עם ה-api-key/secret-key שלנו — לא סומכים על סטטוס שמגיע מה-URL של הלקוח,
    כי אפשר לזייף query params) אם העסקה בעצם כן הצליחה.
    מחזירה (status_code, charge_ref) גולמיים (למשל "000" להצלחה), או None אם אין תשובה חד-משמעית.
    """
    headers = {
        "api-key":    settings.PAYPLUS_API_KEY,
        "secret-key": settings.PAYPLUS_SECRET_KEY,
        "Content-Type": "application/json",
    }
    payload = {"transaction_uid": transaction_uid} if transaction_uid else {"more_info": str(payment.id)}
    try:
        resp = requests.post(
            f"{settings.PAYPLUS_BASE_URL}/Transactions/View",
            json=payload, headers=headers, timeout=6,
        )
    except requests.RequestException:
        logger.warning("payplus: status check request failed for payment #%s", payment.id, exc_info=True)
        return None
    if not resp.ok:
        logger.warning("payplus: status check HTTP %s for payment #%s: %s", resp.status_code, payment.id, resp.text[:500])
        return None
    try:
        body = resp.json()
    except ValueError:
        logger.warning("payplus: status check non-JSON response for payment #%s: %s", payment.id, resp.text[:500])
        return None

    results_code = (body.get("results") or {}).get("code")
    if results_code not in (0, None):
        logger.info("payplus: status check returned results.code=%s for payment #%s: %s", results_code, payment.id, body)
        return None

    # הצורה האמיתית שחזרה מ-PayPlus: data.transactions.transaction, כאשר "transaction" הוא
    # dict בודד אם יש תוצאה אחת, או list אם יש כמה (דומה למבנה XML-to-JSON טיפוסי).
    data = body.get("data") or {}
    txns_wrapper = data.get("transactions")
    if isinstance(txns_wrapper, dict):
        inner = txns_wrapper.get("transaction")
    elif isinstance(txns_wrapper, list):
        inner = txns_wrapper
    else:
        inner = data.get("transaction")

    if isinstance(inner, list):
        transactions = inner
    elif isinstance(inner, dict):
        transactions = [inner]
    else:
        transactions = None

    if not transactions:
        logger.info("payplus: status check found no transactions yet for payment #%s: %s", payment.id, body)
        return None

    txn = transactions[0]
    status_code = str(txn.get("status_code") or "").strip()
    if not status_code:
        logger.info("payplus: status check transaction has no status_code for payment #%s: %s", payment.id, txn)
        return None
    logger.info("payplus: status check resolved payment #%s to status_code=%s", payment.id, status_code)
    return status_code, (txn.get("transaction_uid") or txn.get("uid") or "")


def _apply_payment_outcome(request, payment, new_status: str, charge_ref: str | None = None) -> None:
    """
    מעדכנת סטטוס תשלום בצורה אידמפוטנטית: יוצרת Booking, מקצה ref, ושולחת התראות בהצלחה.
    לוגיקה משותפת ל-webhook (המקור העיקרי) ול-fallback שבודק סטטוס ישירות מול PayPlus
    (pay_status) — כדי שלשני המקורות תהיה בדיוק אותה התנהגות ואותה הגנת אידמפוטנטיות.
    """
    FINALS = ("succeeded", "failed", "canceled", "refunded")

    if payment.status == "succeeded":
        if not payment.charge_id and getattr(payment, "booking_id", None):
            payment.charge_id = (getattr(payment.booking, "payment_ref", None) or str(payment.booking_id))
            payment.save(update_fields=["charge_id"])
        return

    if payment.status in FINALS:
        return

    if new_status == "succeeded":
        extra = request.session.pop("pending_booking_extra", {})
        request.session.modified = True
        booking = _finalize_booking_after_payment(payment, pending_extra=extra)

        assign_unique_ref(booking, payment, digits=8)

        payment.status = "succeeded"
        payment.webhook_received_at = timezone.now()
        if charge_ref and not payment.charge_id:
            payment.charge_id = charge_ref
        payment.save(update_fields=["status", "webhook_received_at", "charge_id"])

        new_bstatus = _pick_booking_status(Booking, "confirmed", "paid", "succeeded")
        if getattr(booking, "status", None) != new_bstatus:
            try:
                booking.status = new_bstatus
                booking.save(update_fields=["status"])
            except Exception:
                booking.save()

        notify_booking_success(payment, booking)
    else:
        payment.status = new_status
        payment.webhook_received_at = timezone.now()
        payment.save(update_fields=["status", "webhook_received_at"])


def _verify_payplus_signature(request) -> bool:
    """מאמת שהבקשה אכן הגיעה מ-PayPlus: HMAC-SHA256 של גוף הבקשה הגולמי, מושווה מול header בשם hash."""
    if request.META.get("HTTP_USER_AGENT") != "PayPlus":
        return False
    received_hash = request.headers.get("hash", "")
    secret = settings.PAYPLUS_SECRET_KEY
    if not received_hash or not secret:
        return False
    computed_hash = base64.b64encode(
        hmac.new(secret.encode(), request.body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(computed_hash, received_hash)


def _append_qs(url, **params):
    logger.debug("_append_qs called with url: %s, params: %s", url, params)

    s, n, p, q, f = urlsplit(url)
    data = dict(parse_qsl(q))
    data.update({k: v for k, v in params.items() if v is not None})
    return urlunsplit((s, n, p, urlencode(data), f))
#----------------------------------------------------------------

@ratelimit(key="ip", rate="10/m", method="POST", block=True)
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

    if settings.PAYMENT_PROVIDER == "payplus":
        try:
            link = _create_payplus_payment_link(request, payment)
        except Exception:
            logger.exception("payplus: failed to create payment link for payment #%s", payment.id)
            payment.status = "failed"
            payment.error_message = "שגיאה ביצירת קישור לתשלום"
            payment.save(update_fields=["status", "error_message"])
            messages.error(request, "לא ניתן להתחיל את התשלום כרגע. נסו שוב מאוחר יותר.", extra_tags="payment_failed")
            return redirect("home")
        return redirect(link)
    return redirect(reverse("mock_checkout", kwargs={"payment_id": payment.id}))

# 2) חזרה מהסליקה — לא חוסמים בהמתנה ל-webhook, רק בודקים סטטוס נוכחי ומפנים לדף הבית.
#    אם עדיין pending, ה-JS בדף (base.html) יבצע polling מול pay_status עד שהתשלום יתעדכן.
def pay_return(request):
    """נקרא אחרי הסליקה (או אם המשתמש חזר באמצע) עם ?payment_id=XXX."""
    logger.debug("pay_return called with method: %s", request.method)
    if settings.DEBUG:
        # אבחון חד-פעמי: אם PayPlus בכל זאת מצרפת מידע שימושי ל-query string בחזרה
        # (סטטוס/מזהה עסקה), נרצה לראות זאת כאן כדי לשקול דרך אמינה יותר מ-polling.
        logger.info("pay_return GET params: %s", dict(request.GET))

    # נשלוף וננקה את ה-id מהסשן כדי לא להציג שוב הודעות בשגגה
    pid = request.GET.get("payment_id") or request.session.pop("last_payment_id", None)
    if not pid:
        messages.error(request, "לא נמצא תשלום תואם.", extra_tags="payment_failed")
        return redirect('home')

    finals = ('succeeded', 'failed', 'canceled', 'refunded')
    payment = Payment.objects.filter(id=pid).first()

    # PayPlus מצרפת transaction_uid ל-query string בחזרה מהסליקה — לא סומכים עליו כשלעצמו
    # (אפשר לזייף URL), אלא משתמשים בו כמפתח חיפוש לאימות אמיתי מול PayPlus (עם המפתחות
    # שלנו). זה פותר את בעיית ה-webhook שלא מגיע ב-localhost כבר בעמוד החזרה עצמו, בלי
    # לחכות ל-polling בכלל.
    txn_uid = request.GET.get("transaction_uid")
    if payment and payment.status == "pending" and payment.provider == "payplus" and txn_uid:
        result = _query_payplus_transaction_status(payment, transaction_uid=txn_uid)
        if result and result[0] == "000":
            _apply_payment_outcome(request, payment, "succeeded", charge_ref=result[1])
            payment.refresh_from_db()

    if payment and payment.status == 'succeeded':
        messages.success(
            request,
            "הקבלה והפרטים על התור יישלחו במייל ובהודעת SMS בדקות הקרובות.",
            extra_tags="payment_succeeded",
        )
    elif payment and payment.status in finals:
        # סטטוס סופי אמיתי (failed/canceled/refunded) — כישלון וודאי
        messages.error(
            request,
            "התשלום לא הושלם. ניתן לנסות שוב או ליצור קשר.",
            extra_tags="payment_failed",
        )
    else:
        # עדיין pending — לא ידוע בוודאות שנכשל (ה-webhook עלול להגיע באיחור),
        # אז לא מציגים הודעת כישלון שקרית ולא חוסמים את הבקשה. ה-JS בדף יעדכן בעצמו.
        messages.info(
            request,
            "התשלום מתעבד, רגע בבקשה...",
            extra_tags=f"payment_pending payment_id_{pid}",
        )

    return redirect('home')


def pay_status(request, payment_id: int):
    """
    נקודת קצה קלה ל-polling מהדפדפן: מחזירה את סטטוס התשלום הנוכחי בלי לחסום.
    אם עדיין pending ומדובר ב-PayPlus, בודקת גם ישירות מול PayPlus (Transactions/View)
    ולא רק מחכה ל-webhook — כי בסביבת פיתוח מקומית ה-webhook של PayPlus לא יכול להגיע
    ל-localhost. רק תוצאת "הצליח" מוחלת כאן (fail-safe): אם הבדיקה לא חד-משמעית, ממשיכים
    להסתמך על ה-webhook כמקור האמת לכישלון/ביטול, כדי לא לסמן בטעות תשלום שהצליח כנכשל.
    """
    payment = Payment.objects.filter(id=payment_id).first()
    if not payment:
        return JsonResponse({"status": "not_found"}, status=404)

    if payment.status == "pending" and payment.provider == "payplus":
        result = _query_payplus_transaction_status(payment)
        if result and result[0] == "000":
            _apply_payment_outcome(request, payment, "succeeded", charge_ref=result[1])

    return JsonResponse({"status": payment.status})

# Webhook — כאן קובעים סטטוס סופי, יוצרים Booking, ושולחים מייל+SMS (3
@csrf_exempt
def pay_webhook(request):
    """
    Webhook מאוחד — תומך במוק וב-PayPlus.

    מוק (DEBUG בלבד): POST (form) עם payment_id + outcome=success|fail|cancel
    PayPlus:          POST (JSON) מאומת ע"י header בשם hash (HMAC-SHA256 של הגוף)
    """
    logger.debug("pay_webhook called with method: %s", request.method)

    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    if request.headers.get("hash"):
        # פורמט PayPlus — אימות חתימה חובה (fail closed אם לא תקין)
        if not _verify_payplus_signature(request):
            logger.warning("webhook: payplus signature missing/mismatch from IP %s", request.META.get("REMOTE_ADDR"))
            return HttpResponseBadRequest("Unauthorized")
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            return HttpResponseBadRequest("invalid json")
        transaction = body.get("transaction") or {}
        try:
            pid = int(transaction.get("more_info"))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("missing more_info")
        status_code = str(transaction.get("status_code") or "").strip()
        new_status  = "succeeded" if status_code == "000" else "failed"
        charge_ref  = transaction.get("uid") or ""
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
        charge_ref  = None

    payment = get_object_or_404(Payment, id=pid)
    _apply_payment_outcome(request, payment, new_status, charge_ref)

    # Redirect אם הגיע next; אחרת JSON
    # מותרות רק URLs פנימיות למניעת Open Redirect
    next_url = request.POST.get("next") or request.GET.get("next")
    # startswith("/") alone allows //evil.com (protocol-relative open redirect)
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
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