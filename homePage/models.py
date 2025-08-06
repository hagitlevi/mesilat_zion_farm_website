
from django.db import models

class PageContent(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    # ... שדות נוספים
    def _str_(self):
        return self.title

