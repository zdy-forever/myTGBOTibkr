import os
import sqlite3
import subprocess
import sys

import pytest

from quantbot.backup import create_backup, restore_backup


def test_online_backup_and_integrity_checked_restore(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("create table sample(value text)")
        connection.execute("insert into sample values ('ok')")
    backup = create_backup(source, tmp_path / "backups")
    destination = tmp_path / "restored.db"
    restore_backup(backup, destination)
    with sqlite3.connect(destination) as connection:
        assert connection.execute("select value from sample").fetchone() == ("ok",)


def test_module_entrypoint_creates_real_backup(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("create table sample(value text)")
        connection.execute("insert into sample values ('preserved')")
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{source}", "BACKUP_DIR": str(tmp_path / "backups"),
           "PHONE_HMAC_SECRET": "x" * 32, "PHONE_ENCRYPTION_KEY": "", "BOT_OWNER_TELEGRAM_ID": ""}
    result = subprocess.run([sys.executable, "-m", "quantbot.backup"], cwd=tmp_path,
                            env=env, capture_output=True, text=True, check=True)
    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1 and str(backups[0]) in result.stdout
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("select value from sample").fetchone() == ("preserved",)
    help_result = subprocess.run([sys.executable, "-m", "quantbot.backup", "--help"], cwd=tmp_path,
                                 env=env, capture_output=True, text=True, check=True)
    assert "--restore" in help_result.stdout


def test_backup_missing_database_does_not_create_empty_backup(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_backup(tmp_path / "missing.db", tmp_path / "backups")
    assert not (tmp_path / "backups").exists()
