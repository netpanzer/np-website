"""Deployment sanity checks, run by `manage.py check` and by `migrate`."""

from django.conf import settings
from django.core.checks import Warning, register


@register()
def stats_collect_token_is_set(app_configs, **kwargs):
    """
    Surface a missing STATS_COLLECT_TOKEN at deploy time.

    The endpoint itself already fails closed when this is unset outside DEBUG,
    so the risk is not an open endpoint - it is silently stopping collection
    and only finding out when the ranking never fills in. run.sh calls migrate,
    which runs system checks, so this shows up in the deploy log.
    """
    if settings.DEBUG or settings.STATS_COLLECT_TOKEN:
        return []

    return [
        Warning(
            'STATS_COLLECT_TOKEN is not set, so /api/v1/stats/collect will '
            'refuse to run and no ranking or statistics data will be recorded.',
            hint='Set STATS_COLLECT_TOKEN in the environment to the same value '
                 'the collector job sends.',
            id='website.W001',
        )
    ]
