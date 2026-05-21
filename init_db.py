from app import init_db


def init_database():
    # Reuse the canonical app migration/bootstrap path so schema changes do not drift.
    init_db()
    print("Database initialized: skill_exchange.db")


if __name__ == "__main__":
    init_database()
