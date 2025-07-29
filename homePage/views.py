from django.shortcuts import render

def home(request):
    return render(request, 'homePage/home.html')