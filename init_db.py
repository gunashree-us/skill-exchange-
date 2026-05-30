from core import init_db


def init_database():
    # Reuse the canonical app migration/bootstrap path so schema changes do not drift.
    init_db()
    print("Database initialized from schema_postgres.sql")


if __name__ == "__main__":
    init_database()
