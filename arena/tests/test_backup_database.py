from contextlib import closing
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from arena.management.commands.backup_database import backup_sqlite


class BackupDatabaseTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source with spaces.sqlite3"
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE payment (id INTEGER PRIMARY KEY, reference TEXT UNIQUE, amount INTEGER NOT NULL);
                CREATE TABLE audit (id INTEGER PRIMARY KEY, payment_id INTEGER REFERENCES payment(id), reason TEXT);
                CREATE INDEX audit_payment ON audit(payment_id);
                INSERT INTO payment VALUES (1, 'BANK-TEST', 2200);
                INSERT INTO audit VALUES (1, 1, 'Test approval');
                PRAGMA user_version=7;
            """)

    def command(self, destination=None, source=None, **kwargs):
        config = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(source or self.source)}}
        output = StringIO()
        arguments = {"stdout": output, **kwargs}
        if destination is not None:
            arguments["output"] = str(destination)
        with patch.object(settings, "DATABASES", config), patch.object(settings, "BASE_DIR", self.root):
            call_command("backup_database", **arguments)
        return output.getvalue()

    def test_backup_restores_data_audit_indexes_and_constraints_to_a_new_database(self):
        backup = self.root / "backup.sqlite3"
        source_before = self.source.read_bytes()
        output = self.command(backup)
        self.assertIn("integrity_check=ok", output)
        self.assertEqual(self.source.read_bytes(), source_before)
        # Simulate changes after the backup; restore must retain the old snapshot.
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE payment SET amount=9999")
            db.commit()
        restored = self.root / "restored.sqlite3"
        self.command(restored, source=backup)
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(db.execute("SELECT reference, amount FROM payment").fetchall(), [("BANK-TEST", 2200)])
            self.assertEqual(db.execute("SELECT payment_id, reason FROM audit").fetchall(), [(1, "Test approval")])
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='audit_payment'").fetchone())
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO payment VALUES (2, 'BANK-TEST', 2200)")
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_backup_includes_committed_wal_and_excludes_uncommitted_transaction(self):
        with closing(sqlite3.connect(self.source)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO payment VALUES (2, 'WAL-COMMITTED', 2200)")
            writer.commit()
            writer.execute("INSERT INTO payment VALUES (3, 'NOT-COMMITTED', 2200)")
            self.assertTrue(Path(str(self.source) + "-wal").exists())
            destination = self.root / "wal-backup.sqlite3"
            self.command(destination)
            with closing(sqlite3.connect(destination)) as copied:
                self.assertEqual(copied.execute("SELECT id FROM payment ORDER BY id").fetchall(), [(1,), (2,)])
                self.assertEqual(copied.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertFalse(Path(str(destination) + "-wal").exists())
            writer.rollback()

    def test_existing_destination_and_source_are_never_overwritten(self):
        destination = self.root / "existing.sqlite3"
        destination.write_bytes(b"KEEP THIS FILE")
        source_before = self.source.read_bytes()
        for path in (destination, self.source):
            with self.subTest(path=path), self.assertRaises(CommandError):
                self.command(path)
        self.assertEqual(destination.read_bytes(), b"KEEP THIS FILE")
        self.assertEqual(self.source.read_bytes(), source_before)

    def test_destination_race_does_not_replace_newly_created_file(self):
        destination = self.root / "race.sqlite3"
        import os
        original_link = os.link
        def concurrent_destination(source, target):
            Path(target).write_bytes(b"CONCURRENT FILE")
            return original_link(source, target)
        with patch("arena.management.commands.backup_database.os.link", side_effect=concurrent_destination):
            with self.assertRaises(CommandError):
                self.command(destination)
        self.assertEqual(destination.read_bytes(), b"CONCURRENT FILE")
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_invalid_source_never_publishes_or_creates_source(self):
        missing = self.root / "missing.sqlite3"
        destination = self.root / "failure.sqlite3"
        with self.assertRaises(CommandError):
            self.command(destination, source=missing)
        self.assertFalse(missing.exists())
        self.assertFalse(destination.exists())
        broken = self.root / "broken.sqlite3"
        broken.write_bytes(b"THIS IS NOT SQLITE")
        with self.assertRaises(CommandError):
            self.command(destination, source=broken)
        self.assertEqual(broken.read_bytes(), b"THIS IS NOT SQLITE")
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_timeout_cleans_partial_copy_without_publishing(self):
        destination = self.root / "timeout.sqlite3"
        # First time establishes the deadline, subsequent checks exceed it.
        with patch("arena.management.commands.backup_database.time.monotonic", side_effect=[0, 100, 100, 100]):
            with self.assertRaises(CommandError):
                self.command(destination, timeout=1)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_locked_source_times_out_without_touching_source_or_publishing(self):
        destination = self.root / "locked.sqlite3"
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("UPDATE payment SET amount=9999")
            with self.assertRaises(CommandError):
                self.command(destination, timeout=1)
            writer.rollback()
            self.assertEqual(writer.execute("SELECT amount FROM payment").fetchone()[0], 2200)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_failed_publication_is_safe_and_partial_is_removed(self):
        destination = self.root / "unsupported.sqlite3"
        with patch("arena.management.commands.backup_database.os.link", side_effect=OSError("No hard links")):
            with self.assertRaises(CommandError):
                self.command(destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_default_names_are_unique_and_only_write_under_configured_base(self):
        self.command()
        self.command()
        copies = list((self.root / ".local" / "backups").glob("*.sqlite3"))
        self.assertEqual(len(copies), 2)
        self.assertNotEqual(copies[0].name, copies[1].name)

    def test_success_handles_redirected_cp1252_stdout_and_unicode_output_path(self):
        destination = self.root / "копия_日本.sqlite3"
        byte_stream = BytesIO()
        stdout = TextIOWrapper(byte_stream, encoding="cp1252", errors="strict")
        self.addCleanup(stdout.close)
        self.command(destination, stdout=stdout)
        stdout.flush()
        result = byte_stream.getvalue().decode("cp1252")
        self.assertIn("Backup complete; integrity_check=ok:", result)
        self.assertIn(str(destination).encode("cp1252", errors="backslashreplace").decode("cp1252"), result)
        self.assertEqual((stdout.encoding, stdout.errors), ("cp1252", "strict"))
        self.assertTrue(destination.is_file())
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute("SELECT amount FROM payment").fetchone()[0], 2200)

    def test_unicode_capable_stdout_preserves_readable_path(self):
        destination = self.root / "резервная-копия.sqlite3"
        result = self.command(destination)
        self.assertIn(str(destination), result)

    def test_non_file_databases_unknown_alias_and_invalid_timeouts_fail_closed(self):
        destination = self.root / "invalid.sqlite3"
        for name in (":memory:", "file:memory?mode=memory&cache=shared", ""):
            config = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": name}}
            with patch.object(settings, "DATABASES", config), self.assertRaises(CommandError):
                call_command("backup_database", output=str(destination))
        with self.assertRaises(CommandError):
            self.command(destination, database="unknown")
        for timeout in (0, 301, True, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(CommandError):
                backup_sqlite(self.source, destination, timeout=timeout)
        self.assertFalse(destination.exists())
