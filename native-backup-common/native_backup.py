#!/usr/bin/env python3
"""Snapshot export and fail-closed startup for the two Victoria apps."""
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

ENGINE = os.environ['VICTORIA_ENGINE']
VERSION = {'victoriametrics': '1.153.0', 'victorialogs': '1.52.0'}[ENGINE]
PORT = {'victoriametrics': 8428, 'victorialogs': 9428}[ENGINE]
DATA = Path(os.environ.get('VICTORIA_DATA', '/data'))
LIVE, WORK, BACKUP = (DATA / x for x in ('live', 'work', 'backup'))
INSTALL, READY, JOURNAL = (DATA / x for x in ('installation.json', 'live-ready.json', 'operation.json'))


def syncdir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def durable_json(path, value):
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(value, f, sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path); syncdir(path.parent)


def remove(path):
    if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
    elif path.exists() or path.is_symlink(): path.unlink()


def fsync_tree(root, *, onerror=None):
    for directory, dirs, files in os.walk(root, topdown=False, onerror=onerror, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if not stat.S_ISREG(path.lstat().st_mode): raise ValueError(f'not a regular file: {path}')
            with open(path, 'rb') as f: os.fsync(f.fileno())
        for name in dirs:
            if (Path(directory) / name).is_symlink(): raise ValueError('symlink directory')
        syncdir(directory)


def inventory(root, *, onerror=None):
    result = []
    if not root.is_dir() or root.is_symlink(): raise ValueError('payload missing')
    for directory, dirs, files in os.walk(root, onerror=onerror, followlinks=False):
        for d in dirs:
            if (Path(directory) / d).is_symlink(): raise ValueError('symlink directory')
        for name in files:
            p = Path(directory) / name
            if not stat.S_ISREG(p.lstat().st_mode): raise ValueError('non-regular file')
            h = hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
            result.append({'path': 'payload/' + p.relative_to(root).as_posix(), 'size': p.stat().st_size, 'sha256': h.hexdigest()})
    return sorted(result, key=lambda item: item['path'])


def ident():
    obj = json.loads(INSTALL.read_text())
    if set(obj) != {'schema_version', 'engine', 'engine_version', 'installation_id'} or obj['schema_version'] != 1 or obj['engine'] != ENGINE or obj['engine_version'] != VERSION: raise ValueError('installation identity mismatch')
    if str(uuid.UUID(obj['installation_id'])) != obj['installation_id']: raise ValueError('invalid installation id')
    return obj


def check_part_list(directory, kind):
    names = json.loads((directory / 'parts.json').read_text())
    if kind == 'metrics':
        if not isinstance(names, dict) or set(names) != {'Small', 'Big'} or not all(isinstance(v, list) for v in names.values()): raise ValueError('invalid metric part list')
        entries = [(directory if size == 'Small' else directory.parent.parent / 'big' / directory.name, name) for size, values in names.items() for name in values]
    else:
        if not isinstance(names, list): raise ValueError('invalid part list')
        entries = [(directory, name) for name in names]
    if len({(str(base), name) for base, name in entries}) != len(entries): raise ValueError('duplicate part')
    for base, name in entries:
        if not isinstance(name, str) or not re.fullmatch(r'[0-9A-F]{16}', name): raise ValueError('invalid part name')
        part = base / name
        if not part.is_dir() or part.is_symlink(): raise ValueError('missing part directory')
        metadata = json.loads((part / 'metadata.json').read_text())
        required = {'metadata.json', 'index.bin', 'metaindex.bin'}
        if kind == 'indexdb': required |= {'items.bin', 'lens.bin'}
        if kind == 'metrics': required |= {'values.bin', 'timestamps.bin'}
        if kind == 'datadb':
            version = metadata.get('FormatVersion', 0)
            if type(version) is not int or not 0 <= version <= 3: raise ValueError('invalid part format')
            required |= {'columns_header.bin', 'timestamps.bin', 'message_bloom.bin', 'message_values.bin'}
            if version >= 1:
                required |= {'column_names.bin', 'columns_header_index.bin'}
                shards = metadata.get('BloomValuesShardsCount', 0)
                if type(shards) is not int or not 0 <= shards <= 10000: raise ValueError('invalid shard count')
                required |= {f'{prefix}.bin{i}' for i in range(shards) for prefix in ('bloom', 'values')}
            else: required |= {'bloom.bin', 'values.bin'}
            if version >= 3: required.add('column_idxs.bin')
        for filename in required:
            path = part / filename
            if not path.is_file() or path.is_symlink(): raise ValueError(f'missing part file: {path}')


def validate_native(root):
    if ENGINE == 'victorialogs':
        partitions = root / 'partitions'
        if not partitions.is_dir() or partitions.is_symlink(): raise ValueError('missing partitions')
        paths = list(partitions.iterdir())
        if not paths: raise ValueError('no partitions')
        for part in paths:
            if not re.fullmatch(r'\d{8}', part.name) or not part.is_dir() or part.is_symlink(): raise ValueError('invalid partition')
            for kind in ('datadb', 'indexdb'): check_part_list(part / kind, kind)
    else:
        if not (root / 'metadata').is_dir() or not (root / 'data/indexdb').is_dir() or not (root / 'data/small').is_dir(): raise ValueError('invalid VictoriaMetrics storage layout')
        part_lists = list((root / 'data').rglob('parts.json'))
        if not part_lists: raise ValueError('missing VictoriaMetrics part lists')
        for path in part_lists:
            kind = 'indexdb' if 'indexdb' in path.parts else 'metrics'
            check_part_list(path.parent, kind)


def validate_backup(*, onerror=None):
    identity = ident(); manifest = json.loads((BACKUP / 'manifest.json').read_text())
    for key in ('schema_version', 'engine', 'engine_version', 'installation_id'):
        if manifest.get(key) != identity[key]: raise ValueError('manifest identity mismatch')
    if str(uuid.UUID(manifest['generation'])) != manifest['generation']: raise ValueError('invalid generation')
    times = []
    for key in ('snapshot_started_at', 'snapshot_completed_at', 'export_completed_at'):
        value = manifest[key]
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z', value): raise ValueError('invalid timestamp')
        times.append(dt.datetime.fromisoformat(value.replace('Z', '+00:00')))
    if times != sorted(times): raise ValueError('invalid timestamp order')
    files = inventory(BACKUP / 'payload', onerror=onerror)
    if not files or files != manifest['files']: raise ValueError('missing or corrupt backup files')
    parts = manifest['partitions']
    if ENGINE == 'victoriametrics':
        if parts != []: raise ValueError('invalid metric partitions')
    else:
        actual = sorted(p.name for p in (BACKUP / 'payload' / 'partitions').iterdir())
        if parts != actual or not parts or any(not re.fullmatch(r'\d{8}', p) for p in parts): raise ValueError('invalid log partitions')
        validate_native(BACKUP / 'payload')
    return manifest


def copy_verified(source, dest):
    before = inventory(source)
    if not before: raise ValueError('source has no files')
    shutil.copytree(source, dest, symlinks=True)
    if inventory(dest) != before or inventory(source) != before: raise ValueError('source changed or copy failed verification')
    fsync_tree(dest)


def snapshot_metadata(info):
    # Access time and hardlink count can change while the frozen bytes stay put.
    return info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def raise_walk_error(error):
    raise error


def snapshot_tree(root):
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f'not a snapshot directory: {root}')
    entries = {Path('.'): root_info}
    for directory, dirs, files in os.walk(root, onerror=raise_walk_error, followlinks=False):
        parent = Path(directory)
        for names, has_type in ((dirs, stat.S_ISDIR), (files, stat.S_ISREG)):
            for name in names:
                path = parent / name
                info = path.lstat()
                if not has_type(info.st_mode):
                    raise ValueError(f'unexpected snapshot file type: {path}')
                entries[path.relative_to(root)] = info
    return entries


def link_vl_snapshot(source, dest, payload):
    """Stage a frozen native snapshot and hash each payload inode once."""
    before = snapshot_tree(source)
    files = []
    expected = {}
    directories = []
    for relative, info in before.items():
        src, dst = source / relative, dest / relative
        key = dst.relative_to(payload)
        if stat.S_ISDIR(info.st_mode):
            dst.mkdir()
            directories.append((src, dst))
            continue
        stamp = snapshot_metadata(info)
        os.link(src, dst, follow_symlinks=False)
        fd = os.open(dst, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as reader:
            if snapshot_metadata(os.fstat(fd)) != stamp:
                raise ValueError(f'snapshot source changed while linking: {src}')
            digest = hashlib.file_digest(reader, 'sha256').hexdigest()
            if snapshot_metadata(os.fstat(fd)) != stamp:
                raise ValueError(f'snapshot source changed while hashing: {src}')
        expected[key] = stamp
        files.append({
            'path': 'payload/' + key.as_posix(),
            'size': info.st_size,
            'sha256': digest,
        })

    if not files:
        raise ValueError(f'empty snapshot source: {source}')
    after = snapshot_tree(source)
    if (before.keys() != after.keys()
            or any(snapshot_metadata(info) != snapshot_metadata(after[path])
                   for path, info in before.items())):
        raise ValueError(f'snapshot source changed: {source}')
    for src, dst in reversed(directories):
        shutil.copystat(src, dst, follow_symlinks=False)
        expected[dst.relative_to(payload)] = snapshot_metadata(dst.lstat())
    return files, expected


def verify_vl_payload(payload, expected):
    entries = snapshot_tree(payload)
    if {path: snapshot_metadata(info) for path, info in entries.items()} != expected:
        raise ValueError('snapshot payload paths or file identity changed')
    # Supervisor archives these inodes as regular files because other links to
    # them are outside the included tree. A second included name would become
    # a tar hardlink, which the B2 validator rejects.
    inodes = [(info.st_dev, info.st_ino) for info in entries.values()
              if stat.S_ISREG(info.st_mode)]
    if len(inodes) != len(set(inodes)):
        raise ValueError('multiple snapshot payload paths refer to the same file')


def vl_snapshot_sources(reply, owned):
    if not isinstance(reply, list) or not reply:
        raise ValueError('no partition snapshots')
    root = LIVE / 'partitions'
    resolved_root = root.resolve(strict=True)
    sources = {}
    invalid = False
    for item in reply:
        try:
            if not isinstance(item, str):
                raise ValueError('invalid snapshot path')
            path = Path(item)
            rel = path.relative_to(root)
            if (len(rel.parts) != 3 or not re.fullmatch(r'\d{8}', rel.parts[0])
                    or rel.parts[1] != 'snapshots' or rel.parts[2] in ('.', '..')
                    or not re.fullmatch(r'[A-Za-z0-9._-]+', rel.parts[2])
                    or path.resolve(strict=True) != resolved_root / rel):
                raise ValueError('invalid snapshot path')
        except (ValueError, OSError, RuntimeError):
            invalid = True
            continue
        if item not in owned:
            owned.append(item)
        if rel.parts[0] in sources:
            invalid = True
        sources[rel.parts[0]] = path
    if invalid:
        raise ValueError('invalid, missing, or duplicate partition snapshot path')
    return sources


def promote_live(staged, identity, operation):
    if LIVE.exists(): raise ValueError('refusing to replace live data')
    durable_json(JOURNAL, {'operation': operation, 'installation_id': identity['installation_id']})
    os.rename(staged, LIVE); syncdir(DATA)
    durable_json(READY, identity)
    JOURNAL.unlink(); syncdir(DATA)


def startup(mode):
    if JOURNAL.exists(): raise ValueError('interrupted operation; inspect operation.json')
    if INSTALL.exists():
        identity = ident()
        if LIVE.is_dir() and not LIVE.is_symlink() and READY.exists() and json.loads(READY.read_text()) == identity: return
        if LIVE.exists(): raise ValueError('uncommitted live data exists')
        validate_backup()
        WORK.mkdir(exist_ok=True); staged = WORK / 'restore'; remove(staged)
        if ENGINE == 'victoriametrics':
            subprocess.run(['/vmrestore-prod', '-src=fs://' + str(BACKUP / 'payload'), '-storageDataPath=' + str(staged), '-concurrency=1'], check=True)
            if not inventory(staged): raise ValueError('restored database is empty')
            fsync_tree(staged, onerror=raise_walk_error)
            validate_native(staged)
            validate_backup()
        else:
            copy_verified(BACKUP / 'payload', staged)
            if inventory(staged) != json.loads((BACKUP / 'manifest.json').read_text())['files']: raise ValueError('restore checksum mismatch')
        promote_live(staged, identity, 'restore'); return
    if LIVE.exists() or READY.exists() or BACKUP.exists(): raise ValueError('installation identity missing')
    if mode not in ('import_legacy', 'init-empty'): raise ValueError('no installation; import or valid restored backup required')
    source = Path('/share') / ('victoria-metrics-data' if ENGINE == 'victoriametrics' else 'victoria-logs-data')
    WORK.mkdir(exist_ok=True); staged = WORK / 'import'; remove(staged)
    if mode == 'init-empty': staged.mkdir(); fsync_tree(staged)
    else:
        validate_native(source)
        copy_verified(source, staged)
        validate_native(staged)
    identity = {'schema_version': 1, 'engine': ENGINE, 'engine_version': VERSION, 'installation_id': str(uuid.uuid4())}
    durable_json(JOURNAL, {'operation': mode, 'installation_id': identity['installation_id']})
    durable_json(INSTALL, identity)
    os.rename(staged, LIVE); syncdir(DATA); durable_json(READY, identity)
    JOURNAL.unlink(); syncdir(DATA)


def now(): return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def api(path, params=None):
    query = '?' + urllib.parse.urlencode(params) if params else ''
    config_path = DATA / 'options.json'
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    request = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}{query}')
    if config.get('enableHTTPAuth'):
        token = base64.b64encode((config['username'] + ':' + config['password']).encode()).decode()
        request.add_header('Authorization', 'Basic ' + token)
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
        return json.loads(body) if body else None


def export():
    started_clock = time.monotonic()
    if JOURNAL.exists(): raise ValueError('interrupted operation')
    identity = ident()
    if not LIVE.is_dir() or not READY.exists() or json.loads(READY.read_text()) != identity: raise ValueError('live data not initialized')
    WORK.mkdir(exist_ok=True); staged = WORK / 'export'; remove(staged); staged.mkdir()
    payload = staged / 'payload'; payload.mkdir()
    started = now(); snapshots = []; partitions = []
    timings = {}
    try:
        if ENGINE == 'victoriametrics':
            reply = api('/snapshot/create')
            if reply.get('status') != 'ok' or not re.fullmatch(r'[A-Za-z0-9._-]+', reply.get('snapshot', '')): raise ValueError('snapshot creation failed')
            snapshots = [reply['snapshot']]; completed = now()
            subprocess.run(['/vmbackup-prod', '-snapshotName='+snapshots[0], '-storageDataPath='+str(LIVE), '-dst=fs://'+str(payload), '-concurrency=1'], check=True)
            files = inventory(payload)
        else:
            phase_start = time.monotonic()
            reply = api('/internal/partition/snapshot/create')
            completed = now()
            timings['snapshot_seconds'] = time.monotonic() - phase_start

            phase_start = time.monotonic()
            sources = vl_snapshot_sources(reply, snapshots)
            partitions = sorted(sources)
            partition_dir = payload / 'partitions'
            partition_dir.mkdir()
            files = []
            expected = {}
            for partition, source in sources.items():
                entries, metadata = link_vl_snapshot(source, partition_dir / partition, payload)
                files.extend(entries)
                expected.update(metadata)
            expected[Path('partitions')] = snapshot_metadata(partition_dir.lstat())
            expected[Path('.')] = snapshot_metadata(payload.lstat())
            files.sort(key=lambda item: item['path'])
            timings['link_and_hash_seconds'] = time.monotonic() - phase_start

            phase_start = time.monotonic()
            verify_vl_payload(payload, expected)
            validate_native(payload)
            timings['staged_verify_seconds'] = time.monotonic() - phase_start
            phase_start = time.monotonic()
            # The engine syncs its snapshot files. Persist the new directory
            # entries for our links, without syncing the data files again.
            for path, stamp in expected.items():
                if stat.S_ISDIR(stamp[0]):
                    syncdir(payload / path)
            timings['sync_seconds'] = time.monotonic() - phase_start
        if not files: raise ValueError('empty snapshot')
        phase_start = time.monotonic()
        manifest = dict(identity, generation=str(uuid.uuid4()), snapshot_started_at=started, snapshot_completed_at=completed, export_completed_at=now(), partitions=sorted(partitions), files=files)
        durable_json(staged / 'manifest.json', manifest)
        if ENGINE == 'victoriametrics':
            fsync_tree(staged)
        durable_json(JOURNAL, {'operation': 'publish', 'generation': manifest['generation']})
        old = WORK / 'previous-backup'; remove(old)
        if BACKUP.exists(): os.rename(BACKUP, old); syncdir(DATA); syncdir(WORK)
        os.rename(staged, BACKUP); syncdir(DATA); syncdir(WORK)
        if ENGINE == 'victorialogs':
            verify_vl_payload(BACKUP / 'payload', expected)
            if json.loads((BACKUP / 'manifest.json').read_text()) != manifest:
                raise ValueError('published snapshot manifest mismatch')
        else:
            validate_backup()
        JOURNAL.unlink(); syncdir(DATA); remove(old)
        timings['publish_and_verify_seconds'] = time.monotonic() - phase_start
    finally:
        phase_start = time.monotonic()
        for snap in snapshots:
            try:
                if ENGINE == 'victoriametrics': api('/snapshot/delete', {'snapshot': snap})
                else: api('/internal/partition/snapshot/delete', {'path': snap})
            except Exception as exc: print(f'could not remove owned snapshot: {exc}', file=sys.stderr)
    if ENGINE == 'victorialogs':
        timings['cleanup_seconds'] = time.monotonic() - phase_start
        timings['total_seconds'] = time.monotonic() - started_clock
        print('native backup timing: ' + json.dumps(dict(
            timings, files=len(files), bytes=sum(item['size'] for item in files),
            partitions=len(partitions)), sort_keys=True))


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / '.native-backup.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        action = sys.argv[1]
        if action == 'startup': startup(sys.argv[2])
        elif action == 'export': export()
        else: raise ValueError('unknown action')

if __name__ == '__main__':
    try: main()
    except Exception as exc: print(f'native backup: {exc}', file=sys.stderr); sys.exit(1)
