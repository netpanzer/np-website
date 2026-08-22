import inspect
import threading
import time
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from website import checks, services, views
from website.gamequery import (
    MAX_FIELD_LENGTH,
    MAX_PLAYERS,
    parse_master_list,
    parse_status,
)
from website.models import (
    GameServer,
    PlayerAllTimeStat,
    PlayerMonthStat,
    PlayerRoundState,
    ServerActivitySample,
    ServerDailyStat,
)

# Captured verbatim from netpanzer.io:28900.
MASTER_REPLY = '\\ip\\69.164.193.165\\port\\3031\\ip\\168.138.247.215\\port\\3031\\final\\'

# Captured verbatim from 69.164.193.165:3031, a populated Objective server.
STATUS_POPULATED = (
    'gamename\\netpanzer\\protocol\\1128\\hostname\\WinrickLabs Large Dallas TX'
    '\\gameversion\\0.9.0\\mapname\\Two clans\\mapcycle\\Bad Neuburg,Bullet Hole'
    '\\mapstyle\\SummerDay\\authentication\\n\\password\\n\\numplayers\\2\\maxplayers\\100'
    '\\gamestyle\\Objective\\units_per_player\\500\\time\\4\\timelimit\\120\\fraglimit\\5000'
    '\\objectivelimit\\7'
    '\\player_0\\Player613\\kills_0\\0\\deaths_0\\0\\score_0\\5\\points_0\\0\\flag_0\\0\\flagu_0\\4'
    '\\player_1\\Player613(1)\\kills_1\\3\\deaths_1\\1\\score_1\\2\\points_1\\7\\flag_1\\1\\flagu_1\\4'
    '\\final\\'
)

# Captured verbatim from 168.138.247.215:3031, an empty Frag Limit server.
STATUS_EMPTY = (
    'gamename\\netpanzer\\protocol\\1128\\hostname\\NETPANZER.COM.BR\\gameversion\\0.9.0-RC6'
    '\\mapname\\Two Clans\\mapcycle\\Two Clans\\mapstyle\\SummerDay\\authentication\\n'
    '\\password\\n\\numplayers\\0\\maxplayers\\10\\empty\\1\\gamestyle\\Frag Limit'
    '\\units_per_player\\30\\time\\446396\\timelimit\\120\\fraglimit\\5000\\objectivelimit\\7\\final\\'
)


class ParseMasterListTest(TestCase):
    def test_parses_real_reply(self):
        self.assertEqual(
            parse_master_list(MASTER_REPLY),
            [('69.164.193.165', 3031), ('168.138.247.215', 3031)],
        )

    def test_drops_trailing_incomplete_pair(self):
        truncated = '\\ip\\1.2.3.4\\port\\3031\\ip\\5.6.7.8\\port\\'
        self.assertEqual(parse_master_list(truncated), [('1.2.3.4', 3031)])

    def test_deduplicates(self):
        duplicated = '\\ip\\1.2.3.4\\port\\3031\\ip\\1.2.3.4\\port\\3031\\final\\'
        self.assertEqual(parse_master_list(duplicated), [('1.2.3.4', 3031)])

    def test_empty_reply(self):
        self.assertEqual(parse_master_list(''), [])
        self.assertEqual(parse_master_list('\\final\\'), [])


class ParseStatusTest(TestCase):
    def test_parses_populated_server(self):
        status = parse_status(STATUS_POPULATED)

        self.assertEqual(status['hostname'], 'WinrickLabs Large Dallas TX')
        self.assertEqual(status['mapname'], 'Two clans')
        self.assertEqual(status['gamestyle'], 'Objective')
        self.assertEqual(status['gameversion'], '0.9.0')
        self.assertEqual(status['protocol'], 1128)
        self.assertEqual(status['numplayers'], 2)
        self.assertEqual(status['maxplayers'], 100)
        self.assertIs(status['password'], False)
        self.assertIs(status['authentication'], False)

        self.assertEqual(
            status['players'],
            [
                {'name': 'Player613', 'kills': 0, 'deaths': 0, 'score': 5, 'points': 0},
                {'name': 'Player613(1)', 'kills': 3, 'deaths': 1, 'score': 2, 'points': 7},
            ],
        )

    def test_parses_empty_server(self):
        status = parse_status(STATUS_EMPTY)

        self.assertEqual(status['hostname'], 'NETPANZER.COM.BR')
        self.assertEqual(status['numplayers'], 0)
        self.assertEqual(status['maxplayers'], 10)
        self.assertEqual(status['gamestyle'], 'Frag Limit')
        self.assertEqual(status['players'], [])

    def test_player_names_containing_underscores(self):
        # "player_0" splits on the last underscore, so an underscored name is safe.
        raw = '\\hostname\\x\\player_0\\my_name_here\\kills_0\\4\\final\\'
        status = parse_status(raw)
        self.assertEqual(status['players'][0]['name'], 'my_name_here')
        self.assertEqual(status['players'][0]['kills'], 4)

    def test_truncated_reply_does_not_raise(self):
        status = parse_status(STATUS_POPULATED[:120])
        self.assertEqual(status['hostname'], 'WinrickLabs Large Dallas TX')

    def test_password_and_auth_flags(self):
        status = parse_status('\\authentication\\y\\password\\y\\final\\')
        self.assertIs(status['authentication'], True)
        self.assertIs(status['password'], True)


def snapshot(players, address='1.2.3.4', port=3031):
    return [{
        'address': address,
        'port': port,
        'online': True,
        'name': 'Test Server',
        'num_players': len(players),
        'players': players,
    }]


def player(name, kills=0, deaths=0, score=0, points=0):
    return {'name': name, 'kills': kills, 'deaths': deaths, 'score': score, 'points': points}


class IngestTest(TestCase):
    MONTH = '2026-08'

    def ingest(self, players):
        return services.ingest(snapshot(players), month=self.MONTH)

    def stat(self, name):
        return PlayerMonthStat.objects.get(month=self.MONTH, player_name=name)

    def test_first_sighting_counts_existing_totals(self):
        self.ingest([player('Ada', kills=5, deaths=2, points=3)])

        stat = self.stat('Ada')
        self.assertEqual((stat.kills, stat.deaths, stat.points), (5, 2, 3))

    def test_normal_increment(self):
        self.ingest([player('Ada', kills=5, deaths=2, points=3)])
        self.ingest([player('Ada', kills=8, deaths=2, points=6)])

        stat = self.stat('Ada')
        self.assertEqual((stat.kills, stat.deaths, stat.points), (8, 2, 6))

    def test_unchanged_snapshot_adds_nothing(self):
        self.ingest([player('Ada', kills=5)])
        self.ingest([player('Ada', kills=5)])
        self.ingest([player('Ada', kills=5)])

        self.assertEqual(self.stat('Ada').kills, 5)

    def test_round_reset_counts_new_value_as_increment(self):
        self.ingest([player('Ada', kills=10, deaths=4)])
        # Map change: the server's counters drop back down.
        self.ingest([player('Ada', kills=2, deaths=1)])

        stat = self.stat('Ada')
        self.assertEqual((stat.kills, stat.deaths), (12, 5))

    def test_falling_points_are_not_mistaken_for_a_round_reset(self):
        # Points can drop mid-round. Treating that as a reset would re-credit
        # the whole board every time someone lost points.
        self.ingest([player('Ada', kills=4, points=10)])
        self.ingest([player('Ada', kills=4, points=6)])

        stat = self.stat('Ada')
        self.assertEqual((stat.kills, stat.points), (4, 6))

    def test_points_can_end_up_negative(self):
        self.ingest([player('Ada', kills=1, deaths=9, points=0)])
        self.ingest([player('Ada', kills=1, deaths=9, points=-7)])

        self.assertEqual(self.stat('Ada').points, -7)

    def test_reset_credits_a_negative_round_score(self):
        self.ingest([player('Ada', kills=5, deaths=2, points=8)])
        # New round, and this one went badly.
        self.ingest([player('Ada', kills=1, deaths=6, points=-3)])

        stat = self.stat('Ada')
        self.assertEqual((stat.kills, stat.deaths, stat.points), (6, 8, 5))

    def test_player_leaves_and_returns(self):
        self.ingest([player('Ada', kills=6)])
        self.ingest([])
        # Rejoins with a fresh scoreboard.
        self.ingest([player('Ada', kills=3)])

        self.assertEqual(self.stat('Ada').kills, 9)

    def test_totals_aggregate_across_servers(self):
        services.ingest(snapshot([player('Ada', kills=4)], address='1.1.1.1'), month=self.MONTH)
        services.ingest(snapshot([player('Ada', kills=6)], address='2.2.2.2'), month=self.MONTH)

        self.assertEqual(self.stat('Ada').kills, 10)
        self.assertEqual(PlayerRoundState.objects.filter(player_name='Ada').count(), 2)
        self.assertEqual(GameServer.objects.count(), 2)

    def test_offline_servers_are_skipped(self):
        entries = snapshot([player('Ada', kills=4)])
        entries[0]['online'] = False

        summary = services.ingest(entries, month=self.MONTH)

        self.assertEqual(summary['servers'], 0)
        self.assertFalse(PlayerMonthStat.objects.exists())

    def test_blank_player_names_are_ignored(self):
        self.ingest([player('  ', kills=9)])
        self.assertFalse(PlayerMonthStat.objects.exists())


class LongTermHistoryTest(TestCase):
    """
    A tracker whose history expires is not a tracker. ServerDailyStat and the
    career totals must survive indefinitely, and the raw samples they are
    rolled up from must be the only thing that ever gets pruned.
    """

    MONTH = '2026-08'

    def test_daily_rollup_records_peak_and_average(self):
        for count in (2, 9, 4):
            entries = snapshot([])
            entries[0]['num_players'] = count
            services.ingest(entries, month=self.MONTH)

        daily = ServerDailyStat.objects.get()
        self.assertEqual(daily.peak_players, 9)
        self.assertEqual(daily.sample_count, 3)
        self.assertEqual(daily.total_players, 15)
        self.assertEqual(daily.average_players, 5.0)

    def test_pruning_raw_samples_leaves_the_history_intact(self):
        entries = snapshot([player('Ada', kills=3)])
        entries[0]['num_players'] = 7
        services.ingest(entries, month=self.MONTH)

        # Age the raw detail rows well past any retention window.
        old = timezone.now() - timezone.timedelta(days=4000)
        ServerActivitySample.objects.update(sampled_at=old)
        services.ingest(snapshot([]), month=self.MONTH)

        self.assertEqual(ServerActivitySample.objects.filter(sampled_at=old).count(), 0)
        # The permanent record and the player totals are untouched.
        self.assertEqual(ServerDailyStat.objects.get().peak_players, 7)
        self.assertEqual(PlayerMonthStat.objects.get(player_name='Ada').kills, 3)
        self.assertEqual(PlayerAllTimeStat.objects.get(player_name='Ada').kills, 3)

    def test_ingest_never_deletes_daily_history(self):
        ancient = timezone.now().date() - timezone.timedelta(days=4000)
        server = GameServer.objects.create(address='9.9.9.9', port=3031)
        ServerDailyStat.objects.create(
            server=server, day=ancient, peak_players=42, sample_count=1, total_players=42
        )

        services.ingest(snapshot([player('Ada')]), month=self.MONTH)

        self.assertTrue(ServerDailyStat.objects.filter(day=ancient, peak_players=42).exists())

    def test_all_time_totals_span_months(self):
        services.ingest(snapshot([player('Ada', kills=4, points=2)]), month='2026-07')
        # New month, and the counters kept climbing on the server.
        services.ingest(snapshot([player('Ada', kills=10, points=5)]), month='2026-08')

        self.assertEqual(PlayerMonthStat.objects.get(month='2026-07').kills, 4)
        self.assertEqual(PlayerMonthStat.objects.get(month='2026-08').kills, 6)
        career = PlayerAllTimeStat.objects.get(player_name='Ada')
        self.assertEqual(career.kills, 10)
        self.assertEqual(career.points, 5)

    def test_all_time_metrics_are_stored_not_summed(self):
        services.ingest(snapshot([player('Ada', kills=10, deaths=4, points=3)]), month='2026-08')

        career = PlayerAllTimeStat.objects.get(player_name='Ada')
        self.assertEqual(career.strength, 21.0)
        self.assertEqual(career.kd_ratio, 11 / 5)

    def test_all_time_ranking_is_served_from_the_career_table(self):
        services.ingest(snapshot([player('Ada', kills=3)]), month='2026-06')
        services.ingest(snapshot([player('Ada', kills=9)]), month='2026-07')
        services.ingest(snapshot([player('Bob', kills=5)], address='2.2.2.2'), month='2026-07')

        payload = self.client.get('/api/v1/ranking', {'month': 'all', 'sort': 'kills'}).json()

        self.assertEqual(payload['month'], 'all')
        self.assertEqual(payload['players'][0]['name'], 'Ada')
        self.assertEqual(payload['players'][0]['kills'], 9)

    def test_all_time_costs_the_same_however_many_months_exist(self):
        for index in range(24):
            PlayerMonthStat.objects.create(
                month=f'2025-{index % 12 + 1:02d}', player_name=f'P{index}', kills=index
            )
        PlayerAllTimeStat.objects.create(player_name='P0', kills=1)

        with CaptureQueriesContext(connection) as queries:
            self.client.get('/api/v1/ranking', {'month': 'all'})

        # No GROUP BY over the monthly archive.
        self.assertFalse(
            any('GROUP BY' in q['sql'].upper() for q in queries.captured_queries),
            'the all-time board must not aggregate the monthly table on read',
        )


class ActivityRangeTest(TestCase):
    """The activity summary must be able to look back further than a week."""

    def setUp(self):
        self.server = GameServer.objects.create(address='1.2.3.4', port=3031)
        today = timezone.now().date()
        # A big day two years ago, and a quiet one today.
        for days_ago, peak in ((730, 64), (0, 3)):
            ServerDailyStat.objects.create(
                server=self.server,
                day=today - timezone.timedelta(days=days_ago),
                peak_players=peak,
                sample_count=1,
                total_players=peak,
                average_players=peak,
            )

    def test_all_time_is_the_default_and_sees_old_history(self):
        response = self.client.get('/statistics')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['activity_range'], 'all')
        self.assertEqual(response.context['peak_players'], 64)

    def test_short_range_excludes_older_history(self):
        response = self.client.get('/statistics', {'range': '30d'})

        self.assertEqual(response.context['peak_players'], 3)

    def test_one_year_range_excludes_a_two_year_old_peak(self):
        response = self.client.get('/statistics', {'range': '1y'})

        self.assertEqual(response.context['peak_players'], 3)

    def test_unknown_range_falls_back(self):
        response = self.client.get('/statistics', {'range': 'forever'})

        self.assertEqual(response.context['activity_range'], 'all')

    def test_average_is_weighted_by_sample_count(self):
        # A day observed 10 times at 1 player must not outweigh one observed
        # once at 10 players.
        ServerDailyStat.objects.create(
            server=self.server,
            day=timezone.now().date() - timezone.timedelta(days=1),
            peak_players=1, sample_count=10, total_players=10, average_players=1.0,
        )

        response = self.client.get('/statistics')

        # (64 + 3 + 10) / (1 + 1 + 10)
        self.assertEqual(response.context['average_players'], round(77 / 12, 1))

    def test_reports_when_tracking_started(self):
        response = self.client.get('/statistics')

        self.assertEqual(
            response.context['tracking_since'],
            timezone.now().date() - timezone.timedelta(days=730),
        )


class UntrustedInputTest(TestCase):
    """
    Anyone can register a server with the master server and then answer our
    status query with anything. Nothing it sends may be able to break ingest:
    on a backend that enforces varchar widths, one oversized string would raise
    DataError and roll back the whole atomic snapshot, stalling collection for
    as long as that server stays listed.
    """

    MONTH = '2026-08'

    def field_width(self, model, field):
        return model._meta.get_field(field).max_length

    def test_parser_clamps_player_names(self):
        raw = '\\hostname\\x\\player_0\\' + 'A' * 5000 + '\\kills_0\\1\\final\\'
        status = parse_status(raw)

        self.assertEqual(len(status['players'][0]['name']), MAX_FIELD_LENGTH)

    def test_parser_clamps_scalar_fields(self):
        status = parse_status('\\hostname\\' + 'H' * 5000 + '\\final\\')

        self.assertEqual(len(status['hostname']), MAX_FIELD_LENGTH)

    def test_parser_caps_the_player_list(self):
        parts = []
        for index in range(MAX_PLAYERS + 50):
            parts.append(f'\\player_{index}\\P{index}\\kills_{index}\\1')
        status = parse_status(''.join(parts) + '\\final\\')

        self.assertLessEqual(len(status['players']), MAX_PLAYERS)

    def test_parser_clamps_master_list_hosts(self):
        servers = parse_master_list('\\ip\\' + 'h' * 5000 + '\\port\\3031\\final\\')

        self.assertEqual(len(servers[0][0]), MAX_FIELD_LENGTH)

    def test_ingest_survives_an_oversized_player_name(self):
        long_name = 'B' * 5000
        entries = snapshot([player(long_name, kills=3)])

        services.ingest(entries, month=self.MONTH)

        stat = PlayerMonthStat.objects.get()
        self.assertEqual(len(stat.player_name), self.field_width(PlayerMonthStat, 'player_name'))
        self.assertEqual(stat.kills, 3)
        # The round-state cursor must key on the same trimmed name, or every
        # later snapshot would look like a brand new player.
        self.assertEqual(PlayerRoundState.objects.get().player_name, stat.player_name)

    def test_repeat_ingest_of_an_oversized_name_does_not_double_count(self):
        long_name = 'C' * 5000
        services.ingest(snapshot([player(long_name, kills=4)]), month=self.MONTH)
        services.ingest(snapshot([player(long_name, kills=4)]), month=self.MONTH)

        self.assertEqual(PlayerMonthStat.objects.count(), 1)
        self.assertEqual(PlayerMonthStat.objects.get().kills, 4)

    def test_ingest_survives_an_oversized_server_name_and_address(self):
        entries = snapshot([player('Ada', kills=1)], address='D' * 5000)
        entries[0]['name'] = 'E' * 5000

        services.ingest(entries, month=self.MONTH)

        server = GameServer.objects.get()
        self.assertEqual(len(server.address), self.field_width(GameServer, 'address'))
        self.assertEqual(len(server.name), self.field_width(GameServer, 'name'))

    def test_nothing_written_exceeds_its_column_width(self):
        """
        The general invariant, rather than a list of fields I thought of.

        full_clean() enforces max_length the way a strict backend would, so if
        any ingested value is too wide for its column this fails here instead of
        as a DataError in production.
        """
        entries = snapshot(
            [player('F' * 4000, kills=1), player('G' * 300, kills=2)],
            address='H' * 4000,
        )
        entries[0]['name'] = 'I' * 4000

        services.ingest(entries, month=self.MONTH)

        for model in (GameServer, PlayerRoundState, PlayerMonthStat):
            for row in model.objects.all():
                with self.subTest(model=model.__name__, pk=row.pk):
                    # Only the width rules matter here; skip relational checks.
                    row.full_clean(exclude=['server'], validate_unique=False)

    def test_end_to_end_from_a_hostile_status_reply(self):
        # Straight from the wire format, not hand-built dicts.
        raw = (
            '\\hostname\\' + 'H' * 900 +
            '\\numplayers\\1\\maxplayers\\10'
            '\\player_0\\' + 'P' * 900 + '\\kills_0\\2\\deaths_0\\1\\points_0\\3\\final\\'
        )
        status = parse_status(raw)
        entry = {
            'address': '1.2.3.4',
            'port': 3031,
            'online': True,
            'name': status['hostname'],
            'num_players': status['numplayers'],
            'players': status['players'],
        }

        services.ingest([entry], month=self.MONTH)

        self.assertEqual(PlayerMonthStat.objects.get().kills, 2)


class DerivedMetricsTest(TestCase):
    def test_strength_and_kd_match_the_familiar_formulas(self):
        stat = PlayerMonthStat(month='2026-08', player_name='Ada', kills=10, deaths=4, points=3)
        stat.recompute()

        self.assertEqual(stat.strength, 2 * 10 + 3 - 0.5 * 4)
        self.assertEqual(stat.kd_ratio, 11 / 5)

    def test_saving_refreshes_the_metrics(self):
        # Any write path, not just ingest, must leave the columns consistent.
        stat = PlayerMonthStat.objects.create(
            month='2026-08', player_name='Ada', kills=10, deaths=4, points=3
        )
        self.assertEqual(stat.strength, 21.0)

        stat.kills = 20
        stat.save()
        self.assertEqual(PlayerMonthStat.objects.get(pk=stat.pk).strength, 41.0)

    def test_metrics_refresh_even_with_update_fields(self):
        stat = PlayerMonthStat.objects.create(month='2026-08', player_name='Ada', kills=1)
        stat.kills = 12
        stat.save(update_fields=['kills'])

        self.assertEqual(PlayerMonthStat.objects.get(pk=stat.pk).strength, 24.0)

    def test_ingest_stores_the_metrics(self):
        services.ingest(snapshot([player('Ada', kills=10, deaths=4, points=3)]), month='2026-08')

        stat = PlayerMonthStat.objects.get(player_name='Ada')
        self.assertEqual(stat.strength, 21.0)
        self.assertEqual(stat.kd_ratio, 11 / 5)

    def test_metrics_stay_in_step_with_later_ingests(self):
        services.ingest(snapshot([player('Ada', kills=2)]), month='2026-08')
        services.ingest(snapshot([player('Ada', kills=9, deaths=3, points=4)]), month='2026-08')

        stat = PlayerMonthStat.objects.get(player_name='Ada')
        expected = PlayerMonthStat(kills=stat.kills, deaths=stat.deaths, points=stat.points)
        expected.recompute()
        self.assertEqual(stat.strength, expected.strength)
        self.assertEqual(stat.kd_ratio, expected.kd_ratio)


class ViewTest(TestCase):
    def test_ranking_page_renders_without_data(self):
        response = self.client.get('/ranking')
        self.assertEqual(response.status_code, 200)

    def test_statistics_page_renders_without_data(self):
        response = self.client.get('/statistics')
        self.assertEqual(response.status_code, 200)

    def test_ranking_orders_by_requested_mode(self):
        PlayerMonthStat.objects.create(month='2026-08', player_name='Sniper', kills=50, deaths=1)
        PlayerMonthStat.objects.create(month='2026-08', player_name='Grinder', kills=60, deaths=90)

        response = self.client.get('/api/v1/ranking', {'month': '2026-08', 'sort': 'kills'})
        self.assertEqual(response.json()['players'][0]['name'], 'Grinder')

        response = self.client.get('/api/v1/ranking', {'month': '2026-08', 'sort': 'kd'})
        self.assertEqual(response.json()['players'][0]['name'], 'Sniper')

    def test_ranking_rejects_unknown_sort_mode(self):
        response = self.client.get('/api/v1/ranking', {'month': '2026-08', 'sort': 'bogus'})
        self.assertEqual(response.json()['sort'], 'strength')

    def test_ranking_rejects_malformed_month(self):
        for bad in ('not-a-month', '2026-13', '2026', "2026-08' OR 1=1"):
            response = self.client.get('/api/v1/ranking', {'month': bad})
            self.assertNotEqual(response.json()['month'], bad)


class PaginationCostTest(TestCase):
    """
    The read path must not scale with the size of the month.

    It previously loaded every row for the month and sorted in Python, so a
    deep page cost the same as the first one and the JSON endpoint serialised
    everything. These lock that in.
    """

    MONTH = '2026-08'
    TOTAL = 300

    def setUp(self):
        PlayerMonthStat.objects.bulk_create([
            self._stat(index) for index in range(self.TOTAL)
        ])

    def _stat(self, index):
        stat = PlayerMonthStat(
            month=self.MONTH,
            player_name=f'Player{index:03d}',
            kills=index,
            deaths=index % 7,
            points=index % 11,
        )
        stat.recompute()
        return stat

    def test_a_page_loads_only_its_own_rows(self):
        for page in (1, 12):
            with self.subTest(page=page):
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(
                        '/ranking', {'month': self.MONTH, 'sort': 'kills', 'page': page}
                    )
                self.assertEqual(response.status_code, 200)
                # A COUNT plus one LIMIT/OFFSET fetch, regardless of depth.
                selects = [q['sql'] for q in queries.captured_queries if 'SELECT' in q['sql']]
                limited = [sql for sql in selects if 'LIMIT' in sql]
                self.assertTrue(limited, 'expected a LIMIT-ed query')
                self.assertTrue(
                    all(f'LIMIT {self.TOTAL}' not in sql for sql in limited),
                    'a page should not fetch the whole month',
                )

    def test_deep_page_costs_the_same_as_the_first(self):
        with CaptureQueriesContext(connection) as first:
            self.client.get('/ranking', {'month': self.MONTH, 'page': 1})
        with CaptureQueriesContext(connection) as deep:
            self.client.get('/ranking', {'month': self.MONTH, 'page': 12})

        self.assertEqual(len(first.captured_queries), len(deep.captured_queries))

    def test_ranking_ordering_is_correct_across_pages(self):
        first = self.client.get(
            '/api/v1/ranking', {'month': self.MONTH, 'sort': 'kills', 'per_page': 100}
        ).json()
        second = self.client.get(
            '/api/v1/ranking',
            {'month': self.MONTH, 'sort': 'kills', 'per_page': 100, 'page': 2},
        ).json()

        self.assertEqual(first['count'], self.TOTAL)
        self.assertEqual(len(first['players']), 100)
        # Highest kills first, ranks continuous, no overlap between pages.
        self.assertEqual(first['players'][0]['kills'], self.TOTAL - 1)
        self.assertEqual(first['players'][0]['rank'], 1)
        self.assertEqual(second['players'][0]['rank'], 101)
        self.assertEqual(second['players'][0]['kills'], self.TOTAL - 101)

    def test_api_caps_page_size(self):
        response = self.client.get(
            '/api/v1/ranking', {'month': self.MONTH, 'per_page': 100000}
        ).json()

        self.assertEqual(response['per_page'], 100)
        self.assertEqual(len(response['players']), 100)

    def test_api_tolerates_junk_page_size(self):
        response = self.client.get(
            '/api/v1/ranking', {'month': self.MONTH, 'per_page': 'lots'}
        ).json()

        self.assertEqual(response['per_page'], 25)

    def test_search_is_filtered_in_the_database(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                '/api/v1/ranking', {'month': self.MONTH, 'q': 'Player01'}
            ).json()

        self.assertEqual(response['count'], 10)  # Player010..Player019
        self.assertTrue(
            any('LIKE' in q['sql'].upper() for q in queries.captured_queries),
            'search should be a database filter, not a Python one',
        )

    def test_statistics_podium_is_the_month_top_three(self):
        response = self.client.get('/statistics', {'month': self.MONTH, 'page': 5})

        self.assertEqual(response.status_code, 200)
        podium = [row['stat'].player_name for row in response.context['top_three']]
        top = list(
            PlayerMonthStat.objects.filter(month=self.MONTH)
            .order_by('-strength', '-kills', 'player_name')
            .values_list('player_name', flat=True)[:3]
        )
        self.assertEqual(podium, top)

    def test_collect_endpoint_rejects_get(self):
        self.assertEqual(self.client.get('/api/v1/stats/collect').status_code, 405)


class CollectEndpointSecurityTest(TestCase):
    """
    This endpoint is @csrf_exempt and writes to the database, so it must never
    end up reachable by accident.
    """

    URL = '/api/v1/stats/collect'

    def post(self, token=None):
        headers = {'x-collect-token': token} if token is not None else {}
        return self.client.post(self.URL, headers=headers)

    def test_requires_the_token_when_configured(self):
        with self.settings(STATS_COLLECT_TOKEN='secret'):
            self.assertEqual(self.post().status_code, 403)
            self.assertEqual(self.post('wrong').status_code, 403)
            self.assertEqual(self.post('secret' + 'x').status_code, 403)
            self.assertEqual(self.post('secre').status_code, 403)

    def test_fails_closed_when_the_token_is_missing_in_production(self):
        # A deploy that forgets the env var must not leave a public write
        # endpoint behind.
        with self.settings(STATS_COLLECT_TOKEN='', DEBUG=False):
            response = self.post()

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['ok'])
        # Nothing was written.
        self.assertFalse(GameServer.objects.exists())

    def test_open_only_under_debug(self):
        with self.settings(STATS_COLLECT_TOKEN='', DEBUG=True):
            with mock.patch.object(services, 'collect', return_value=(True, {'servers': 0})):
                response = self.post()

        self.assertEqual(response.status_code, 200)

    def test_token_comparison_is_constant_time(self):
        # hmac.compare_digest, not ==, so response timing cannot leak the token
        # a byte at a time.
        source = inspect.getsource(views.api_v1_stats_collect)
        self.assertIn('compare_digest', source)

    def test_non_ascii_token_does_not_error(self):
        with self.settings(STATS_COLLECT_TOKEN='paßwort-é'):
            self.assertEqual(self.post('wrong').status_code, 403)

    def test_system_check_warns_when_unconfigured(self):
        with self.settings(STATS_COLLECT_TOKEN='', DEBUG=False):
            self.assertEqual(
                [w.id for w in checks.stats_collect_token_is_set(None)], ['website.W001']
            )
        with self.settings(STATS_COLLECT_TOKEN='set', DEBUG=False):
            self.assertEqual(checks.stats_collect_token_is_set(None), [])


class CollectConcurrencyTest(TestCase):
    """
    A collection is seconds of network IO. A second caller must be told it is
    throttled immediately rather than queueing behind the first, which would
    tie up a worker to reach the same answer.
    """

    def setUp(self):
        services.reset_throttle()

    def tearDown(self):
        services.reset_throttle()

    def test_second_caller_does_not_block_on_the_first(self):
        started = threading.Event()
        release = threading.Event()

        def slow_fetch():
            started.set()
            release.wait(5)
            return []

        results = {}

        # ingest is stubbed out: this is about lock behaviour, and SQLite will
        # not take a write from a second thread inside the test transaction.
        with mock.patch.object(services, '_fetch_servers', side_effect=slow_fetch), \
                mock.patch.object(services, 'ingest', return_value={'servers': 0}):
            first = threading.Thread(target=lambda: results.setdefault('first', services.collect()))
            first.start()
            self.assertTrue(started.wait(5), 'first collection never started')

            # The first is mid-fetch and holding the lock.
            began = time.monotonic()
            ran, summary = services.collect()
            waited = time.monotonic() - began

            release.set()
            first.join(5)

        self.assertFalse(ran)
        self.assertTrue(summary['throttled'])
        self.assertLess(waited, 0.5, 'the second caller queued behind the running collection')
        self.assertTrue(results['first'][0])

    def test_throttles_repeat_calls_after_one_succeeds(self):
        with mock.patch.object(services, '_fetch_servers', return_value=[]):
            self.assertTrue(services.collect()[0])
            ran, summary = services.collect()

        self.assertFalse(ran)
        self.assertTrue(summary['throttled'])
        self.assertIn('retry_in', summary)

    def test_lock_is_released_when_a_collection_fails(self):
        with mock.patch.object(services, '_fetch_servers', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                services.collect()

        # A later caller must still be able to acquire the lock.
        with mock.patch.object(services, '_fetch_servers', return_value=[]):
            self.assertTrue(services.collect(force=True)[0])


class LiveCacheConcurrencyTest(TestCase):
    """The read path must not queue behind an in-flight refresh either."""

    def setUp(self):
        services.reset_cache()

    def tearDown(self):
        services.reset_cache()

    def test_reader_gets_stale_data_instead_of_waiting(self):
        with mock.patch.object(services, '_fetch_servers', return_value=[]):
            services.get_live_servers(force=True)  # prime the cache

        started = threading.Event()
        release = threading.Event()

        def slow_fetch():
            started.set()
            release.wait(5)
            return []

        # TTL of 0 so the reader genuinely wants a refresh and has to decide
        # between waiting for the one in flight and serving what it has.
        with self.settings(NP_LIVE_CACHE_SECONDS=0), \
                mock.patch.object(services, '_fetch_servers', side_effect=slow_fetch):
            refresher = threading.Thread(target=lambda: services.get_live_servers(force=True))
            refresher.start()
            self.assertTrue(started.wait(5), 'refresh never started')

            began = time.monotonic()
            payload = services.get_live_servers(force=False)
            waited = time.monotonic() - began

            release.set()
            refresher.join(5)

        self.assertTrue(payload['stale'])
        self.assertLess(waited, 0.5, 'the reader queued behind the in-flight refresh')

    def test_cold_cache_waits_rather_than_returning_nothing(self):
        with mock.patch.object(
            services, '_fetch_servers',
            return_value=[{'address': '1.2.3.4', 'port': 3031, 'online': True,
                           'name': 'S', 'num_players': 1, 'players': []}],
        ):
            payload = services.get_live_servers()

        self.assertEqual(payload['count'], 1)
        self.assertFalse(payload['stale'])
