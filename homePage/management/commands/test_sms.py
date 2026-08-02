from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from homePage.services.ntfy_gateway import send_sms_via_ntfy


class Command(BaseCommand):
    help = ("בודקת שליחת SMS אמיתי דרך ntfy מול הגדרות הסביבה הנוכחיות. "
            "דורש --phone מפורש בכוונה (בלי ברירת מחדל) כדי שלא יישלח בטעות למספר לא רצוי.")

    def add_arguments(self, parser):
        parser.add_argument("--phone", required=True, help="מספר טלפון ישראלי לבדיקה (חובה)")

    def handle(self, *args, **options):
        phone = options["phone"]
        self.stdout.write(f"SEND_SMS={getattr(settings, 'SEND_SMS', None)}")
        self.stdout.write(f"NTFY_URL={getattr(settings, 'NTFY_URL', None)}")
        self.stdout.write(f"NTFY_TOPIC set={bool(getattr(settings, 'NTFY_TOPIC', ''))}")
        self.stdout.write(f"Sending test SMS to: {phone}")

        if not getattr(settings, "SEND_SMS", False):
            raise CommandError("SEND_SMS is off in current settings — nothing will be sent.")

        try:
            sent = send_sms_via_ntfy(phone, "בדיקת SMS — חוות מסילת ציון (test_sms)")
        except Exception as ex:
            self.stderr.write(self.style.ERROR(f"ntfy call FAILED: {type(ex).__name__}: {ex}"))
            raise SystemExit(1)

        if sent:
            self.stdout.write(self.style.SUCCESS(f"OK — ntfy accepted the message for {phone}"))
        else:
            self.stderr.write(self.style.ERROR("send_sms_via_ntfy returned falsy — check topic/phone/text"))
            raise SystemExit(1)
