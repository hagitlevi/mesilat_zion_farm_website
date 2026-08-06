from django.apps import AppConfig


class HomepageConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'homePage'

    def ready(self):
        # מסנכרן את מספר המשתתפים המקסימלי בפעילויות התלויות במספר הסוסים הפנוי
        # (NUMBER_OF_HORSES ב-ENV) בכל עליית שרת - כדי שלא יהיה צריך לעדכן ידנית
        # את הפעילויות באדמין בכל פעם שמספר הסוסים משתנה.
        from django.conf import settings
        from django.db import DatabaseError
        from django.db.utils import OperationalError, ProgrammingError

        try:
            from homePage.models import Activity
            Activity.objects.filter(
                name__in=["רכיבת שטח", "רכיבת לילה", "רכיבה בזריחה"]
            ).update(max_participants=settings.NUMBER_OF_HORSES)
        except (DatabaseError, OperationalError, ProgrammingError):
            # קורה למשל בזמן `migrate`/`makemigrations` לפני שהטבלה קיימת - לא קריטי,
            # הסנכרון פשוט ירוץ שוב בהפעלה הבאה של השרת.
            pass