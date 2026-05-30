import os
import sqlite3

import psycopg
from psycopg.rows import dict_row

from core import BASE_DIR, ensure_database_ready


SQLITE_PATH = os.environ.get("SQLITE_PATH") or os.path.join(BASE_DIR, "skill_exchange.db")
DATABASE_URL = os.environ.get("DATABASE_URL")

TABLES = [
    "users",
    "skills",
    "user_skills",
    "exchange_requests",
    "messages",
    "user_devices",
    "profile_certificates",
    "reviews",
    "notifications",
    "app_bootstrap_state",
]


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def load_rows(sqlite_db, table_name):
    rows = sqlite_db.execute(f"SELECT * FROM {quote_ident(table_name)}").fetchall()
    columns = [description[0] for description in sqlite_db.execute(f"SELECT * FROM {quote_ident(table_name)} LIMIT 1").description]
    return columns, rows


def sqlite_table_exists(sqlite_db, table_name):
    row = sqlite_db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def upsert_table(pg_db, table_name, columns, rows):
    if not rows:
        return 0
    quoted_columns = ", ".join(quote_ident(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    update_columns = [column for column in columns if column != "id"]
    if update_columns:
        update_clause = ", ".join(
            f"{quote_ident(column)} = EXCLUDED.{quote_ident(column)}"
            for column in update_columns
        )
        sql = (
            f"INSERT INTO {quote_ident(table_name)} ({quoted_columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {update_clause}"
        )
    else:
        sql = (
            f"INSERT INTO {quote_ident(table_name)} ({quoted_columns}) VALUES ({placeholders}) "
            "ON CONFLICT (id) DO NOTHING"
        )
    with pg_db.cursor() as cursor:
        cursor.executemany(sql, rows)
    return len(rows)


def reset_sequence(pg_db, table_name):
    with pg_db.cursor() as cursor:
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table_name,))
        row = cursor.fetchone()
        if not row:
            sequence_name = None
        elif isinstance(row, dict):
            sequence_name = next(iter(row.values()))
        else:
            sequence_name = row[0]
        if not sequence_name:
            return
        cursor.execute(
            f"SELECT setval(%s, COALESCE((SELECT MAX(id) FROM {quote_ident(table_name)}), 1), true)",
            (sequence_name,),
        )


def main():
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL before running this migration.")
    if not os.path.exists(SQLITE_PATH):
        raise RuntimeError(f"SQLite database not found: {SQLITE_PATH}")

    sqlite_db = sqlite3.connect(SQLITE_PATH)
    sqlite_db.row_factory = sqlite3.Row
    pg_db = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    try:
        ensure_database_ready(pg_db, force_schema_bootstrap=True)
        migrated_counts = {}
        for table_name in TABLES:
            if not sqlite_table_exists(sqlite_db, table_name):
                migrated_counts[table_name] = 0
                continue
            columns, rows = load_rows(sqlite_db, table_name)
            migrated_counts[table_name] = upsert_table(pg_db, table_name, columns, rows)
            reset_sequence(pg_db, table_name)
        pg_db.commit()
    finally:
        sqlite_db.close()
        pg_db.close()

    print("Migration complete:")
    for table_name in TABLES:
        print(f"- {table_name}: {migrated_counts.get(table_name, 0)} rows")


if __name__ == "__main__":
    main()
