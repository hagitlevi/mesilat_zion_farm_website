from django.conf import settings
from django.core.management.base import BaseCommand
from homePage.models import Activity


class Command(BaseCommand):
    help = "מסנכרן את מספר המשתתפים המקסימלי בפעילויות לפי NUMBER_OF_HORSES"

    def handle(self, *args, **options):
        updated = Activity.objects.filter(
            name__in=["רכיבת שטח", "רכיבת לילה", "רכיבה בזריחה"]
        ).update(max_participants=settings.NUMBER_OF_HORSES)
        self.stdout.write(f"עודכנו {updated} פעילויות")