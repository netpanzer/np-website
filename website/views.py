from django.shortcuts import render

# Create your views here.
def index(request):
    return render(request, 'index.html')

def history(request):
    return render(request, 'history.html')

def servers(request):
    return render(request, 'servers.html')

def ranking(request):
    return render(request, 'ranking.html')

def clans(request):
    return render(request, 'clans.html')

def blog(request):
    return render(request, 'blog.html')

def downloads(request):
    return render(request, 'downloads.html')

def chat(request):
    return render(request, 'chat.html')

def homepage(request):
    return render(request, 'index.html')