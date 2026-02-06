import os
import sqlite3
from datetime import datetime, timedelta


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _backup_sqlite(db_path: str, backup_path: str) -> None:
    src = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(backup_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


def _cleanup_old(backups_dir: str, keep_days: int) -> None:
    cutoff = datetime.utcnow() - timedelta(days=keep_days)
    for name in os.listdir(backups_dir):
        if not name.endswith(".db"):
            continue
        full_path = os.path.join(backups_dir, name)
        try:
            mtime = datetime.utcfromtimestamp(os.path.getmtime(full_path))
        except OSError:
            continue
        if mtime < cutoff:
            try:
                os.remove(full_path)
            except OSError:
                continue


def main() -> None:
    db_path = os.getenv("DATABASE_PATH", "orderly_copytrading.db")
    backups_dir = os.getenv("BACKUP_DIR", "backups")
    keep_days = int(os.getenv("BACKUP_KEEP_DAYS", "7"))

    _ensure_dir(backups_dir)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backups_dir, f"orderly_copytrading_{timestamp}.db")

    _backup_sqlite(db_path, backup_path)
    _cleanup_old(backups_dir, keep_days)
    print(f"Backup created: {backup_path}")


if __name__ == "__main__":
    main()
