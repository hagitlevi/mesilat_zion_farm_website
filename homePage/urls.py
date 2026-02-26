from django.urls import path
from . import views
from .views import cancel_request_view
from django.views.generic import TemplateView

urlpatterns = [
    path('', views.home, name='home'),
    path('riding-lessons/', views.riding_lessons_view, name='riding_lessons'),
    path('night-riding/', views.night_riding_view, name='night_riding'),
    path('/couple-riding/', views.couple_riding_view, name='couple_riding'),
    path('sunrise-riding/', views.sunrise_riding_view, name='sunrise_riding'),
    path('group-riding/', views.group_riding_view, name='group_riding'),
    path('carriage-trip/', views.carriage_trip_view, name='carriage_trip'),
    path('photographs/', views.photographs_view, name='photographs'),
    path('children_riding/', views.children_riding_view, name='children_riding'),
    path('gallery/', views.gallery_view, name='gallery'),
    path('available-appointment/<int:activity_id>/', views.available_appointment_view, name='available_appointment'),
    path('confirm-booking/', views.confirm_booking, name='confirm_booking'),
    path('booking-form/', views.booking_form, name='booking_form'),
    path("mock-payment-success/", views.mock_payment_success, name="mock_payment_success"),
    path('reviews/', views.site_reviews, name='site_reviews'),
    path("cancel-request/", cancel_request_view, name="cancel_request"),
    path("terms/", TemplateView.as_view(template_name="homePage/terms.html"), name="terms"),
    path("privacy/", TemplateView.as_view(template_name="homePage/privacy.html"), name="privacy"),
    path("cancel-policy/", TemplateView.as_view(template_name="homePage/cancel_policy.html"), name="cancel_policy"),
    path("pay/return/", views.pay_return, name="pay_return"),
    path("pay/start/", views.pay_start, name="pay_start"),
    path("pay/mock-checkout/<int:payment_id>/", views.mock_checkout, name="mock_checkout"),
    path("pay/webhook/", views.pay_webhook, name="pay_webhook"),  # לבדיקת הצלחה/כשל

    path("appointments/hold/", views.hold_appointment, name="hold_appointment"),

    path("appointments/release/", views.release_hold, name="release_hold"),

    path("appointments/snapshot/", views.appointments_snapshot, name="appointments_snapshot"),

    path("appointments/renew/", views.renew_hold, name="renew_hold"),
]