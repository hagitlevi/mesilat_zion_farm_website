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
