from django.contrib import admin
from .models import PageContent # וודא שאתה מייבא את המודל הנכון

admin.site.register(PageContent) # וודא שהשם PageContent זהה לשם המודל שלך