"""
scripts/reset_db.py - Clean-slate v2 SQLite migration helper.

Backs up the existing SQLite database (if present) and recreates all tables
using the v2 FK-normalized schema. Existing legacy name-keyed test rows are discarded.
"""
import os
import sys
import shutil
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def reset_database(app_instance, backup: bool = True) -> None:
    from db import db
    import models  # noqa: F401 - register all ORM models

    uri = app_instance.config.get("SQLALCHEMY_DATABASE_URI", "")
    if backup and uri.startswith("sqlite:///") and uri != "sqlite:///:memory:":
        db_path = uri.replace("sqlite:///", "", 1)
        if os.path.exists(db_path):
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = f"{db_path}.bak.{ts}"
            shutil.copy2(db_path, backup_path)
            print(f"Backed up existing database to {backup_path}")
            # Remove WAL/SHM sidecar files before recreating
            for suffix in ("-wal", "-shm"):
                sidecar = db_path + suffix
                if os.path.exists(sidecar):
                    os.remove(sidecar)
            os.remove(db_path)

    with app_instance.app_context():
        db.drop_all()
        db.create_all()
        print("Created v2 CardioSense schema successfully.")


if __name__ == "__main__":
    from app import app

    reset_database(app, backup=True)
