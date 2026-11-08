"""Create a checked SQLite snapshot without opening the source for writing."""

import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def backup_sqlite(source, destination, *, timeout=30):
    """Publish a verified new backup; existing paths are never overwritten.

    The temporary file and final file share a directory/filesystem. An exclusive
    hard link publishes the complete snapshot atomically, including on Windows.
    Filesystems without hard-link support fail safely instead of copying over
    an existing destination or publishing an incomplete backup.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise CommandError("--timeout должен быть от 1 до 300 секунд.")
    try:
        source = Path(source).expanduser().resolve()
        raw_destination = Path(destination).expanduser()
        if os.path.lexists(raw_destination):
            raise CommandError("Файл назначения уже существует. Выберите новое имя; перезапись запрещена.")
        destination = raw_destination.resolve()
        if source == destination:
            raise CommandError("Резервная копия не может заменить исходную базу.")
        if not source.is_file() or source.stat().st_size == 0:
            raise CommandError("Исходная SQLite-база не найдена или файл пуст. Новая исходная база не создаётся.")
    except (OSError, ValueError):
        raise CommandError("Некорректный или недоступный путь к SQLite-файлу.") from None
    deadline = time.monotonic() + timeout
    temporary = None

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise CommandError("Время резервного копирования истекло. Итоговый файл не создан.")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".sqlite-backup-", suffix=".partial", dir=destination.parent)
        temporary = Path(name)
        os.close(descriptor)
        # mode=ro also prevents an incorrect source path from creating a new DB.
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=min(timeout, 1))) as source_db:
            source_db.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(str(temporary), timeout=min(timeout, 1))) as backup_db:
                source_db.backup(backup_db, pages=256, progress=progress, sleep=0.05)
                # A standalone snapshot must not depend on WAL/SHM sidecars.
                backup_db.execute("PRAGMA journal_mode=DELETE")
                backup_db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                integrity = backup_db.execute("PRAGMA integrity_check").fetchall()
                if integrity != [("ok",)]:
                    raise CommandError("Проверка целостности копии не пройдена. Итоговый файл не создан.")
                progress(None, 0, 0)
        # Flush database bytes before exposing the finished name to an operator.
        # Windows FlushFileBuffers requires a writable handle; only the new
        # temporary snapshot is opened here, never the source.
        with temporary.open("rb+") as checked_file:
            os.fsync(checked_file.fileno())
        # Unlike rename/replace, link cannot overwrite an existing final path,
        # including a destination created by another process during the backup.
        os.link(temporary, destination)
    except FileExistsError:
        raise CommandError("Файл назначения появился во время копирования. Перезапись запрещена.") from None
    except (sqlite3.Error, OSError) as exc:
        raise CommandError(f"Не удалось создать проверенную резервную копию ({type(exc).__name__}). Итоговый файл не опубликован.") from None
    finally:
        if temporary is not None:
            # Delete only this command's uniquely created temporary file.
            temporary.unlink(missing_ok=True)
    return destination


class Command(BaseCommand):
    help = "Create a new verified SQLite backup using SQLite's online backup API."
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default", help="Database alias from Django settings (default: default).")
        parser.add_argument("--output", help="New output file. Defaults to .local/backups/<source>-<UTC time>-<random>.sqlite3.")
        parser.add_argument("--timeout", type=float, default=30, help="Backup/check time limit in seconds, 1..300 (default: 30).")

    def handle(self, *args, **options):
        config = settings.DATABASES.get(options["database"])
        if not config or config.get("ENGINE") != "django.db.backends.sqlite3":
            raise CommandError("Выберите настроенную файловую SQLite-базу.")
        name = str(config.get("NAME", ""))
        if not name or name == ":memory:" or name.startswith("file:"):
            raise CommandError("Команда поддерживает обычный путь к SQLite-файлу, не memory/URI-базы.")
        source = Path(name).expanduser().resolve()
        if options["output"]:
            destination = options["output"]
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            destination = Path(settings.BASE_DIR) / ".local" / "backups" / f"{source.stem}-{stamp}-{uuid4().hex[:8]}.sqlite3"
        completed = backup_sqlite(source, destination, timeout=options["timeout"])
        message = f"Backup complete; integrity_check=ok: {completed}"
        # Redirected Windows stdout may use a legacy encoding (e.g. cp1252).
        # Escape only characters this particular stream cannot represent;
        # never change global stdout, its encoding, or the actual file path.
        encoding = getattr(self.stdout, "encoding", None)
        if encoding:
            message = message.encode(encoding, errors="backslashreplace").decode(encoding)
        self.stdout.write(self.style.SUCCESS(message))
