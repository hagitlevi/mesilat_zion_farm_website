from homePage.models import Activity, SiteReview
from django.http import Http404
from django.db.models import Avg
from ..utils import group_consecutive_hours
from ..services.booking_service import build_business_hours_rows
import json
from django.shortcuts import render, get_object_or_404
from datetime import timedelta
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

def home(request):
  """דף הבית - מציג שעות פעילות, ביקורות אחרונות, ומידע כללי"""
  logger.debug("home view called")

  hours_rows = build_business_hours_rows()  # שלך, מחזיר ראשון..שבת
  hours_rows = group_consecutive_hours(hours_rows)  # קיבוץ רצפים זהים
  popup_payload = request.session.pop('payment_popup', None)  # קריאה חד-פעמית
  is_winter = ((timezone.localtime().dst() or timedelta(0)) == timedelta(0))
  latest_reviews = list(SiteReview.objects.order_by('-created_at')[:4])  # ← 4 אחרונות
  reviews_total = SiteReview.objects.count()
  reviews_avg = SiteReview.objects.aggregate(avg=Avg('rating'))['avg'] or 0
  return render(request, "homePage/home.html", {
    "hours_rows": hours_rows,  # שם המפתח לא משתנה → אין שינוי בתבנית
    "is_winter": is_winter,
    "payment_popup_json": json.dumps(popup_payload) if popup_payload else None,
    "latest_reviews": latest_reviews,
    "reviews_total": reviews_total,
    "reviews_avg": reviews_avg,
  })

def riding_lessons_view(request):
  """דף שיעורי רכיבה וטיפולית"""
  logger.debug("riding_lessons_view called")

  activity = get_object_or_404(Activity, name="שיעורי רכיבה/ טיפולית")
  return render(request, 'homePage/riding_lessons.html', {'activity': activity})

def night_riding_view(request):
  """דף רכיבת לילה - זמין רק בקיץ"""
  logger.debug("night_riding_view called")

  is_winter_now = ((timezone.localtime().dst() or timedelta(0)) == timedelta(0))
  if is_winter_now:
    raise Http404("‌رכיבת לילה אינה פעילה בחורף")

  activity = get_object_or_404(Activity, name="רכיבת לילה")
  return render(request, "homePage/night_riding.html", {"activity": activity})

def sunrise_riding_view(request):
  """דף רכיבת זריחה - זמין רק בקיץ"""
  logger.debug("sunrise_riding_view called")

  activity = get_object_or_404(Activity, name="רכיבה בזריחה")
  return render(request, 'homePage/sunrise_riding.html', {'activity': activity})

def couple_riding_view(request):
  """דף רכיבה זוגית - זמין רק בקיץ"""
  logger.debug("couple_riding_view called")

  activity = (
    Activity.objects
    .filter(name="רכיבה זוגית", activity_type="couple")  # יום רגיל כברירת מחדל
    .first()
  )

  if not activity:
    activity = Activity.objects.filter(name="רכיבה זוגית").order_by("id").first()
  if not activity:
    raise Http404("לא נמצאה פעילות 'רכיבה זוגית'")
  return render(request, 'homePage/couple_riding.html', {'activity': activity})

def group_riding_view(request):
  """דף רכיבת שטח - זמין רק בקיץ"""
  logger.debug("group_riding_view called")

  qs = Activity.objects.filter(name="רכיבת שטח").order_by('id')
  activity = qs.first()
  if not activity:
    raise Http404("לא נמצאה פעילות 'רכיבת שטח'")
  return render(request, 'homePage/group_riding.html', {'activity': activity})

def carriage_trip_view(request):
  """דף טיול כרכרה - זמין רק בקיץ"""
  logger.debug("carriage_trip_view called")

  qs = Activity.objects.filter(name="טיול כרכרה").order_by('id')
  activity = qs.first()  # תחזירי אחת – הראשונה
  if not activity:
    raise Http404("לא נמצאה פעילות 'טיול כירכרה'")
  return render(request, 'homePage/carriage_trip.html', {'activity': activity})

def photographs_view(request):
  """דף צילומים - זמין רק בקיץ"""
  logger.debug("photographs_view called")

  qs = Activity.objects.filter(name="צילומים").order_by('id')
  activity = qs.first()  # תחזירי אחת – הראשונה
  if not activity:
    raise Http404("לא נמצאה פעילות 'צילומים'")
  return render(request, 'homePage/photographs.html', {'activity': activity})

def children_riding_view(request):
  """דף רכיבת ילדים - זמין רק בקיץ"""
  logger.debug("children_riding_view called")

  activity = get_object_or_404(Activity, name="רכיבת ילדים")
  return render(request, 'homePage/children_riding.html', {'activity': activity})

def gallery_view(request):
  """דף גלריה - זמין רק בקיץ"""
  logger.debug("gallery_view called")

  return render(request, 'homePage/gallery.html')
