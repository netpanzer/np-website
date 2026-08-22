from django.core.management.base import BaseCommand

from website import services


class Command(BaseCommand):
    help = 'Query the master server and fold the current snapshot into the stats.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Ignore the throttle and collect immediately.',
        )

    def handle(self, *args, **options):
        payload = services.get_live_servers(force=True)

        if payload.get('error'):
            self.stderr.write(self.style.ERROR(
                f'Could not reach the master server: {payload["error"]}'
            ))
            return

        online = [s for s in payload['servers'] if s['online']]
        self.stdout.write(
            f'{payload["count"]} server(s) listed, {len(online)} responding, '
            f'{payload["total_players"]} player(s) online'
        )
        for server in payload['servers']:
            if server['online']:
                self.stdout.write(
                    f'  {server["address"]}:{server["port"]}  {server["name"]}  '
                    f'[{server.get("map", "")}] {server["num_players"]}/{server["max_players"]} '
                    f'{server.get("ping_ms", 0)}ms'
                )
            else:
                self.stdout.write(
                    f'  {server["address"]}:{server["port"]}  (no response)'
                )

        summary = services.ingest(payload['servers'])
        self.stdout.write(self.style.SUCCESS(
            f'Ingested into {summary["month"]}: {summary["players"]} player row(s), '
            f'+{summary["kills_added"]} kills, pruned {summary["pruned_samples"]} old sample(s)'
        ))
