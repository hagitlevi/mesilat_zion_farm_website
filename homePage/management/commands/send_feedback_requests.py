from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from datetime import timedelta
import secrets

from homePage.models import Booking

def _build_feedback_sms(booking, url: str) -> str:
    name = (booking.customer_name or "לקוח/ה").strip()
    lines = [
        f"היי {name}, מקווים שנהנית!",
        "נשמח למשוב קצר על החוויה:",
        url
    ]
    return "\n".join(lines)

class Command(BaseCommand):
    help = "שולח SMS בקשת משוב ~30 דק׳ אחרי סוף ההזמנה ללקוחות שטרם קיבלו בקשה"

    def add_arguments(self, parser):
        parser.add_argument("--window-min", type=int, default=120,
                            help="כמה דקות אחורה לחפש (ברירת מחדל: 120)")
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

            if not ok:
                try:
                    from homePage.services.ntfy_gateway import send_sms_via_phone
                    ok = send_sms_via_phone(phone, text)
                except Exception:
                    ok = False

            if ok:
                b.feedback_sms_sent_at = timezone.now()
                b.save(update_fields=["feedback_sms_sent_at", "feedback_token"])
                sent += 1

        self.stdout.write(f"Feedback SMS sent: {sent}")
