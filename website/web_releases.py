"""Verified immutable browser releases, stored outside the rsync deploy tree."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import uuid

from django.conf import settings

REVISION = re.compile(r'^[0-9a-f]{40}$')
CLIENT_FILES = {'index.html', 'launcher.js', 'netpanzer.js', 'netpanzer.wasm', 'netpanzer.data', 'COPYING.txt'}
REQUIRED = {'client/' + name for name in CLIENT_FILES}
ALLOWED = REQUIRED | {'client/' + name + '.gz' for name in CLIENT_FILES}
MAX_BYTES = 512 * 1024 * 1024


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1 or
            not isinstance(manifest.get('release'), str) or not REVISION.fullmatch(manifest['release'])):
        raise ValueError('Invalid browser release manifest')
    files = manifest.get('files', {})
    if not isinstance(files, dict) or not REQUIRED <= files.keys() or not files.keys() <= ALLOWED:
        raise ValueError('Unexpected or missing release files')
    for entry in files.values():
        if (not isinstance(entry, dict) or not isinstance(entry.get('size'), int) or
                not 0 < entry['size'] <= MAX_BYTES or
                not isinstance(entry.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
            raise ValueError('Invalid file checksum or size')
    if sum(entry['size'] for entry in files.values()) > MAX_BYTES:
        raise ValueError('Release exceeds size limit')
    return manifest


def active_release():
    try:
        return validate_manifest(json.loads((Path(settings.NP_WEB_ROOT) / 'current' / 'manifest.json').read_text()))
    except (OSError, ValueError, TypeError):
        return None


def activate(root, revision):
    root = Path(root)
    if not REVISION.fullmatch(revision):
        raise ValueError('Invalid revision')
    manifest = validate_manifest(json.loads((root / 'releases' / revision / 'manifest.json').read_text()))
    if manifest['release'] != revision:
        raise ValueError('Release directory does not match manifest')
    # Atomic pointer swap. Existing clients keep using their versioned URLs.
    pointer = root / ('.current-' + uuid.uuid4().hex)
    try:
        pointer.symlink_to('releases/' + revision, target_is_directory=True)
        os.replace(pointer, root / 'current')
    finally:
        pointer.unlink(missing_ok=True)


def install(archive, expected_sha256, root, activate_release=True):
    root, archive = Path(root), Path(archive)
    with archive.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != expected_sha256:
            raise ValueError('Archive SHA-256 mismatch')
    releases = root / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.incoming-', dir=releases))
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            members = []
            for member in tar:
                members.append(member)
                if len(members) > len(ALLOWED) + 1:
                    raise ValueError('Too many archive entries')
            names = [member.name for member in members]
            if (len(names) != len(set(names)) or 'manifest.json' not in names or
                    not set(names) <= ALLOWED | {'manifest.json'} or
                    any(not member.isfile() for member in members) or
                    sum(member.size for member in members) > MAX_BYTES + 65536):
                raise ValueError('Unsafe or oversized release archive')
            if tar.getmember('manifest.json').size > 65536:
                raise ValueError('Manifest too large')
            manifest_bytes = tar.extractfile('manifest.json').read()
            manifest = validate_manifest(json.loads(manifest_bytes))
            if set(names) != set(manifest['files']) | {'manifest.json'}:
                raise ValueError('Archive contents differ from manifest')
            for name, entry in manifest['files'].items():
                member = tar.getmember(name)
                if member.size != entry['size']:
                    raise ValueError('Release file size mismatch')
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with tar.extractfile(member) as source, destination.open('wb') as output:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != entry['sha256']:
                    raise ValueError('Release file checksum mismatch: ' + name)
            (stage / 'manifest.json').write_bytes(manifest_bytes)
            (stage / 'archive.sha256').write_text(expected_sha256 + '\n')
        revision = manifest['release']
        target = releases / revision
        if target.exists():
            if (target / 'manifest.json').read_bytes() != manifest_bytes:
                raise ValueError('Refusing to overwrite an immutable release')
        else:
            stage.rename(target)
        if activate_release:
            activate(root, revision)
        return revision
    finally:
        if stage.exists():
            shutil.rmtree(stage)  # Only our freshly allocated staging directory.
