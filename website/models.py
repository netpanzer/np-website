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


class GameServer(models.Model):
    """A game server the master server has told us about."""

    address = models.CharField(max_length=255)
    port = models.IntegerField()
    name = models.CharField(max_length=255, blank=True, default='')
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('address', 'port')

    def __str__(self):
        return f'{self.name or "unnamed"} ({self.address}:{self.port})'


class PlayerRoundState(models.Model):
    """
    Last counters we observed for a player on a server.

    The \\status\\ reply only reports the *current round*, and those counters
    reset on map change or when a player rejoins. This row is the cursor we
    diff against to turn those absolute values into increments.
    """

    server = models.ForeignKey(GameServer, on_delete=models.CASCADE)
    player_name = models.CharField(max_length=255)
    last_kills = models.IntegerField(default=0)
    last_deaths = models.IntegerField(default=0)
    last_score = models.IntegerField(default=0)
    last_points = models.IntegerField(default=0)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('server', 'player_name')

    def __str__(self):
        return f'{self.player_name} @ {self.server}'


class PlayerStat(models.Model):
    """
    Shared shape for the player leaderboards.

    The ranking metrics are stored rather than derived on read. They only change
    when we ingest a snapshot, but they are ordered by on every page view, so
    keeping them as columns lets the database sort with an index and return one
    page rather than making Python materialise and sort the whole table.
    """

    player_name = models.CharField(max_length=255, db_index=True)
    kills = models.IntegerField(default=0)
    deaths = models.IntegerField(default=0)
    score = models.IntegerField(default=0)
    points = models.IntegerField(default=0)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)

    # Maintained by recompute() on write. See SORT_FIELDS in views.py for the
    # orderings these back.
    strength = models.FloatField(default=0)
    kd_ratio = models.FloatField(default=0)

    class Meta:
        abstract = True

    def recompute(self):
        """Refresh the stored ranking metrics from the raw counters."""
        # Composite index, kept identical to the ranking players are used to.
        self.strength = 2 * self.kills + self.points - 0.5 * self.deaths
        # Favours efficiency while avoiding division by zero.
        self.kd_ratio = (self.kills + 1) / (self.deaths + 1)

    def save(self, *args, **kwargs):
        """
        Keep the stored metrics in step with the counters.

        Doing this here rather than at the call site means no write path -
        ingest, the admin, a future import - can leave the ranking columns
        disagreeing with the kills/deaths/points they are derived from.
        bulk_create() bypasses save(), so callers using it must recompute().
        """
        self.recompute()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            kwargs['update_fields'] = set(update_fields) | {'strength', 'kd_ratio'}
        return super().save(*args, **kwargs)


class PlayerMonthStat(PlayerStat):
    """Per-player totals for one month, aggregated across servers. Kept forever."""

    month = models.CharField(max_length=7, db_index=True)  # "YYYY-MM", UTC

    class Meta:
        unique_together = ('month', 'player_name')
        indexes = [
            # One index per ranking sort mode, each carrying the tie-breakers so
            # the ordering is fully index-backed and stable across pages.
            models.Index(fields=['month', '-strength', '-kills', 'player_name'],
                         name='pms_month_strength_idx'),
            models.Index(fields=['month', '-kills', 'deaths', 'player_name'],
                         name='pms_month_kills_idx'),
            models.Index(fields=['month', '-kd_ratio', '-kills', 'player_name'],
                         name='pms_month_kd_idx'),
            models.Index(fields=['month', '-points', '-kills', 'player_name'],
                         name='pms_month_points_idx'),
        ]

    def __str__(self):
        return f'{self.player_name} ({self.month})'


class PlayerAllTimeStat(PlayerStat):
    """
    Per-player career totals, accumulated alongside the monthly rows.

    Maintained on write for the same reason the metrics are: summing the
    monthly table on every request would mean a GROUP BY over every month we
    have ever recorded, which only gets slower as the archive grows. This way
    the all-time board sorts and paginates exactly like a monthly one.
    """

    player_name = models.CharField(max_length=255, unique=True)

    class Meta:
        indexes = [
            models.Index(fields=['-strength', '-kills', 'player_name'],
                         name='pats_strength_idx'),
            models.Index(fields=['-kills', 'deaths', 'player_name'],
                         name='pats_kills_idx'),
            models.Index(fields=['-kd_ratio', '-kills', 'player_name'],
                         name='pats_kd_idx'),
            models.Index(fields=['-points', '-kills', 'player_name'],
                         name='pats_points_idx'),
        ]

    def __str__(self):
        return f'{self.player_name} (all time)'


class ServerActivitySample(models.Model):
    """
    Point-in-time population of a server, at collection resolution.

    These are the detail rows and they ARE pruned - see
    NP_RAW_SAMPLE_RETENTION_DAYS. They exist for recent, fine-grained views;
    the permanent record is ServerDailyStat, which is rolled up from them on
    write and never deleted.
    """

    server = models.ForeignKey(GameServer, on_delete=models.CASCADE)
    sampled_at = models.DateTimeField(default=timezone.now, db_index=True)
    num_players = models.IntegerField(default=0)

    def __str__(self):
        return f'{self.server}: {self.num_players} @ {self.sampled_at}'


class ServerDailyStat(models.Model):
    """
    One permanent row per server per day.

    This is the long-term history, so it is never pruned. A row is ~50 bytes,
    so a decade of a dozen servers is well under 50MB - cheap enough that
    throwing any of it away would be the wrong trade. Rolled up during ingest
    rather than by a batch job, so it cannot drift or fall behind.
    """

    server = models.ForeignKey(GameServer, on_delete=models.CASCADE)
    day = models.DateField(db_index=True)
    peak_players = models.IntegerField(default=0)
    sample_count = models.IntegerField(default=0)
    # Running total of every sample taken that day; average_players is derived
    # from it on write so the read path never has to divide across rows.
    total_players = models.IntegerField(default=0)
    average_players = models.FloatField(default=0)

    class Meta:
        unique_together = ('server', 'day')
        indexes = [
            models.Index(fields=['day', '-peak_players'], name='sds_day_peak_idx'),
        ]

    def record(self, num_players):
        """Fold one sample into the day."""
        self.peak_players = max(self.peak_players, num_players)
        self.sample_count += 1
        self.total_players += num_players
        self.average_players = self.total_players / self.sample_count

    def __str__(self):
        return f'{self.server} on {self.day}: peak {self.peak_players}'


class GameServerAdmin(admin.ModelAdmin):
    list_display = ('name', 'address', 'port', 'last_seen',)
    search_fields = ('name', 'address',)


class PlayerMonthStatAdmin(admin.ModelAdmin):
    list_display = ('month', 'player_name', 'kills', 'deaths', 'points', 'last_seen',)
    list_filter = ('month',)
    search_fields = ('player_name',)


class PlayerAllTimeStatAdmin(admin.ModelAdmin):
    list_display = ('player_name', 'kills', 'deaths', 'points', 'first_seen', 'last_seen',)
    search_fields = ('player_name',)


class ServerDailyStatAdmin(admin.ModelAdmin):
    list_display = ('day', 'server', 'peak_players', 'average_players', 'sample_count',)
    list_filter = ('server',)


class PlayerRoundStateAdmin(admin.ModelAdmin):
    list_display = ('player_name', 'server', 'last_kills', 'last_deaths', 'updated_at',)
    search_fields = ('player_name',)


admin.site.register(GameServer, GameServerAdmin)
admin.site.register(PlayerMonthStat, PlayerMonthStatAdmin)
admin.site.register(PlayerAllTimeStat, PlayerAllTimeStatAdmin)
admin.site.register(PlayerRoundState, PlayerRoundStateAdmin)
admin.site.register(ServerDailyStat, ServerDailyStatAdmin)
