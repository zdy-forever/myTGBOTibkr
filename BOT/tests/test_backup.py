import sqlite3

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
