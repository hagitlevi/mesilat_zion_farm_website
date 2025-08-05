from django.urls import path
from . import views
urlpatterns = [
    path('', views.home, name='home'),
    path('riding-lessons/', views.riding_lessons_view, name='riding_lessons'),
    path('night-riding/', views.night_riding_view, name='night_riding'),
    path('couple-riding/', views.couple_riding_view, name='couple_riding'),
    path('sunrise-riding/', views.sunrise_riding_view, name='sunrise_riding'),
    path('group-riding/', views.group_riding_view, name='group_riding'),
    path('carriage-trip/', views.carriage_trip_view, name='carriage_trip'),
    path('photographs/', views.photographs_view, name='photographs'),
    path('children_riding/', views.children_riding_view, name='children_riding'),

]