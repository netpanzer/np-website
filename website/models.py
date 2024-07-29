from django.contrib import admin
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User


class Announcement(models.Model):
    created_at = models.DateTimeField(blank=True, null=True, default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    subject = models.CharField(max_length=1000)
    message = models.TextField(max_length=10000)


class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'created_by', 'subject',)


admin.site.register(Announcement, AnnouncementAdmin)
