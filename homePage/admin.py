from django.contrib import admin
from .models import PageContent, Activity, Appointment, CustomSchedule

@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'description']  # תציגי את id + name + description

admin.site.register(PageContent)

admin.site.register(Appointment)

admin.site.register(CustomSchedule)
