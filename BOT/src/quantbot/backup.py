from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings


def sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.endswith(":memory:"):
        raise ValueError("backup command supports file-backed SQLite only")
    return Path(database_url.removeprefix(prefix)).resolve()


def create_backup(source: Path, directory: Path) -> Path:
    if not source.is_file():
        raise FileNotFoundError(source)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"quantbot-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return target


def prune_backups(directory: Path, retention_days: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    for path in directory.glob("quantbot-*.db"):
        if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
            path.unlink()


def restore_backup(backup: Path, destination: Path) -> None:
    if not backup.is_file():
        raise FileNotFoundError(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".restore")
    shutil.copy2(backup, temporary)
    with sqlite3.connect(temporary) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            temporary.unlink(missing_ok=True)
            raise ValueError("backup failed SQLite integrity check")
    temporary.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env(require_token=False)
    source = sqlite_path(settings.database_url)
    if args.restore:
        restore_backup(args.restore, source)
        print(f"restored {args.restore} to {source}")
    else:
        target = create_backup(source, settings.backup_dir)
        prune_backups(settings.backup_dir, settings.backup_retention_days)
        print(target)


if __name__ == "__main__":
    main()
