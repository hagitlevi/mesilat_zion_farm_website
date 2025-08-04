from django.shortcuts import render

def home(request):
    return render(request, 'homePage/home.html')

def riding_lessons_view(request):
    return render(request, 'homePage/riding_lessons.html')