from django.contrib import admin
from .models import PageContent, Activity, Appointment, CustomSchedule


@admin.register(PageContent)
class PageContentAdmin(admin.ModelAdmin):
    list_display = ("id", "title")
    search_fields = ("title", "body")
    ordering = ("id",)


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "activity_type", "duration_minutes")
    search_fields = ("name", "description")
    list_filter = ("activity_type", "duration_minutes")
    ordering = ("name",)
    list_display_links = ("id", "name")


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "duration_minutes", "is_booked", "is_break")
    list_filter = ("is_booked", "is_break", "date")
    date_hierarchy = "date"
    ordering = ("-date", "time")
    search_fields = ("customer_name", "customer_phone")

    # עובד רק אם במודל Appointment יש:
    # activities = models.ManyToManyField(Activity, blank=True, related_name="appointments")
    filter_horizontal = ("activities",)


@admin.register(CustomSchedule)
class CustomScheduleAdmin(admin.ModelAdmin):
    list_display = ("date", "start_time", "end_time", "is_active")
    list_filter = ("is_active",)
    date_hierarchy = "date"
    ordering = ("-date",)

from django.contrib import admin
from .models import Weekday, BusinessHours, ActivityRule

@admin.register(Weekday)
class WeekdayAdmin(admin.ModelAdmin):
    list_display = ("code", "name")

@admin.register(BusinessHours)
class BusinessHoursAdmin(admin.ModelAdmin):
    list_display = ("season", "start_time", "end_time")
    filter_horizontal = ("days",)   # מציג צ׳קבוקסים / widget מרובה

@admin.register(ActivityRule)
class ActivityRuleAdmin(admin.ModelAdmin):
    list_display = ("activity","season","start_time","end_time","assigned_only","booking_cutoff_minutes")
    filter_horizontal = ("days",)
