import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from reporting.models import AppSettings


class Command(BaseCommand):
    help = "Create a recoverable local database backup and retain seven daily plus four weekly copies."

    def handle(self, *args, **options):
        backup_root = Path(os.environ.get("BACKUP_DIR", settings.BASE_DIR / "backups"))
        daily_dir = backup_root / "daily"
        weekly_dir = backup_root / "weekly"
        daily_dir.mkdir(parents=True, exist_ok=True)
        weekly_dir.mkdir(parents=True, exist_ok=True)
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        engine = settings.DATABASES["default"]["ENGINE"]
        try:
            if engine.endswith("sqlite3"):
                source = Path(settings.DATABASES["default"]["NAME"])
                target = daily_dir / f"ultex-meta-{stamp}.sqlite3"
                with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
                    src.backup(dst)
                    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise CommandError("SQLite integrity check failed.")
            elif engine.endswith("postgresql"):
                target = daily_dir / f"ultex-meta-{stamp}.dump"
                db = settings.DATABASES["default"]
                env = os.environ.copy()
                env["PGPASSWORD"] = db["PASSWORD"]
                subprocess.run(
                    ["pg_dump", "--format=custom", "--no-owner", "--file", str(target), "--host", db["HOST"], "--port", str(db["PORT"]), "--username", db["USER"], db["NAME"]],
                    check=True,
                    env=env,
                    capture_output=True,
                )
            else:
                raise CommandError(f"Unsupported database engine: {engine}")
            if timezone.localdate().weekday() == 6:
                weekly_target = weekly_dir / target.name
                shutil.copy2(target, weekly_target)
            self._prune(daily_dir, 7)
            self._prune(weekly_dir, 4)
            app_settings = AppSettings.load()
            app_settings.backup_last_success_at = timezone.now()
            app_settings.backup_status = "success"
            app_settings.save(update_fields=["backup_last_success_at", "backup_status", "updated_at"])
            self.stdout.write(self.style.SUCCESS(f"Backup created: {target.name}"))
        except Exception:
            app_settings = AppSettings.load()
            app_settings.backup_status = "failed"
            app_settings.save(update_fields=["backup_status", "updated_at"])
            raise

    @staticmethod
    def _prune(directory: Path, keep: int):
        files = sorted((path for path in directory.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink()

