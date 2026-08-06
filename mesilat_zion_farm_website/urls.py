"""
URL configuration for mesilat_zion_farm_website project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin # הוספה של שורת ייבוא זו
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include
from django.templatetags.static import static
from django.shortcuts import redirect
from django.http import HttpResponse

from homePage.sitemaps import StaticViewSitemap

sitemaps = {
    "static": StaticViewSitemap,
}


def favicon_redirect(request):
    return redirect(static('homePage/images/favicon.ico'))


ROBOTS_TXT = """User-agent: *
Allow: /

Disallow: /mzf-admin/
Disallow: /cancel-request/
Disallow: /booking-form/
Disallow: /confirm-booking/
Disallow: /available-appointment/
Disallow: /pay/
Disallow: /appointments/
Disallow: /children_riding/
Disallow: /group-riding/

Sitemap: https://mesilatzionfarm.co.il/sitemap.xml
"""


def robots_txt(request):
    return HttpResponse(ROBOTS_TXT, content_type="text/plain")


urlpatterns = [
    path('mzf-admin/', admin.site.urls),
    path('favicon.ico', favicon_redirect),
    path('robots.txt', robots_txt),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='sitemap'),
    path('', include('homePage.urls')),
]