from django.utils.translation import gettext_lazy as _
from django.db import models, transaction                                 # בסיס מודלים של Django
from django.core.validators import MinValueValidator, MaxValueValidator  # ולידטורים לטווח
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone


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
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)  # האם יש תורים בכלל ביום הזה

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
    activity      = models.ForeignKey('Activity', on_delete=models.PROTECT, related_name='bookings')
    customer_name = models.CharField(max_length=50, blank=True)
    customer_phone= models.CharField(max_length=15, blank=True)
    customer_email= models.EmailField(blank=True)
    participants  = models.PositiveIntegerField(default=1)
    total_price   = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    payment_method= models.CharField(max_length=20, blank=True)  # 'credit_card'/'bit'/...
    payment_ref   = models.CharField(max_length=64, blank=True)
    status        = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    details       = models.CharField("פרטים/הערות", blank=True, null=True)
    start_dt      = models.DateTimeField()   # נוח להצגה וסינון
    end_dt        = models.DateTimeField()
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "הזמנה"
        verbose_name_plural = "הזמנות"

    def __str__(self):
        local_dt = timezone.localtime(self.start_dt)  # המרה ל־TZ שהוגדר ב־settings.py
        return f"#{self.id} | {self.activity} | {self.customer_name} | {local_dt:%Y-%m-%d - %H:%M}"

class Appointment(models.Model):
    date = models.DateField()
    time = models.TimeField()
    duration_minutes = models.IntegerField(default=15)
    is_booked = models.BooleanField(default=False)
    is_break = models.BooleanField(default=False)
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
    name = models.CharField(                                  # שם המגיב (רשות)
        max_length=80,
        blank=True
    )
    rating = models.PositiveSmallIntegerField(                # דירוג 1–5
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(                               # טקסט חופשי של התגובה (רשות)
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)      # נוצר ב־save הראשון
    updated_at = models.DateTimeField(auto_now=True)          # מתעדכן בכל save

    class Meta:
        ordering = ['-created_at']                            # תציגו מהחדש לישן
        indexes = [models.Index(fields=['created_at'])]
        verbose_name = "ביקורת"
        verbose_name_plural = "ביקורות"

    def __str__(self):
        who = self.name or "אנונימי"                         # אם אין שם—"אנונימי"
        return f"{who} ({self.rating}★)"                      # ייצוג נוח באדמין/קונסול




# --- שחרור סלוטים כשמוחקים Booking ---
def release_slots_for_booking(booking, using='default'):
    """
    משחרר את כל הסלוטים של ההזמנה:
    - ניתוק מההזמנה
    - סימון כפנוי (לא תפוס/לא הפסקה/לא משולם)
    - איפוס פרטי לקוח/רפרנס
    - איפוס activity ו-M2M activities (אם קיימים)
    """
    from .models import Appointment  # הימנעות מ-circular import אם מקומת במקום אחר

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
