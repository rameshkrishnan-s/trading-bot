"""Consistent SQLite backup, integrity verification and non-destructive restore."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def copy_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('Source database does not exist')
    if destination.exists():
        raise ValueError('Destination exists; refusing to overwrite history')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.sqlite-backup-', dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)
        dst = sqlite3.connect(temporary)
        try:
            src.backup(dst, pages=256, sleep=.05)
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Database integrity check failed')
            dst.commit()
        finally:
            src.close()
            dst.close()
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        # Atomic publication that cannot overwrite a concurrently created destination.
        os.link(temporary, destination)
        parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument('--destination', type=Path, help='New file, also used for a restore drill')
    output.add_argument('--directory', type=Path, help='Create a timestamped backup here')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = args.destination or args.directory / (args.source.stem + '-' + stamp + '.sqlite3')
    print(json.dumps({'backup': str(copy_database(args.source, destination)), 'integrity': 'ok'}))


if __name__ == '__main__':
    main()
