from django.contrib import admin
from .models import Activity, Appointment, CustomSchedule, Booking


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


class AppointmentInline(admin.TabularInline):
    model = Appointment
    extra = 0
    can_delete = False
    fields = ("date", "time", "duration_minutes", "is_break", "is_booked", "is_paid", "payment_reference", "activity")
    readonly_fields = fields

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display  = ("id", "activity", "start_dt", "end_dt",
                     "customer_name", "customer_phone", "status", "payment_ref", "total_price", "participants")
    list_filter   = ("activity", "status", "start_dt")
    search_fields = ("customer_name", "customer_phone", "customer_email", "payment_ref")
    inlines = [AppointmentInline]

from django.contrib import admin
from .models import SiteReview

@admin.register(SiteReview)
class SiteReviewAdmin(admin.ModelAdmin):
    list_display  = ('name', 'rating', 'created_at')      # הורדנו is_approved/email
    list_filter   = ('rating',)
    search_fields = ('name', 'comment')
    ordering      = ('-created_at',)
