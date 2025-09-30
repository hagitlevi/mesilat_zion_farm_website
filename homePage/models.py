from django.utils.translation import gettext_lazy as _
from django.db import models, transaction
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from datetime import time
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
    date = models.DateField(unique=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)  # האם יש תורים בכלל ביום הזה

    # זריחה
    allow_sunrise       = models.BooleanField(default=False)
    sunrise_start_time  = models.TimeField(null=True, blank=True)
    sunrise_end_time    = models.TimeField(null=True, blank=True)

    # לילה
    allow_night         = models.BooleanField(default=False)
    night_start_time    = models.TimeField(null=True, blank=True)
    night_end_time      = models.TimeField(null=True, blank=True)
    class Meta:
        verbose_name = "לוח זמנים מיוחד"
        verbose_name_plural = "לוחות זמנים מיוחדים"

    def __str__(self):
        return f"{self.date}: {self.start_time} - {self.end_time}" if self.is_active else f"{self.date}: לא פעיל"

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
    days = models.ManyToManyField(Weekday, related_name="business_hours")
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        verbose_name = "שעות עבודה כלליות"
        verbose_name_plural = "שעות עבודה כלליות"

    def __str__(self):
        return f"{self.season} {self.start_time}-{self.end_time}"

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
