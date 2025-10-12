from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
import secrets
from datetime import datetime, timedelta
from django.core.cache import cache
from homePage.models import Booking, TreatmentSession

def _get_name(obj) -> str:
    # Booking: customer_name  |  TreatmentSession: customer_full_name
    return (
        (getattr(obj, "customer_name", "") or "").strip()
        or (getattr(obj, "customer_full_name", "") or "").strip()
        or "לקוח/ה"
    )

def _build_feedback_sms(obj, url: str) -> str:
    name = _get_name(obj)
    lines = [
        f"היי {name}, מקווים שנהנית!",
        "נשמח למשוב קצר על החוויה:",
        url
    ]
    return "\n".join(lines)

class Command(BaseCommand):
    help = "שולח SMS בקשת משוב ~30 דק׳ אחרי סוף ההזמנה ללקוחות שטרם קיבלו בקשה"

    def add_arguments(self, parser):
        parser.add_argument("--window-min", type=int, default=450,
                            help="כמה דקות אחורה לחפש (ברירת מחדל: 450)")
        parser.add_argument("--delay-min", type=int, default=30,
                            help="דיליי בדקות אחרי סוף ההזמנה (ברירת מחדל: 30)")

    def handle(self, *args, **opts):
        if not getattr(settings, "SEND_SMS", False):
            self.stdout.write("SEND_SMS=False — לא שולח משובים.")
            return

        now = timezone.localtime()
        delay = timedelta(minutes=int(opts["delay_min"]))         # 30 דק׳
        window = timedelta(minutes=int(opts["window_min"]))       # 120 דק׳ אחורה

        target_before = now - delay            # הזמנות שהסתיימו עד הזמן הזה
        target_after  = now - (delay + window) # אבל לא ישנות מדי

        qs = (Booking.objects
              .filter(end_dt__lte=target_before, end_dt__gte=target_after)
              .filter(feedback_sms_sent_at__isnull=True))

        base_url = getattr(settings, "FEEDBACK_URL", "")
        sent = 0

        for b in qs:
            phone = (b.customer_phone or "").strip()
            if not phone or not base_url:
                continue

            # אסימון ייחודי (אפשר להשתמש בו בעתיד לקישור מותאם)
            if not (b.feedback_token or "").strip():
                b.feedback_token = secrets.token_urlsafe(16)

            text = _build_feedback_sms(b, base_url)

            ok = False
            try:
                from homePage.services.ntfy_gateway import send_sms_via_ntfy
                ok = send_sms_via_ntfy(phone, text)
            except Exception:
                ok = False


            if ok:
                b.feedback_sms_sent_at = timezone.now()
                b.save(update_fields=["feedback_sms_sent_at", "feedback_token"])
                sent += 1

        # === TreatmentSession (טיפולית) – בלי שדות ב-DB, חסימה מכפילויות דרך cache ===
        tz = timezone.get_current_timezone()
        s_qs = (TreatmentSession.objects
                .filter(end_time__isnull=False,
                        date__gte=(target_after - timedelta(days=1)).date(),
                        date__lte=(target_before + timedelta(days=1)).date()))

        for s in s_qs:
            phone = (s.customer_phone or "").strip()
            if not phone or not base_url:
                continue

            # מחברים date + end_time לזמן מודע ל-TZ
            try:
                end_local = timezone.make_aware(datetime.combine(s.date, s.end_time), tz)
            except Exception:
                continue

            # בתוך החלון בלבד (אחרי הדיליי ולפני חלון החיפוש)
            if not (target_after <= end_local <= target_before):
                continue

            # דה-דופליקציה: cache key לפי מזהה וזמן סיום
            cache_key = f"feedback_sms:session:{s.id}:{int(end_local.timestamp())}"
            if not cache.add(cache_key, 1, timeout=14 * 24 * 3600):  # 14 יום
                continue

            text = _build_feedback_sms(s, base_url)

            ok = False
            try:
                from homePage.services.ntfy_gateway import send_sms_via_ntfy
                ok = send_sms_via_ntfy(phone, text)
            except Exception:
                ok = False


            if ok:
                sent += 1

        self.stdout.write(f"Feedback SMS sent: {sent}")
