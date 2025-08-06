from django.shortcuts import render

def home(request):
    return render(request, 'homePage/home.html')

def riding_lessons_view(request):
    return render(request, 'homePage/riding_lessons.html')

def night_riding_view(request):
    return render(request, 'homePage/night_riding.html')

def sunrise_riding_view(request):
    return render(request, 'homePage/sunrise_riding.html')

def couple_riding_view(request):
    return render(request, 'homePage/couple_riding.html')

def group_riding_view(request):
    return render(request, 'homePage/group_riding.html')

def carriage_trip_view(request):
    return render(request, 'homePage/carriage_trip.html')

def photographs_view(request):
    return render(request, 'homePage/photographs.html')

def children_riding_view(request):
    return render(request, 'homePage/children_riding.html')

def gallery_view(request):
    return render(request, 'homePage/gallery.html')