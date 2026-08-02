import smtplib

from django.conf import settings
from django.core.mail import BadHeaderError, EmailMultiAlternatives
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "בודקת שליחת מייל אמיתית מול הגדרות ה-SMTP הנוכחיות, בלי לעבור דרך תהליך הזמנה שלם."

    def add_arguments(self, parser):
        parser.add_argument(
            "--to", default=None,
            help="כתובת יעד (ברירת מחדל: EMAIL_HOST_USER — שולח לעצמך)",
        )

    def handle(self, *args, **options):
        to_addr = options["to"] or settings.EMAIL_HOST_USER
        self.stdout.write(f"SEND_EMAIL={getattr(settings, 'SEND_EMAIL', None)}")
        self.stdout.write(f"EMAIL_BACKEND={settings.EMAIL_BACKEND}")
        self.stdout.write(f"EMAIL_HOST={settings.EMAIL_HOST}:{settings.EMAIL_PORT} (TLS={settings.EMAIL_USE_TLS})")
        self.stdout.write(f"EMAIL_HOST_USER={settings.EMAIL_HOST_USER}")
        self.stdout.write(f"DEFAULT_FROM_EMAIL={settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"Sending test email to: {to_addr}")

        email = EmailMultiAlternatives(
            "בדיקת שליחת מייל — חוות מסילת ציון",
            "זהו מייל בדיקה בלבד, נשלח ידנית דרך test_email.",
            settings.DEFAULT_FROM_EMAIL,
            [to_addr],
        )
        try:
            sent = email.send(fail_silently=False)
        except (smtplib.SMTPException, OSError, BadHeaderError) as ex:
            self.stderr.write(self.style.ERROR(f"SMTP FAILED: {type(ex).__name__}: {ex}"))
            raise SystemExit(1)

        if sent >= 1:
            self.stdout.write(self.style.SUCCESS(f"OK — email sent to {to_addr}"))
        else:
            self.stderr.write(self.style.ERROR("send() returned 0 with no exception — unexpected"))
            raise SystemExit(1)
