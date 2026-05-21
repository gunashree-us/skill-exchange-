import json
import os
import secrets
import sqlite3
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from threading import Lock
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "skill_exchange.db")
STYLES_PATH = os.path.join(BASE_DIR, "static", "styles.css")


def load_local_env():
    # Allow local development to read SECRET_KEY from a repo-root .env without weakening production rules.
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


load_local_env()
DEBUG_MODE = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
SECRET_KEY = os.environ.get("SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY environment variable is required.\n"
        "For local development, either run:\n"
        "  export SECRET_KEY=local-dev-secret\n"
        "or create a .env file in the project root containing:\n"
        "  SECRET_KEY=local-dev-secret"
    )

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["DATABASE"] = DATABASE
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DEFAULT_CATEGORIES = ["Tech", "Design", "Music", "Languages", "Business", "Arts", "Health", "Marketing"]
DEFAULT_TESTIMONIALS = [
    {
        "name": "Sarah Johnson",
        "role": "Graphic Designer",
        "quote": "I learned Spanish while teaching design. Amazing experience!",
    },
    {
        "name": "Michael Chen",
        "role": "Software Developer",
        "quote": "Found the perfect skill exchange partner in just 2 days!",
    },
    {
        "name": "Emma Davis",
        "role": "Marketing Specialist",
        "quote": "Such a supportive and friendly community. Highly recommend!",
    },
]
MAX_NAME_LENGTH = 80
MAX_EMAIL_LENGTH = 255
MAX_PASSWORD_LENGTH = 128
MAX_BIO_LENGTH = 600
MAX_AVAILABILITY_LENGTH = 120
MAX_PROFILE_LINK_LENGTH = 255
MAX_CERTIFICATIONS_LENGTH = 1200
MAX_SKILL_NAME_LENGTH = 80
MAX_CATEGORY_LENGTH = 40
MAX_MESSAGE_LENGTH = 500
MAX_ENCRYPTED_MESSAGE_LENGTH = 12000
MAX_SCHEDULE_LENGTH = 120
MAX_REVIEW_LENGTH = 500
MAX_E2EE_REWRAP_BATCH = 100
REQUEST_DURATIONS = [30, 45, 60, 90, 120]
ADMIN_USERS_PER_PAGE = 25
LEVEL_ORDER = {"Beginner": 1, "Intermediate": 2, "Advanced": 3}
UPLOAD_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
UPLOAD_CERTIFICATE_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp"}
ACTIVE_CHAT_ROOMS = defaultdict(set)
SID_ROOMS = {}
ACTIVE_CONFERENCE_ROOMS = {}
SID_CONFERENCE_ROOMS = defaultdict(set)
DISPLAY_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kolkata"))
UPLOADS_DIR = os.path.join(BASE_DIR, "static", "uploads")
PROFILE_UPLOADS_DIR = os.path.join(UPLOADS_DIR, "profiles")
CERTIFICATE_UPLOADS_DIR = os.path.join(UPLOADS_DIR, "certificates")
SCHEMA_BOOTSTRAP_LOCK = Lock()
SCHEMA_BOOTSTRAPPED = False


def static_file_version(path):
    # Use a stable file timestamp for cache busting without hitting the filesystem on every request.
    return int(os.path.getmtime(path)) if os.path.exists(path) else 0


STYLE_VERSION = static_file_version(STYLES_PATH)


def get_webrtc_ice_servers():
    # Allow TURN/STUN configuration from the environment for networks where STUN-only fails.
    raw_value = os.environ.get("WEBRTC_ICE_SERVERS", "").strip()
    if not raw_value:
        return [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]
    if isinstance(parsed, list) and parsed:
        return parsed
    return [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]


def get_db():
    # Keep one SQLite connection per request and expose rows like dictionaries.
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        ensure_database_ready(g.db)
    return g.db


@app.teardown_appcontext
def close_db(_error):
    # Close the request-scoped database handle when Flask finishes the request.
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_db(query, args=(), one=False):
    # Shared helper for SELECT queries.
    rows = get_db().execute(query, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def execute_db(query, args=()):
    # Shared helper for INSERT/UPDATE/DELETE queries with an immediate commit.
    db = get_db()
    cursor = db.execute(query, args)
    db.commit()
    return cursor


def column_exists(db, table_name, column_name):
    # Lightweight schema migration helper used during startup.
    columns = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(column[1] == column_name for column in columns)


def ensure_schema_updates(db):
    # Backfill new columns for local databases created before recent features existed.
    request_table_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'exchange_requests'"
    ).fetchone()
    needs_request_rebuild = (
        request_table_sql is not None
        and "Countered" not in (request_table_sql[0] or "")
    )
    if needs_request_rebuild:
        db.execute("ALTER TABLE exchange_requests RENAME TO exchange_requests_old")
        db.executescript(
            """
            CREATE TABLE exchange_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                teach_skill_id INTEGER NOT NULL,
                learn_skill_id INTEGER NOT NULL,
                message TEXT DEFAULT '',
                schedule_note TEXT DEFAULT '',
                proposed_time TEXT DEFAULT '',
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Countered', 'Accepted', 'Rejected')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(sender_id) REFERENCES users(id),
                FOREIGN KEY(receiver_id) REFERENCES users(id),
                FOREIGN KEY(teach_skill_id) REFERENCES skills(id),
                FOREIGN KEY(learn_skill_id) REFERENCES skills(id)
            );
            """
        )
        db.execute(
            """
            INSERT INTO exchange_requests (
                id, sender_id, receiver_id, teach_skill_id, learn_skill_id, message, schedule_note,
                proposed_time, duration_minutes, status, created_at
            )
            SELECT
                id, sender_id, receiver_id, teach_skill_id, learn_skill_id, message, schedule_note,
                '', 60, status, created_at
            FROM exchange_requests_old
            """
        )
        db.execute("DROP TABLE exchange_requests_old")
    else:
        if not column_exists(db, "exchange_requests", "proposed_time"):
            db.execute("ALTER TABLE exchange_requests ADD COLUMN proposed_time TEXT DEFAULT ''")
        if not column_exists(db, "exchange_requests", "duration_minutes"):
            db.execute("ALTER TABLE exchange_requests ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 60")
    if not column_exists(db, "users", "public_key"):
        db.execute("ALTER TABLE users ADD COLUMN public_key TEXT DEFAULT ''")
    if not column_exists(db, "users", "private_key_wrapped"):
        db.execute("ALTER TABLE users ADD COLUMN private_key_wrapped TEXT DEFAULT ''")
    if not column_exists(db, "users", "private_key_salt"):
        db.execute("ALTER TABLE users ADD COLUMN private_key_salt TEXT DEFAULT ''")
    if not column_exists(db, "users", "profile_photo_path"):
        db.execute("ALTER TABLE users ADD COLUMN profile_photo_path TEXT DEFAULT ''")
    if not column_exists(db, "users", "github_url"):
        db.execute("ALTER TABLE users ADD COLUMN github_url TEXT DEFAULT ''")
    if not column_exists(db, "users", "linkedin_url"):
        db.execute("ALTER TABLE users ADD COLUMN linkedin_url TEXT DEFAULT ''")
    if not column_exists(db, "users", "certifications"):
        db.execute("ALTER TABLE users ADD COLUMN certifications TEXT DEFAULT ''")
    if not column_exists(db, "users", "profile_setup_completed"):
        db.execute("ALTER TABLE users ADD COLUMN profile_setup_completed INTEGER NOT NULL DEFAULT 1")
    if not column_exists(db, "messages", "delivered_at"):
        db.execute("ALTER TABLE messages ADD COLUMN delivered_at TIMESTAMP")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            device_token TEXT NOT NULL,
            label TEXT DEFAULT '',
            public_key TEXT NOT NULL,
            revoked_at TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, device_token),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_certificates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )


def user_initials(name):
    # Used by the UI anywhere we need a simple text avatar.
    parts = [part for part in name.split() if part]
    return "".join(part[0].upper() for part in parts[:2]) or "U"


def init_db():
    # Load the canonical schema from disk and seed the default admin account once.
    db = sqlite3.connect(app.config["DATABASE"])
    try:
        ensure_database_ready(db, force_schema_bootstrap=True)
    finally:
        db.close()


def seed_default_admin(db):
    # Keep the default admin account available for local development.
    admin = db.execute("SELECT id FROM users WHERE email = ?", ("admin@skillx.local",)).fetchone()
    if admin is None:
        db.execute(
            "INSERT INTO users (name, email, password_hash, bio, is_admin) VALUES (?, ?, ?, ?, 1)",
            ("Admin", "admin@skillx.local", generate_password_hash("admin123"), "Platform administrator"),
        )


def bootstrap_state_ready(db):
    # Keep schema bootstrap state in SQLite so multiple worker processes share one source of truth.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_bootstrap_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schema_ready INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    row = db.execute("SELECT schema_ready FROM app_bootstrap_state WHERE id = 1").fetchone()
    if row is None:
        return False
    return bool(row["schema_ready"] if isinstance(row, sqlite3.Row) else row[0])


def mark_bootstrap_state_ready(db):
    # Persist the completed bootstrap marker once migrations and seed data succeed.
    db.execute(
        """
        INSERT INTO app_bootstrap_state (id, schema_ready, updated_at)
        VALUES (1, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            schema_ready = 1,
            updated_at = CURRENT_TIMESTAMP
        """
    )


def ensure_database_ready(db, *, force_schema_bootstrap=False):
    # Serialize schema bootstrap through SQLite itself so multi-process workers do not race each other.
    global SCHEMA_BOOTSTRAPPED
    if SCHEMA_BOOTSTRAPPED and not force_schema_bootstrap:
        return
    with SCHEMA_BOOTSTRAP_LOCK:
        if SCHEMA_BOOTSTRAPPED and not force_schema_bootstrap:
            return
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("BEGIN IMMEDIATE")
        if bootstrap_state_ready(db) and not force_schema_bootstrap:
            db.commit()
            SCHEMA_BOOTSTRAPPED = True
            return
        if force_schema_bootstrap:
            with open(os.path.join(BASE_DIR, "schema.sql"), "r", encoding="utf-8") as schema_file:
                db.executescript(schema_file.read())
        ensure_schema_updates(db)
        seed_default_admin(db)
        mark_bootstrap_state_ready(db)
        db.commit()
        SCHEMA_BOOTSTRAPPED = True


def login_required(view):
    # Redirect anonymous users before entering any member-only route.
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view):
    # Restrict sensitive admin pages and mutations to admins only.
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None or not g.user["is_admin"]:
            flash("Admin access is required.", "danger")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped_view


@app.before_request
def load_logged_in_user():
    # Hydrate the current user from the session for every request.
    user_id = session.get("user_id")
    g.user = query_db("SELECT * FROM users WHERE id = ?", (user_id,), one=True) if user_id else None


@app.before_request
def csrf_protect():
    # Lightweight CSRF protection for all form posts without extra dependencies.
    if request.method == "POST":
        token = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
        if not token or token != session.get("_csrf_token"):
            if request.is_json:
                return jsonify({"error": "Your session expired. Please try again."}), 400
            flash("Your session expired. Please try again.", "danger")
            return redirect(request.referrer or url_for("index"))


@app.context_processor
def inject_helpers():
    # Expose current_user and csrf_token() to every template automatically.
    from services.notifications import unread_message_count

    unread_total = unread_message_count(g.user["id"]) if g.get("user") else 0
    return {
        "current_user": g.get("user"),
        "csrf_token": get_csrf_token,
        "style_version": STYLE_VERSION,
        "user_initials": user_initials,
        "chat_unread_total": unread_total,
    }


@app.after_request
def disable_static_cache(response):
    # During local development, always fetch fresh HTML/CSS so browser snapshot state
    # cannot reopen old layouts halfway down the page.
    if request.path.startswith("/static/") or response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def get_csrf_token():
    # Store one CSRF token in the session and reuse it across forms.
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_socket_csrf(data):
    # Reuse the same session CSRF token for Socket.IO events that mutate chat state.
    token = (data or {}).get("csrf_token")
    if not token or token != session.get("_csrf_token"):
        emit("chat_error", {"error": "Your session expired. Refresh the page and try again."})
        return False
    return True


def validate_text(value, field_name, max_length, *, min_length=0, required=False):
    # Shared server-side validation for all user-provided text fields.
    cleaned = (value or "").strip()
    if required and not cleaned:
        raise ValueError(f"{field_name} is required.")
    if cleaned and len(cleaned) < min_length:
        raise ValueError(f"{field_name} must be at least {min_length} characters.")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters.")
    return cleaned


def validate_optional_url(value, field_name):
    # Accept common profile URLs, but reject incomplete root domains like github.com or linkedin.com.
    cleaned = validate_text(value, field_name, MAX_PROFILE_LINK_LENGTH)
    if not cleaned:
        return ""
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        cleaned = f"https://{cleaned}"

    parsed = urlparse(cleaned)
    host = (parsed.netloc or "").lower()
    normalized_path = (parsed.path or "").strip("/")

    allowed_hosts = {
        "GitHub link": {"github.com", "www.github.com"},
        "LinkedIn link": {"linkedin.com", "www.linkedin.com"},
    }
    invalid_messages = {
        "GitHub link": "Enter a valid GitHub profile link.",
        "LinkedIn link": "Enter a valid LinkedIn profile link.",
    }

    expected_hosts = allowed_hosts.get(field_name, set())
    if expected_hosts and host not in expected_hosts:
        raise ValueError(invalid_messages[field_name])

    if field_name == "GitHub link":
        if not normalized_path or "/" in normalized_path:
            raise ValueError("Enter a valid GitHub profile link.")
    elif field_name == "LinkedIn link":
        path_parts = [part for part in normalized_path.split("/") if part]
        if len(path_parts) < 2 or path_parts[0] not in {"in", "company", "school"}:
            raise ValueError("Enter a valid LinkedIn profile link.")

    return cleaned


def format_timestamp(value, *, include_date=False):
    # SQLite stores UTC timestamps, so convert them to the local display timezone.
    if not value:
        return ""
    normalized_value = str(value).strip()
    parsed = None
    for parser in (
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")),
        lambda raw: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            parsed = parser(normalized_value)
            break
        except ValueError:
            continue
    if parsed is None:
        return normalized_value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(DISPLAY_TIMEZONE)
    if include_date:
        return parsed.strftime("%d %b, %I:%M %p").lstrip("0").replace(" 0", " ")
    return parsed.strftime("%I:%M %p").lstrip("0")


def update_profile_fields(user_id, bio_value, availability_value, github_url_value="", linkedin_url_value="", certifications_value=""):
    # Profile setup and profile editing share one validation and persistence path.
    current_user = query_db(
        "SELECT bio, availability, github_url, linkedin_url, certifications FROM users WHERE id = ?",
        (user_id,),
        one=True,
    )
    bio = validate_text(current_user["bio"] if bio_value is None else bio_value, "Bio", MAX_BIO_LENGTH)
    availability = validate_text(
        current_user["availability"] if availability_value is None else availability_value,
        "Availability",
        MAX_AVAILABILITY_LENGTH,
    )
    github_url = validate_optional_url(
        current_user["github_url"] if github_url_value is None else github_url_value,
        "GitHub link",
    )
    linkedin_url = validate_optional_url(
        current_user["linkedin_url"] if linkedin_url_value is None else linkedin_url_value,
        "LinkedIn link",
    )
    certifications = validate_text(
        current_user["certifications"] if certifications_value is None else certifications_value,
        "Certifications",
        MAX_CERTIFICATIONS_LENGTH,
    )
    execute_db(
        """
        UPDATE users
        SET bio = ?, availability = ?, github_url = ?, linkedin_url = ?, certifications = ?
        WHERE id = ?
        """,
        (bio, availability, github_url, linkedin_url, certifications, user_id),
    )


def delete_file_if_exists(relative_path):
    # Remove a stored upload from disk when the user replaces or deletes it.
    if not relative_path:
        return
    absolute_path = os.path.join(BASE_DIR, relative_path)
    if os.path.exists(absolute_path):
        os.remove(absolute_path)
