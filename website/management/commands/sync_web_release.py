"""Install the exact CI build pinned by the website commit, before starting ASGI."""
import json
from pathlib import Path
import re
import tempfile
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from website.web_releases import activate, install, REVISION, MAX_BYTES


class Command(BaseCommand):
    help = 'Download and verify the browser build pinned in web-release.json.'

    def handle(self, *args, **options):
        pin_path = settings.BASE_DIR / 'web-release.json'
        if not pin_path.is_file():
            self.stdout.write('No browser release pinned; skipping download.')
            return
        pin = json.loads(pin_path.read_text())
        revision, checksum = pin.get('release', ''), pin.get('sha256', '')
        if not REVISION.fullmatch(revision) or not re.fullmatch('[0-9a-f]{64}', checksum):
            raise CommandError('Invalid web-release.json')
        root = Path(settings.NP_WEB_ROOT)
        receipt = root / 'releases' / revision / 'archive.sha256'
        if receipt.is_file() and receipt.read_text().strip() == checksum:
            activate(root, revision)
            self.stdout.write('Browser release already installed: ' + revision)
            return
        url = f'https://github.com/netpanzer/netpanzer/releases/download/web-{revision}/netpanzer-web-{revision}.tar.gz'
        root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix='.download-', dir=root) as work:
                archive = Path(work) / 'release.tar.gz'
                with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as target:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError('Release download exceeds size limit')
                        target.write(chunk)
                install(archive, checksum, root)
        except (OSError, ValueError) as error:
            raise CommandError(f'Browser release install failed; keeping previous files: {error}') from error
        self.stdout.write('Browser release installed: ' + revision)
