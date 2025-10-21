from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from datetime import timedelta, datetime
import secrets

from homePage.models import Booking, TreatmentSession  # ← NEW

def _build_feedback_sms(booking, url: str) -> str:
    name = (booking.customer_name or "לקוח/ה").strip()
    lines = [
        f"היי {name}, מקווים שנהנית!",
        "נשמח למשוב קצר על החוויה:",
        url
    ]
    return "\n".join(lines)

# ← NEW: אותו טקסט לטיפולית, רק שהשם מגיע משדה אחר
def _build_feedback_sms_session(session, url: str) -> str:
    name = (getattr(session, "customer_full_name", "") or "לקוח/ה").strip()
    lines = [
        f"היי {name}, מקווים שנהנית!",
        "נשמח למשוב קצר על החוויה:",
        url
    ]
    return "\n".join(lines)

class Command(BaseCommand):
    help = "שולח SMS בקשת משוב ~30 דק׳ אחרי סוף ההזמנה/המפגש ללקוחות שטרם קיבלו בקשה"

    def add_arguments(self, parser):
        parser.add_argument("--window-min", type=int, default=120,
                            help="כמה דקות אחורה לחפש (ברירת מחדל: 120)")
        parser.add_argument("--delay-min", type=int, default=30,
                            help="דיליי בדקות אחרי הסוף (ברירת מחדל: 30)")

    def handle(self, *args, **opts):
        if not getattr(settings, "SEND_SMS", False):
            self.stdout.write("SEND_SMS=False — לא שולח משובים.")
            return

        now = timezone.localtime()
        delay  = timedelta(minutes=int(opts["delay_min"]))
        window = timedelta(minutes=int(opts["window_min"]))

        target_before = now - delay            # הסתיימו עד הזמן הזה
        target_after  = now - (delay + window) # אבל לא ישנים מדי

        base_url = getattr(settings, "FEEDBACK_URL", "")
        if not base_url:
            self.stdout.write("FEEDBACK_URL not set — skipping.")
            return

        sent_bookings = 0
        sent_sessions = 0

        # ====== BOOKINGS (כמו שהיה) ======
        qs_b = (Booking.objects
                .filter(end_dt__lte=target_before, end_dt__gte=target_after)
                .filter(feedback_sms_sent_at__isnull=True))

        for b in qs_b:
            phone = (b.customer_phone or "").strip()
            if not phone:
                continue

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
                sent_bookings += 1

        # ====== TREATMENT SESSIONS (חדש) ======
        # מסננים לפי תאריך בטווח גס, ואת הדיוק לפי זמן עושים בפייתון
        tz = timezone.get_current_timezone()
        qs_s = (TreatmentSession.objects
                .filter(end_time__isnull=False,
                        date__gte=target_after.date(),
                        date__lte=target_before.date())
                .filter(feedback_sms_sent_at__isnull=True))

        for s in qs_s:
            # הופכים date+end_time ל־aware datetime כדי להשוות ל-now
            try:
                end_dt = timezone.make_aware(datetime.combine(s.date, s.end_time), tz)
            except Exception:
                continue

            if not (target_after <= end_dt <= target_before):
                continue

            phone = (getattr(s, "customer_phone", "") or "").strip()
            if not phone:
                continue

            if not (getattr(s, "feedback_token", "") or "").strip():
                s.feedback_token = secrets.token_urlsafe(16)

            text = _build_feedback_sms_session(s, base_url)

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
                s.feedback_sms_sent_at = timezone.now()
                s.save(update_fields=["feedback_sms_sent_at", "feedback_token"])
                sent_sessions += 1

        self.stdout.write(f"Feedback SMS sent — bookings: {sent_bookings}, sessions: {sent_sessions}")
