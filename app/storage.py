import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Optional

from app.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderlyCredentials:
    user_id: int
    orderly_account_id: str
    orderly_key: str
    orderly_secret: str


class Storage:
    def __init__(self, db_path: str, master_key: str) -> None:
        self._db_path = db_path
        self._master_key = master_key

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    orderly_account_id TEXT NOT NULL,
                    orderly_key TEXT NOT NULL,
                    orderly_secret_enc TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS followers (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, user_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )

        logger.info("SQLite initialized at %s", self._db_path)

    def upsert_user(
        self,
        user_id: int,
        orderly_account_id: str,
        orderly_key: str,
        orderly_secret: str,
    ) -> None:
        from app.crypto import build_fernet

        fernet = build_fernet(self._master_key)
        secret_enc = encrypt_secret(fernet, orderly_secret)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, orderly_account_id, orderly_key, orderly_secret_enc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    orderly_account_id = excluded.orderly_account_id,
                    orderly_key = excluded.orderly_key,
                    orderly_secret_enc = excluded.orderly_secret_enc
                """,
                (user_id, orderly_account_id, orderly_key, secret_enc),
            )

    def get_user(self, user_id: int) -> Optional[OrderlyCredentials]:
        from app.crypto import build_fernet

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, orderly_account_id, orderly_key, orderly_secret_enc
                FROM users WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

        if not row:
            return None

        fernet = build_fernet(self._master_key)
        orderly_secret = decrypt_secret(fernet, row[3])
        return OrderlyCredentials(
            user_id=row[0],
            orderly_account_id=row[1],
            orderly_key=row[2],
            orderly_secret=orderly_secret,
        )

    def set_following(self, group_id: int, user_id: int, follow: bool) -> None:
        with self._connect() as conn:
            if follow:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO followers (group_id, user_id)
                    VALUES (?, ?)
                    """,
                    (group_id, user_id),
                )
            else:
                conn.execute(
                    "DELETE FROM followers WHERE group_id = ? AND user_id = ?",
                    (group_id, user_id),
                )

    def list_followers(self, group_id: int) -> list[OrderlyCredentials]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT u.user_id, u.orderly_account_id, u.orderly_key, u.orderly_secret_enc
                FROM followers f
                JOIN users u ON u.user_id = f.user_id
                WHERE f.group_id = ?
                """,
                (group_id,),
            ).fetchall()

        from app.crypto import build_fernet

        fernet = build_fernet(self._master_key)
        return [
            OrderlyCredentials(
                user_id=row[0],
                orderly_account_id=row[1],
                orderly_key=row[2],
                orderly_secret=decrypt_secret(fernet, row[3]),
            )
            for row in rows
        ]
