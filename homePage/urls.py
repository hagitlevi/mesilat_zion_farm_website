from django.urls import path
from . import views
urlpatterns = [
    path('', views.home, name='home'),
    path('riding-lessons/', views.riding_lessons_view, name='riding_lessons')

]