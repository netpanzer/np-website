import hmac
import logging
import re

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, Max, Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from website import services
from website.web_releases import active_release
from website.models import (
    Announcement,
    PlayerAllTimeStat,
    PlayerMonthStat,
    ServerDailyStat,
)

logger = logging.getLogger(__name__)

# Ranking sort modes, with the blurb shown under the tabs.
SORT_MODES = {
    'strength': 'Sorts by a composite index: 2*kills + points - 0.5*deaths. '
                'Balances kills and points while penalising deaths.',
    'kills': 'Sorts by total kills, highest first.',
    'kd': 'Sorts by (kills+1)/(deaths+1) to favour efficiency while avoiding division by zero.',
    'points': 'Sorts by total points scored.',
}

# Each ordering is backed by a matching index on PlayerMonthStat, tie-breakers
# included, so the database can sort and return a single page.
SORT_FIELDS = {
    'strength': ('-strength', '-kills', 'player_name'),
    'kills': ('-kills', 'deaths', 'player_name'),
    'kd': ('-kd_ratio', '-kills', 'player_name'),
    'points': ('-points', '-kills', 'player_name'),
}

DEFAULT_SORT = 'strength'
PAGE_SIZE = 25

# Ceiling for the public JSON endpoint, so one request cannot ask the single
# worker to serialise an entire month.
API_MAX_PAGE_SIZE = 100

MONTH_PATTERN = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')

# The leaderboard period. "all" reads the career table, which is maintained on
# write, so looking back over every month we have ever recorded costs the same
# as looking at one of them.
ALL_TIME = 'all'

# Window for the activity summary. None means the whole archive - a tracker
# that cannot show you more than the last week is not much of a tracker.
ACTIVITY_RANGES = {
    '30d': ('Last 30 days', 30),
    '90d': ('Last 90 days', 90),
    '1y': ('Last year', 365),
    'all': ('All time', None),
}
DEFAULT_ACTIVITY_RANGE = 'all'


def index(request):
    return render(request, 'index.html')


def play(request):
    release = active_release()
    response = render(request, 'play.html', {'game_release': release}, status=200 if release else 503)
    response['Cache-Control'] = 'no-store'
    return response


def history(request):
    return render(request, 'history.html')


def servers(request):
    payload = services.get_live_servers()
    return render(request, 'servers.html', {'live': payload})


def _ranking_queryset(month, sort, search):
    """
    Ranking rows for a period as an unevaluated queryset.

    Ordering happens in the database against a covering index, so the caller can
    slice out one page instead of materialising the period. "all" reads the
    career table rather than aggregating the monthly archive, so it stays just
    as cheap however many years accumulate.
    """
    if month == ALL_TIME:
        stats = PlayerAllTimeStat.objects.all()
    else:
        stats = PlayerMonthStat.objects.filter(month=month)

    if search:
        stats = stats.filter(player_name__icontains=search)
    return stats.order_by(*SORT_FIELDS[sort])


def _available_months():
    return list(
        PlayerMonthStat.objects
        .values_list('month', flat=True)
        .distinct()
        .order_by('-month')
    )


def _ranking_context(request):
    """Shared query handling for the ranking and statistics pages."""
    months = _available_months()

    month = (request.GET.get('month') or '').strip()
    if month != ALL_TIME and not MONTH_PATTERN.match(month):
        month = ALL_TIME

    sort = request.GET.get('sort', DEFAULT_SORT)
    if sort not in SORT_FIELDS:
        sort = DEFAULT_SORT

    # Bounded so a long query string cannot turn into a large LIKE scan.
    search = (request.GET.get('q') or '').strip()[:64]

    return {
        'month': month,
        'months': months,
        'all_time': ALL_TIME,
        'is_all_time': month == ALL_TIME,
        'period_label': 'all time' if month == ALL_TIME else month,
        'sort': sort,
        'sort_modes': SORT_MODES,
        'sort_description': SORT_MODES[sort],
        'search': search,
        'stats': _ranking_queryset(month, sort, search),
    }


MEDALS = {1: '\U0001F3C6', 2: '\U0001F948', 3: '\U0001F949'}


def _paginate(context, request, page_size=PAGE_SIZE):
    """
    Slice out one page, keeping ranks continuous across pages.

    Paginator issues a COUNT and a LIMIT/OFFSET against the queryset, so only
    the rows on this page are loaded.
    """
    paginator = Paginator(context['stats'], page_size)
    page = paginator.get_page(request.GET.get('page'))
    offset = page.start_index() - 1

    context['page'] = page
    context['total_players'] = paginator.count
    context['rows'] = [
        {'rank': offset + index + 1, 'medal': MEDALS.get(offset + index + 1), 'stat': stat}
        for index, stat in enumerate(page.object_list)
    ]
    return context


def ranking(request):
    return render(request, 'ranking.html', _paginate(_ranking_context(request), request))


def statistics(request):
    context = _ranking_context(request)

    # Its own small query rather than a slice of the paginated page, so the
    # podium is the month's top three regardless of which page is shown.
    context['top_three'] = [
        {'rank': index + 1, 'medal': MEDALS.get(index + 1), 'stat': stat}
        for index, stat in enumerate(
            _ranking_queryset(context['month'], 'strength', context['search'])[:3]
        )
    ]

    _paginate(context, request)
    context.update(_activity_summary(request))

    return render(request, 'statistics.html', context)


def _activity_summary(request):
    """
    Server population over the selected window, from the permanent daily rows.

    Reads ServerDailyStat rather than the raw samples, so the window can span
    years without depending on detail rows that get pruned.
    """
    key = request.GET.get('range', DEFAULT_ACTIVITY_RANGE)
    if key not in ACTIVITY_RANGES:
        key = DEFAULT_ACTIVITY_RANGE
    label, days = ACTIVITY_RANGES[key]

    daily = ServerDailyStat.objects.all()
    if days is not None:
        daily = daily.filter(day__gte=(timezone.now().date() - timezone.timedelta(days=days)))

    summary = daily.aggregate(
        peak=Max('peak_players'),
        # Weighted by how many samples each day actually contributed, so a day
        # we only partly observed does not count as much as a full one.
        total=Sum('total_players'),
        samples=Sum('sample_count'),
        days=Count('day', distinct=True),
    )

    samples = summary['samples'] or 0
    busiest = daily.order_by('-peak_players', 'day').first()

    return {
        'activity_range': key,
        'activity_range_label': label,
        'activity_ranges': ACTIVITY_RANGES,
        'peak_players': summary['peak'] or 0,
        'average_players': round((summary['total'] or 0) / samples, 1) if samples else 0,
        'days_tracked': summary['days'] or 0,
        'busiest_day': busiest,
        'tracking_since': ServerDailyStat.objects.order_by('day').values_list('day', flat=True).first(),
    }


def clans(request):
    return render(request, 'clans.html')


def blog(request):
    return render(request, 'blog.html')


def downloads(request):
    return render(request, 'downloads.html')


def chat(request):
    return render(request, 'chat.html')


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


def api_v1_servers(request: HttpRequest):
    """Public JSON view of the live server list. Read-only, no database work."""
    return JsonResponse(services.get_live_servers())


def api_v1_ranking(request: HttpRequest):
    """Paginated ranking. One page is loaded per request, never a whole month."""
    context = _ranking_context(request)

    try:
        page_size = int(request.GET.get('per_page', PAGE_SIZE))
    except ValueError:
        page_size = PAGE_SIZE
    page_size = max(1, min(page_size, API_MAX_PAGE_SIZE))

    _paginate(context, request, page_size=page_size)
    page = context['page']

    return JsonResponse({
        'month': context['month'],
        'sort': context['sort'],
        'count': page.paginator.count,
        'page': page.number,
        'pages': page.paginator.num_pages,
        'per_page': page_size,
        'players': [
            {
                'rank': row['rank'],
                'name': row['stat'].player_name,
                'kills': row['stat'].kills,
                'deaths': row['stat'].deaths,
                'score': row['stat'].score,
                'points': row['stat'].points,
                'strength': row['stat'].strength,
                'kd_ratio': round(row['stat'].kd_ratio, 3),
            }
            for row in context['rows']
        ],
    })


@csrf_exempt
@require_POST
def api_v1_stats_collect(request: HttpRequest):
    """
    Cron entry point: query the servers and fold the snapshot into the stats.

    Throttled process-wide, so an extra caller costs nothing.
    """
    expected = settings.STATS_COLLECT_TOKEN

    if not expected:
        # Fail closed. This endpoint is @csrf_exempt and writes to the
        # database, so a deploy that simply forgot the env var must not turn it
        # into an open write endpoint. Only an explicit local DEBUG run is
        # allowed to skip the token.
        if not settings.DEBUG:
            logger.error(
                'STATS_COLLECT_TOKEN is not set; refusing to run collection. '
                'Set it in the environment to enable this endpoint.'
            )
            return JsonResponse({'ok': False, 'error': 'collection is not configured'}, status=503)
        logger.warning('STATS_COLLECT_TOKEN is not set; collect endpoint is open (DEBUG only)')
    else:
        provided = request.headers.get('X-Collect-Token', '')
        # Constant time, so the response latency cannot be used to recover the
        # token a byte at a time. Compared as bytes so a non-ASCII token works.
        if not hmac.compare_digest(provided.encode('utf-8'), expected.encode('utf-8')):
            return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    try:
        ran, summary = services.collect()
    except OSError as error:
        logger.warning('Stats collection failed: %s', error)
        return JsonResponse({'ok': False, 'error': str(error)}, status=502)

    return JsonResponse({'ok': True, 'ran': ran, **summary})
