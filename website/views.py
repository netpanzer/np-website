from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from website.models import Announcement


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

def statistics(request):
    return render(request, 'statistics.html')


DELIMITER = "____"


def api_v1_text_announcements(request: HttpRequest, after_id: int):
    # Returns announcements in a way the game can easily parse.
    announcements = Announcement.objects.filter(id__gt=after_id).order_by('id')
    results = []
    for announcement in announcements:
        results.append(DELIMITER.join([
            str(announcement.id),
            str(int(announcement.created_at.timestamp())),
            announcement.created_by.username if announcement.created_by else "Deleted",
            announcement.subject,
            announcement.message
        ]))
    return HttpResponse(f"{DELIMITER}next{DELIMITER}".join(results))
