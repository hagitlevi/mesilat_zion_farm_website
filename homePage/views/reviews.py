from homePage.models import SiteReview, Booking, CancellationRequest
from ..forms import SiteReviewForm, CancelRequestForm
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit
from django.db.models import Avg
from django.core.paginator import Paginator
from django.shortcuts import render, redirect
from django.contrib import messages
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def _find_booking_by_payment_ref(payment_ref: str):
  """מחפש Booking לפי payment_ref, לא-תלוי-רישיות, ומחזיר את האחרון אם יש כמה עם אותו ref"""
  logger.debug("_find_booking_by_payment_ref called with payment_ref: %s", payment_ref)

  ref = (payment_ref or "").strip()
  if not ref:
    return None
  # חיפוש לא-תלוי-רישיות בשדה payment_ref
  return Booking.objects.filter(payment_ref__iexact=ref).order_by("-id").first()

def _client_ip(request):
    """שולף IP של הלקוח. מאחורי Render (proxy בודד) — הIP האחרון ב-XFF הוא האמיתי."""
    logger.debug("_client_ip called")

    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        # הproxy מוסיף את ה-IP האמיתי בסוף הרשימה — לכן לא [0] שאפשר לזייף
        return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR")

@require_http_methods(["GET", "POST"])
def cancel_request_view(request):
    """טופס בקשת ביטול - מאפשר למשתמשים לשלוח בקשה לביטול הזמנה קיימת, עם פרטים כמו booking_id או payment_ref"""
    logger.debug("cancel_request_view called with method: %s", request.method)

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

@require_http_methods(["GET", "POST"])
@ratelimit(key="ip", rate="5/m", method="POST", block=True)
def site_reviews(request):
    """דף ביקורות - מציג ביקורות קיימות ומאפשר להוסיף ביקורת חדשה עם טופס"""
    logger.debug("site_reviews called with method: %s", request.method)

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