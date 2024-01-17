"""
URL configuration for netpanzer project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
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
from django.contrib import admin
from django.urls import path
from website.views import index
from website.views import history
from website.views import servers
from website.views import clans
from website.views import ranking
from website.views import blog
from website.views import downloads
from website.views import chat
from website.views import homepage

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', index, name='index'), 
    path('history.html', history, name='history'),
    path('servers.html', servers, name='servers'),
    path('clans.html', clans, name='clans'),
    path('ranking.html', ranking, name='ranking'),
    path('blog.html', blog, name='blog'),
    path('downloads.html', downloads, name='downloads'),
    path('chat.html', chat, name='chat'),
    
]
