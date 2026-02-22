from django.utils.translation import gettext_lazy as _
from django.db import models, transaction
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.contrib.auth.models import User
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from django.db.models import Q
from django.core.exceptions import ValidationError
from datetime import date
from convertdate import hebrew as hcal
from django.conf import settings


ACTIVITY_TYPES = [
    ('basic', 'מתחילים'),
    ('advanced', 'מתקדמים'),
    ('white', 'סוס לבן'),
    ('brown', 'סוס חום'),
    ('couple', 'רכיבה זוגית ביום'),
    ('couple_sunrise', 'רכיבה זוגית בזריחה'),
    ('couple_night', 'רכיבה זוגית בלילה'),
    ('none', 'ללא סוג'),
]


class Activity(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField()
    min_participants = models.IntegerField(default=1)
    max_participants = models.IntegerField(default=6)
    price = models.DecimalField(max_digits=6, decimal_places=2, default=0.0)
    activity_type = models.CharField(max_length=20, choices=ACTIVITY_TYPES, default='none')
    duration_minutes = models.IntegerField(choices=[(10, '10 דקות'), (30, '30 דקות'), (45, '45 דקות'), (60, 'שעה'), (90, 'שעה וחצי'), (120, 'שעתיים')], default=30)

    class Meta:
        verbose_name = "פעילות"
        verbose_name_plural = "פעילויות"

    def __str__(self):
        return self.name

class CustomSchedule(models.Model):
    KIND_CHOICES = [
        ("GREGORIAN", "תאריך לועזי"),
        ("HEBREW",    "תאריך עברי חוזר"),
    ]

    HEB_MONTH_CHOICES = [
        ("TISHREI",  "תשרי"),
        ("CHESHVAN", "חשוון"),
        ("KISLEV",   "כסלו"),
        ("TEVET",    "טבת"),
        ("SHEVAT",   "שבט"),
        ("ADAR",     "אדר"),      # ראו adar_policy לשנים מעוברות
        ("NISAN",    "ניסן"),
        ("IYAR",     "אייר"),
        ("SIVAN",    "סיוון"),
        ("TAMMUZ",   "תמוז"),
        ("AV",       "אב"),
        ("ELUL",     "אלול"),
        ("ADAR_I",   "אדר א׳"),
        ("ADAR_II",  "אדר ב׳"),
    ]

    ADAR_POLICY_CHOICES = [
        ("AUTO_ADAR2", "אוטומטי: אדר רגיל / אדר ב׳ בשנה מעוברת"),
        ("ALWAYS_A1",  "תמיד אדר א׳ (במעוברת) / אדר רגיל (בשאינה)"),
        ("ALWAYS_A2",  "תמיד אדר ב׳ (במעוברת) / אדר רגיל (בשאינה)"),
    ]

    MONTH_NAME_TO_NUM = {
        # convertdate.hebrew: ניסן=1 ... אלול=6, תשרי=7 ... אדר=12, אדר ב׳=13
        "NISAN": 1, "IYAR": 2, "SIVAN": 3, "TAMMUZ": 4, "AV": 5, "ELUL": 6,
        "TISHREI": 7, "CHESHVAN": 8, "KISLEV": 9, "TEVET": 10, "SHEVAT": 11,
        "ADAR": 12, "ADAR_I": 12, "ADAR_II": 13,
    }

    # ==== שדות כלליים ====
    kind      = models.CharField("סוג כלל", max_length=10, choices=KIND_CHOICES)
    name      = models.CharField("שם (לא חובה)", max_length=100, blank=True, default="")
    is_active = models.BooleanField("יום פעיל?", default=False,
                                    help_text="סמן/י אם עובדים ביום הזה. כיבוי = חסום לגמרי.")
    repeat_every_year = models.BooleanField("לחזור כל שנה?", default=False)

    # ==== לועזי ====
    date      = models.DateField("תאריך (לועזי)", null=True, blank=True)

    # ==== עברי ====
    h_month     = models.CharField("חודש עברי", max_length=10, choices=HEB_MONTH_CHOICES, blank=True)
    h_day       = models.PositiveSmallIntegerField("יום בחודש", null=True, blank=True)
    adar_policy = models.CharField("מדיניות אדר", max_length=12,
                                   choices=ADAR_POLICY_CHOICES, default="AUTO_ADAR2", blank=True)

    # ==== טווחי שעות / זריחה / לילה (כמו שהיה אצלך) ====
    start_time = models.TimeField("שעת התחלה", null=True, blank=True)
    end_time   = models.TimeField("שעת סיום",   null=True, blank=True)

    allow_sunrise       = models.BooleanField("לאפשר זריחה", default=False)
    sunrise_start_time  = models.TimeField("זריחה - התחלה", null=True, blank=True)
    sunrise_end_time    = models.TimeField("זריחה - סיום",   null=True, blank=True)

    allow_night         = models.BooleanField("לאפשר לילה", default=False)
    night_start_time    = models.TimeField("לילה - התחלה", null=True, blank=True)
    night_end_time      = models.TimeField("לילה - סיום",   null=True, blank=True)

    class Meta:
        verbose_name = "שינוי שעות יום עבודה"
        verbose_name_plural = "שינוי שעות יום עבודה"
        constraints = [
            models.UniqueConstraint(
                fields=["date"], condition=Q(kind="GREGORIAN"),
                name="uniq_customschedule_greg_date"
            ),
            models.UniqueConstraint(
                fields=["h_month", "h_day", "adar_policy"], condition=Q(kind="HEBREW"),
                name="uniq_customschedule_hebrew_combo"
            ),
        ]
        indexes = [
            models.Index(fields=["kind", "date"]),
            models.Index(fields=["kind", "h_month", "h_day"]),
        ]
        ordering = ("-id",)

    # ==== הצגה ====
    def __str__(self):
        base = self.label()
        return f"{base} – {'פעיל' if self.is_active else 'לא פעיל'}"

    def label(self) -> str:
        if self.kind == "GREGORIAN" and self.date:
            return self.date.strftime("%d.%m.%Y")
        if self.kind == "HEBREW" and self.h_month and self.h_day:
            return f"{self.get_h_month_display()} {self.h_day}"
        return self.name or "כלל"

    # ==== ולידציה ====
    def clean(self):
        errors = {}
        if self.kind == "GREGORIAN":
            if not self.date:
                errors["date"] = "נדרש תאריך לועזי."
        elif self.kind == "HEBREW":
            if not self.h_month:
                errors["h_month"] = "נדרש חודש עברי."
            if not self.h_day:
                errors["h_day"] = "נדרש יום בחודש עברי."
        if errors:
            raise ValidationError(errors)

    # ==== לוגיקה: האם כלל זה פוגע בתאריך לועזי נתון ====
    def matches_gregorian_date(self, gregorian_year: date):
        if self.kind == "GREGORIAN":
            if not self.date:
                return None
            # אם חוזר כל שנה → בונים תאריך עם אותה ימ/חודש לשנה המבוקשת
            if self.repeat_every_year:
                return date(gregorian_year, self.date.month, self.date.day)
            # חד-פעמי → מציגים רק אם זו השנה של התאריך עצמו
            return self.date if self.date.year == gregorian_year else None
        if self.kind == "HEBREW":
            hy, hm, hd = hcal.from_gregorian(gregorian_year.year, gregorian_year.month, gregorian_year.day)
            target_month = self._resolve_hebrew_month_for_year(hy)
            return (int(hd) == int(self.h_day or 0)) and (int(hm) == int(target_month))
        return False

    def to_gregorian_for_year(self, gregorian_year: int):
        """
        להמחשה/תצוגה: איזה תאריך לועזי יתקבל בשנה נתונה (אם קיים).
        """
        if self.kind == "GREGORIAN":
            return self.date if (self.date and self.date.year == gregorian_year) else None

        if self.kind == "HEBREW":
            # יתכנו 2 שנים עבריות בתוך שנה לועזית ← נבדוק את שתיהן
            hy_a = hcal.from_gregorian(gregorian_year, 1, 1)[0]
            hy_b = hcal.from_gregorian(gregorian_year, 12, 31)[0]
            for hy in {hy_a, hy_b}:
                hm = self._resolve_hebrew_month_for_year(hy)
                try:
                    y, m, d = hcal.to_gregorian(hy, hm, int(self.h_day or 1))
                except Exception:
                    continue
                if y == gregorian_year:
                    return date(y, m, d)
        return None

    # ==== עזר: הכרעת חודש אדר בשנים מעוברות ====
    def _resolve_hebrew_month_for_year(self, hebrew_year: int) -> int:
        base = self.h_month
        if not base:
            return 0
        is_leap = hcal.leap(hebrew_year)

        if base == "ADAR":
            if self.adar_policy == "AUTO_ADAR2":
                return 13 if is_leap else 12
            if self.adar_policy == "ALWAYS_A1":
                return 12
            if self.adar_policy == "ALWAYS_A2":
                return 13 if is_leap else 12

        if base == "ADAR_I":
            return 12 if is_leap else 12
        if base == "ADAR_II":
            return 13 if is_leap else 12

        return self.MONTH_NAME_TO_NUM[base]

class Season(models.TextChoices):
    SUMMER = "summer", _("קיץ")
    WINTER = "winter", _("חורף")

class Weekday(models.Model):
    code = models.PositiveSmallIntegerField(unique=True)
    name = models.CharField(max_length=20)

    class Meta:
        ordering = ["code"]
        verbose_name = "יום בשבוע"
        verbose_name_plural = "ימים בשבוע"

    def __str__(self):
        return self.name

class BusinessHours(models.Model):
    season = models.CharField(max_length=10, choices=[("summer","קיץ"),("winter","חורף")])
    days = models.ManyToManyField('Weekday', related_name="business_hours")
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        verbose_name = "שעות עבודה כלליות"
        verbose_name_plural = "שעות עבודה כלליות"

    def __str__(self):
        return f"{self.season} {self.start_time}-{self.end_time}"

    # --- חדש: לוגיקה מינימלית שמתאימה את העונה לפי שינוי השעון בישראל ---

    @staticmethod
    def _season_for_dt(dt=None) -> str:
        """מחזיר 'summer' אם DST פעיל ב־Asia/Jerusalem, אחרת 'winter'."""
        tz = ZoneInfo("Asia/Jerusalem")
        if dt is None:
            dt = timezone.now()
        local = dt.astimezone(tz)
        return "summer" if (local.dst() or timedelta(0)) != timedelta(0) else "winter"

    @classmethod
    def active_for_now(cls):
        """QuerySet של שעות העבודה הפעילות כרגע לפי שינוי השעון."""
        return cls.objects.filter(season=cls._season_for_dt())

    @classmethod
    def active_for_date(cls, date_obj):
        """
        QuerySet של שעות העבודה הפעילות לתאריך מסוים.
        משתמשים ב־12:00 באותו יום כדי לא ליפול בדיוק על שעת המעבר.
        """
        tz = ZoneInfo("Asia/Jerusalem")
        noon_local = datetime.combine(date_obj, time(12, 0)).replace(tzinfo=tz)
        return cls.objects.filter(season=cls._season_for_dt(noon_local))

class ActivityRule(models.Model):
    activity = models.ForeignKey("Activity", on_delete=models.CASCADE, related_name="rules")
    season = models.CharField(max_length=10, choices=[("summer","קיץ"),("winter","חורף")])
    days = models.ManyToManyField(Weekday, related_name="activity_rules")
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    end_is_midnight_next_day = models.BooleanField(default=False)
    assigned_only = models.BooleanField(default=False)
    booking_cutoff_minutes = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "כלל פעילות"
        verbose_name_plural = "כללי פעילות"

    def __str__(self):
        return f"{self.activity.name} {self.season}"

class Booking(models.Model):
    STATUS_CHOICES = [
        ("pending", "ממתין"),
        ("paid", "שולם"),
        ("failed", "נכשל"),
        ("refunded", "הוחזר"),
    ]
    activity      = models.ForeignKey('Activity', on_delete=models.PROTECT, related_name='bookings', verbose_name="פעילות")
    customer_name = models.CharField("שם", max_length=50, blank=True)
    customer_phone= models.CharField("טלפון", max_length=15, blank=True)
    customer_email= models.EmailField("מייל", blank=True)
    participants  = models.PositiveIntegerField("משתתפים", default=1)
    total_price   = models.DecimalField("מחיר", max_digits=9, decimal_places=2, null=True, blank=True)
    payment_method= models.CharField("שיטת תשלום", max_length=20, blank=True)
    payment_ref   = models.CharField("מס' הזמנה", max_length=64, blank=True, null=True,
                                     unique=True, db_index=True)
    status        = models.CharField("סטטוס", max_length=10, choices=STATUS_CHOICES, default="pending")
    details       = models.CharField("פרטים/הערות", blank=True, null=True)
    start_dt      = models.DateTimeField("שעת התחלה")
    end_dt        = models.DateTimeField("שעת סיום")
    created_at    = models.DateTimeField("נוצר ב-", auto_now_add=True)
    updated_at    = models.DateTimeField("עודכן ב-", auto_now=True)
    feedback_sms_sent_at = models.DateTimeField(null=True, blank=True)  # מתי נשלח SMS משוב
    feedback_token = models.CharField(max_length=40, blank=True, default="")
    class Meta:
        verbose_name = "הזמנה"
        verbose_name_plural = "הזמנות"

    def __str__(self):
        local_dt = timezone.localtime(self.start_dt)  # המרה ל־TZ שהוגדר ב־settings.py
        return f"#{self.id} | {self.activity} | {self.customer_name} | {local_dt:%Y-%m-%d - %H:%M}"

class Appointment(models.Model):
    date = models.DateField("תאריך")
    time = models.TimeField("שעה")
    duration_minutes = models.IntegerField("אורך", default=15)
    is_booked = models.BooleanField("נקבע", default=False)
    is_break = models.BooleanField("הפסקה", default=False)
    participants_count = models.IntegerField(default=2)
    is_paid = models.BooleanField(default=False)
    payment_reference = models.CharField(max_length=100, blank=True, null=True)
    booking   = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name='slots')
    customer_name = models.CharField(max_length=50, blank=True)
    customer_phone = models.CharField(max_length=15, blank=True)
    activity  = models.ForeignKey('Activity', null=True, blank=True, on_delete=models.SET_NULL)
    activities = models.ManyToManyField(
        Activity,
        blank=True,
        related_name="appointments"
    )
    hold_until = models.DateTimeField(null=True, blank=True, db_index=True)
    hold_token = models.UUIDField(null=True, blank=True, db_index=True)
    hold_created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    hold_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="held_appointments"
    )

    def is_held_active(self):
        return bool(self.hold_until and self.hold_until > timezone.now().astimezone(ZoneInfo("Asia/Jerusalem")))

    class Meta:
        verbose_name = "תור"
        verbose_name_plural = "תורים"

    def __str__(self):
        return f"{self.date} {self.time} {'- תפוס' if self.is_booked else '- פנוי'}"

class SiteReview(models.Model):                               # מודל תגובה/דירוג לכל האתר
    name = models.CharField("שם", max_length=80, blank=True)
    rating = models.PositiveSmallIntegerField("דירוג", validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField("תגובה", blank=True)
    created_at = models.DateTimeField("נוצר ב-", auto_now_add=True)      # נוצר ב־save הראשון
    updated_at = models.DateTimeField("עודכן ב-", auto_now=True)          # מתעדכן בכל save

    class Meta:
        ordering = ['-created_at']                            # תציגו מהחדש לישן
        indexes = [models.Index(fields=['created_at'])]
        verbose_name = "ביקורת"
        verbose_name_plural = "ביקורות"

    def __str__(self):
        who = self.name or "אנונימי"                         # אם אין שם—"אנונימי"
        return f"{who} ({self.rating}★)"                      # ייצוג נוח באדמין/קונסול

class CancellationRequest(models.Model):
    CHANNELS = [("web", "אתר"), ("phone", "טלפון"), ("whatsapp", "וואטסאפ")]
    STATUSES = [("pending", "ממתין"), ("approved", "אושר"), ("rejected", "נדחה"), ("refunded", "זוכה")]

    full_name  = models.CharField("שם מלא", max_length=120)
    phone      = models.CharField("טלפון", max_length=20)
    email      = models.EmailField("אימייל (לא חובה)", blank=True)

    # זה השדה שהלקוח מקליד — המספר שמופיע לו באישור תשלום/הזמנה
    order_id   = models.CharField("מס׳ הזמנה", max_length=80)

    # קישור להזמנה (Booking) אם נמצאה לפי payment_ref
    booking    = models.ForeignKey(
        "homePage.Booking",  # אם ה-Booking באפליקציה אחרת: "payments.Booking"
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cancellation_requests", verbose_name="Booking"
    )

    # קישור לתור (אם יש קשר כזה ב-Booking שלך)
    appointment = models.ForeignKey(
        "homePage.Appointment",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cancellation_requests", verbose_name="Appointment"
    )

    start_dt   = models.DateTimeField("מועד השירות (אם ידוע)", null=True, blank=True)
    reason     = models.TextField("סיבת ביטול (אופציונלי)", blank=True)

    channel    = models.CharField("ערוץ", max_length=20, choices=CHANNELS, default="web")
    status     = models.CharField("סטטוס", max_length=20, choices=STATUSES, default="pending")

    created_at = models.DateTimeField("נוצר ב־", auto_now_add=True)
    ip_address = models.GenericIPAddressField("IP", null=True, blank=True)
    user_agent = models.CharField("User-Agent", max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "בקשת ביטול עסקה"
        verbose_name_plural = "בקשות ביטול עסקה"

    @property
    def appointment_resolved(self):
        if self.appointment:
            return self.appointment
        if self.booking and hasattr(self.booking, "appointment"):
            return getattr(self.booking, "appointment")
        return None

    @property
    def activity_display(self):
        appt = self.appointment_resolved
        try:
            return str(appt.activity) if appt and hasattr(appt, "activity") else ""
        except Exception:
            return ""

    def save(self, *args, **kwargs):
        # אם אין תור משויך ויש כזה דרך Booking – נשלים
        if not self.appointment and self.booking and hasattr(self.booking, "appointment"):
            self.appointment = self.booking.appointment
        super().save(*args, **kwargs)

    def __str__(self):
        base = f"#{self.id} {self.full_name}"
        if self.order_id:
            base += f" | הזמנה: {self.order_id}"
        if self.activity_display:
            base += f" | פעילות: {self.activity_display}"
        if self.start_dt:
            local = timezone.localtime(self.start_dt)
            base += f" | מועד: {local:%Y-%m-%d %H:%M}"
        return base

class TermsConsent(models.Model):
    POLICY_CHOICES = [("terms","Terms of Service"), ("privacy","Privacy Policy")]

    policy      = models.CharField("מדיניות", max_length=16, choices=POLICY_CHOICES)
    version     = models.CharField("גרסה", max_length=16)                 # למשל "1.3"
    subject_id  = models.CharField("ID", max_length=32, db_index=True)  # כאן נשמור טלפון מנורמל (ספרות בלבד, 0XXXXXXXXX)
    full_name = models.CharField("שם מלא", max_length=120, blank=True)
    accepted_at = models.DateTimeField("אושר ב-", auto_now_add=True)
    ip          = models.GenericIPAddressField("IP", null=True, blank=True)
    user_agent  = models.CharField("user agent", max_length=255, blank=True)

    class Meta:
        unique_together = (("policy","version","subject_id"),)
        indexes = [models.Index(fields=["policy","version","subject_id"])]
        verbose_name = "אישר/ה מדיניות"
        verbose_name_plural = "אישורי מדיניות"

    def __str__(self):
        return f"{self.policy} v{self.version} by {self.subject_id} @ {self.accepted_at:%Y-%m-%d}"

class Payment(models.Model):
    STATUS = [
        ('created', 'נוצר'),
        ('pending', 'ממתין'),
        ('succeeded', 'הצליח'),
        ('failed', 'נכשל'),
        ('canceled', 'בוטל'),
        ('refunded', 'הוחזר'),
    ]
    provider = models.CharField(max_length=50, default='mock')
    amount_agorot = models.PositiveIntegerField()
    currency = models.CharField(max_length=10, default='ILS')
    status = models.CharField(max_length=20, choices=STATUS, default='created')

    # קישור לזרימה שלך
    appointment_id = models.IntegerField(null=True, blank=True)
    activity_id = models.IntegerField(null=True, blank=True)
    duration_minutes = models.IntegerField(null=True, blank=True)
    participants = models.IntegerField(default=1)
    customer_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)  # <— חדש כדי שתוכלי לשלוח מייל ללקוח

    # מזהי ספק/מידע
    provider_session_id = models.CharField(max_length=120, blank=True)
    error_code = models.CharField(max_length=50, blank=True)
    error_message = models.TextField(blank=True)
    raw_metadata = models.JSONField(default=dict, blank=True)

    # חזרה/ביטול (Hosted)
    return_url = models.URLField(blank=True)
    cancel_url = models.URLField(blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    webhook_received_at = models.DateTimeField(null=True, blank=True)

    booking = models.OneToOneField(
        'Booking', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='payment'
    )
    charge_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    def __str__(self):
        return f"Payment#{self.id} {(self.amount_agorot/100):.2f} {self.currency} [{self.status}]"

class ScheduleBoard(Appointment):
    class Meta:
        proxy = True
        verbose_name = "לוח שעות - הזמנות"
        verbose_name_plural = "לוח שעות - הזמנות"

class MonthlySummary(Booking):
    class Meta:
        proxy = True
        verbose_name = "סיכום חודש"
        verbose_name_plural = "סיכום חודש"

class Instructor(models.Model):
    user = models.OneToOneField(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="instructor_profile"
    )
    full_name = models.CharField("שם מלא של מדריך/ה", max_length=120)
    phone = models.CharField("טלפון", max_length=32, blank=True)
    active = models.BooleanField("פעיל/ה", default=True)
    color = models.CharField("צבע בלוח", max_length=7, blank=True, help_text="HEX כמו #42A5F5")

    class Meta:
        verbose_name = "מדריך/ה"
        verbose_name_plural = "מדריכים"
        ordering = ("full_name",)

    def __str__(self):
        return self.full_name


class TreatmentSession(models.Model):
    date = models.DateField("תאריך")
    start_time = models.TimeField("שעת התחלה")
    end_time = models.TimeField("שעת סיום")
    LESSON_TYPE_CHOICES = [
        ("lesson", "שיעור רכיבה"),
        ("therapy", "רכיבה טיפולית"),
    ]

    lesson_type = models.CharField(
        "סוג מפגש",
        max_length=20,
        choices=LESSON_TYPE_CHOICES,
        default="lesson",
        blank=True
    )
    # שם מדריך יילקח מהמודל – אין instructor_name יותר
    instructor = models.ForeignKey(
        Instructor, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="treatment_sessions", verbose_name="מדריך/ה"
    )
    payment_ref = models.CharField(
        "מס' עסקה", max_length=64, blank=True, null=True,
        unique=True, db_index=True
    )
    # פרטי לקוח
    customer_full_name = models.CharField("שם מלא של לקוח/ה", max_length=120)
    customer_phone = models.CharField("טלפון לקוח/ה", max_length=32)
    customer_email = models.EmailField("אימייל לקוח/ה", blank=True)

    details = models.TextField("פרטים/הערות", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    feedback_sms_sent_at = models.DateTimeField("נשלח SMS משוב ב-", null=True, blank=True)
    feedback_token = models.CharField("אסימון משוב", max_length=40, blank=True, default="")

    def __str__(self):
        instr = self.instructor.full_name if self.instructor else "—"
        return f"{self.date:%Y-%m-%d} | {self.start_time:%H:%M}-{self.end_time:%H:%M} | {self.customer_full_name} → {instr}"

    def clean(self):
        if self.end_time <= self.start_time:
            raise ValidationError("שעת סיום חייבת להיות אחרי שעת התחלה.")
        start_limit = time(9, 0)
        end_limit = time(20, 0)
        if not (start_limit <= self.start_time <= end_limit):
            raise ValidationError("שעת התחלה מותרת בין 09:00 ל-20:00.")
        if not (start_limit <= self.end_time <= end_limit):
            raise ValidationError("שעת סיום מותרת בין 09:00 ל-20:00.")

    class Meta:
        verbose_name = "הזמנה לרכיבה טיפולית"
        verbose_name_plural = "לוח שעות - רכיבה טיפולית"
        ordering = ("-date", "start_time")
        indexes = [models.Index(fields=["date", "start_time"])]
        permissions = [
            ("can_drag_sessions", "Can drag/resize sessions on calendar"),
            ("can_create_sessions", "Can create sessions from calendar"),
            ("change_details_only", "Can change details field only"),
        ]

class MarketingConsent(models.Model):
    CHANNEL_CHOICES = [
        ("sms", "SMS / טלפון"),
        ("email", "Email"),
        ("whatsapp", "WhatsApp"),
    ]

    version     = models.CharField("גרסה", max_length=16)  # למשל "1.3"
    channel     = models.CharField("ערוץ", max_length=16, choices=CHANNEL_CHOICES)
    subject_id  = models.CharField("מזהה נושא", max_length=64, db_index=True)  # טלפון או מייל מנורמל
    full_name   = models.CharField("שם מלא", max_length=120, blank=True)
    customer_email = models.EmailField("מייל", blank=True)
    accepted_at = models.DateTimeField("אושר ב-", auto_now_add=True)
    ip          = models.GenericIPAddressField("IP", null=True, blank=True)
    user_agent  = models.CharField("user agent", max_length=255, blank=True)

    class Meta:
        unique_together = ("channel", "subject_id", "version")  # כדי שלא יאשר פעמיים אותו דבר

    def __str__(self):
        return f"{self.get_channel_display()} – {self.subject_id} ({self.version})"


# --- שחרור סלוטים כשמוחקים Booking ---
def release_slots_for_booking(booking, using='default'):
    """
    משחרר את כל הסלוטים של ההזמנה:
    - ניתוק מההזמנה
    - סימון כפנוי (לא תפוס/לא הפסקה/לא משולם)
    - איפוס פרטי לקוח/רפרנס
    - איפוס activity ו-M2M activities (אם קיימים)
    """

    with transaction.atomic(using=using):
        qs = Appointment.objects.using(using).select_for_update().filter(booking=booking)
        appts = list(qs)

        # לנקות קשרי M2M אי־אפשר בבאלק
        for a in appts:
            if hasattr(a, "activities"):
                a.activities.clear()

        # איפוס שדות בבאלק + ניתוק מההזמנה
        Appointment.objects.using(using).filter(pk__in=[a.pk for a in appts]).update(
            booking=None,
            is_booked=False,
            is_break=False,
            is_paid=False,
            payment_reference="",
            activity=None,          # אצלך FK הזה nullable ✅
            customer_name="",
            customer_phone="",
        )

@receiver(pre_delete, sender=Booking)
def release_on_booking_delete(sender, instance, using, **kwargs):
    # לפני מחיקת ההזמנה – משחררים את כל הסלוטים שלה
    release_slots_for_booking(instance, using=using)
