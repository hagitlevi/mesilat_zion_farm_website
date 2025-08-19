from django.utils.translation import gettext_lazy as _
from django.db import models

class PageContent(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    def __str__(self):
        return self.title

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

    def __str__(self):
        return self.name

class Appointment(models.Model):
    date = models.DateField()
    time = models.TimeField()
    duration_minutes = models.IntegerField(default=15)
    is_booked = models.BooleanField(default=False)
    is_break = models.BooleanField(default=False)
    participants_count = models.IntegerField(default=2)
    is_paid = models.BooleanField(default=False)
    payment_reference = models.CharField(max_length=100, blank=True, null=True)
    customer_name = models.CharField(max_length=50, blank=True)
    customer_phone = models.CharField(max_length=15, blank=True)
    activities = models.ManyToManyField(
        Activity,
        blank=True,
        related_name="appointments"
    )

    def __str__(self):
        return f"{self.date} {self.time} {'- תפוס' if self.is_booked else '- פנוי'}"


class CustomSchedule(models.Model):
    date = models.DateField(unique=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)  # האם יש תורים בכלל ביום הזה

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

from datetime import datetime, timedelta
import math
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from homePage.models import Appointment  # התאם למסלול המודלים שלך

@transaction.atomic
def mock_payment_success(request):
    """
    סימולציית 'סיום תשלום':
    - קלט: appointment_id (בסלוט ההתחלתי), duration_minutes, payment_ref אופציונלי.
    - פעולה: תופס N סלוטים רצופים בני 15 דק' (N = ceil(duration/15)), מסמן is_paid+is_booked.
    - אם חסר סלוט או שיש סלוט שכבר נתפס → מחזיר 400 עם הודעה.
    """
    try:
        appointment_id = int(request.GET.get("appointment_id"))
        duration_minutes = int(request.GET.get("duration_minutes"))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("חסר/שגוי: appointment_id או duration_minutes")

    if duration_minutes <= 0:
        return HttpResponseBadRequest("duration_minutes חייב להיות > 0")

    payment_ref = request.GET.get("payment_ref") or f"MOCK-{timezone.now().strftime('%Y%m%d%H%M%S')}"

    # נועה: ננעל את הרשומה ההתחלתית כדי למנוע מרוץ
    base_appt = get_object_or_404(
        Appointment.objects.select_for_update(), id=appointment_id
    )

    # כמה סלוטים של 15 דק' צריך לתפוס
    slot_count = max(1, math.ceil(duration_minutes / 15))

    # בניית רשימת שעות (time) שצריך לתפוס באותו יום
    base_dt = datetime.combine(base_appt.date, base_appt.time)
    times_needed = [(base_dt + timedelta(minutes=15 * i)).time() for i in range(slot_count)]

    # נחלץ את כל הסלוטים הרלוונטיים לאותו תאריך, באותן שעות
    # אם יש לכם is_break בשדה – נשאיר מסנן שלא יתפוס הפסקות, אם לא – אפשר להסיר את התנאי.
    qs = Appointment.objects.select_for_update().filter(
        date=base_appt.date,
        time__in=times_needed,
    )
    # אם יש לכם שדה is_break, השאירו את זה:
    if 'is_break' in [f.name for f in Appointment._meta.get_fields()]:
        qs = qs.filter(is_break=False)

    appts = list(qs)
    # בדיקה שהבאנו את כל הסלוטים הנדרשים
    if len(appts) != slot_count:
        return HttpResponseBadRequest("לא נמצאו כל סלוטי הזמן הנדרשים לתור הזה.")

    # בדיקה שאין סלוט תפוס
    for a in appts:
        if a.is_booked:
            return HttpResponseBadRequest("אחד או יותר מהסלוטים כבר נתפסו. נסי לבחור תור אחר.")

    # עדכון כל הסלוטים: שולם + נתפס + רפרנס תשלום
    for a in appts:
        a.is_paid = True
        a.is_booked = True
        if hasattr(a, "payment_reference"):
            a.payment_reference = payment_ref
        a.save(update_fields=["is_paid", "is_booked"] + (["payment_reference"] if hasattr(a, "payment_reference") else []))

    # החזרה ללקוח (אפשר להחליף ל-redirect למסך "בוצע")
    return JsonResponse({
        "status": "ok",
        "message": "התשלום סומן כהושלם והתור נתפס בהצלחה.",
        "captured_slots": slot_count,
        "payment_reference": payment_ref,
        "date": base_appt.date.isoformat(),
        "times": [t.strftime("%H:%M") for t in times_needed],
    })
