from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from website.web_releases import install, activate


class Command(BaseCommand):
    help = 'Verify/install browser assets, or atomically roll back to an installed revision.'

    def add_arguments(self, parser):
        parser.add_argument('archive', nargs='?')
        parser.add_argument('--sha256')
        parser.add_argument('--activate', metavar='REVISION')
        parser.add_argument('--no-activate', action='store_true')

    def handle(self, *args, **options):
        try:
            if options['activate']:
                if options['archive']:
                    raise ValueError('Choose an archive or --activate, not both')
                activate(settings.NP_WEB_ROOT, options['activate'])
                revision = options['activate']
            else:
                if not options['archive'] or not options['sha256']:
                    raise ValueError('An archive and --sha256 are required')
                revision = install(options['archive'], options['sha256'], settings.NP_WEB_ROOT, not options['no_activate'])
        except (OSError, ValueError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS('Browser release ready: ' + revision))
