import json
import os
import secrets
import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from threading import Lock
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
import psycopg
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
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


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_database_config():
    # Require PostgreSQL everywhere so local and deployed environments behave the same way.
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required.\n"
            "Example:\n"
            "  export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/skill_exchange"
        )
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("DATABASE_URL must use the postgres:// or postgresql:// scheme.")
    normalized_url = re.sub(r"^postgres://", "postgresql://", database_url, count=1)
    return {"dsn": normalized_url}


DATABASE_CONFIG = resolve_database_config()
IntegrityError = psycopg.IntegrityError

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
app.config["DATABASE_DSN"] = DATABASE_CONFIG["dsn"]
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
UPLOAD_CHAT_MEDIA_EXTENSIONS = {
    "png", "jpg", "jpeg", "webp", "gif",
    "mp4", "mov", "m4v", "webm",
    "mp3", "wav", "ogg", "m4a", "aac",
    "pdf", "zip",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
}
ACTIVE_CHAT_ROOMS = defaultdict(set)
SID_ROOMS = {}
ACTIVE_CONFERENCE_ROOMS = {}
SID_CONFERENCE_ROOMS = defaultdict(set)
DISPLAY_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kolkata"))
UPLOADS_DIR = os.path.join(BASE_DIR, "static", "uploads")
PROFILE_UPLOADS_DIR = os.path.join(UPLOADS_DIR, "profiles")
CERTIFICATE_UPLOADS_DIR = os.path.join(UPLOADS_DIR, "certificates")
CHAT_MEDIA_UPLOADS_DIR = os.path.join(UPLOADS_DIR, "chat_media")
SCHEMA_BOOTSTRAP_LOCK = Lock()
SCHEMA_BOOTSTRAPPED = False
POSTGRES_BOOTSTRAP_LOCK_ID = 314159


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


def _normalize_ice_servers(ice_servers):
    normalized_servers = []
    for server in ice_servers or []:
        urls = server.get("urls") if isinstance(server, dict) else None
        if isinstance(urls, str):
            urls = [urls]
        filtered_urls = []
        for url in urls or []:
            cleaned = str(url).strip()
            if not cleaned:
                continue
            # Cloudflare warns that browser TURN URLs using alternate port 53 can stall ICE gathering.
            if ":53" in cleaned:
                continue
            filtered_urls.append(cleaned)
        if not filtered_urls:
            continue
        payload = {"urls": filtered_urls}
        if isinstance(server, dict) and server.get("username"):
            payload["username"] = server["username"]
        if isinstance(server, dict) and server.get("credential"):
            payload["credential"] = server["credential"]
        normalized_servers.append(payload)
    return normalized_servers


def cloudflare_turn_credentials_configured():
    return bool(os.environ.get("CLOUDFLARE_TURN_KEY_ID") and os.environ.get("CLOUDFLARE_TURN_API_TOKEN"))


def get_runtime_webrtc_ice_servers():
    # Prefer short-lived Cloudflare TURN credentials when configured; otherwise use static ICE config.
    if not cloudflare_turn_credentials_configured():
        return get_webrtc_ice_servers()

    turn_key_id = os.environ["CLOUDFLARE_TURN_KEY_ID"].strip()
    api_token = os.environ["CLOUDFLARE_TURN_API_TOKEN"].strip()
    ttl_seconds = max(300, min(int(os.environ.get("CLOUDFLARE_TURN_TTL_SECONDS", "86400")), 86400))
    request_body = json.dumps({"ttl": ttl_seconds}).encode("utf-8")
    api_request = urllib_request.Request(
        f"https://rtc.live.cloudflare.com/v1/turn/keys/{turn_key_id}/credentials/generate-ice-servers",
        data=request_body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(api_request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
        return get_webrtc_ice_servers()
    return _normalize_ice_servers(payload.get("iceServers")) or get_webrtc_ice_servers()


def repair_legacy_secure_chat_state(db):
    # Clear migrated or partial secure-chat account keys that would break automatic chat encryption.
    db.execute(
        """
        UPDATE users
        SET public_key = '',
            private_key_wrapped = '',
            private_key_salt = ''
        WHERE
            (
                COALESCE(public_key, '') <> ''
                AND (
                    COALESCE(private_key_wrapped, '') = ''
                    OR COALESCE(private_key_salt, '') = ''
                )
            )
            OR (
                (
                    COALESCE(private_key_wrapped, '') <> ''
                    OR COALESCE(private_key_salt, '') <> ''
                )
                AND COALESCE(public_key, '') = ''
            )
            OR (COALESCE(public_key, '') <> '' AND LENGTH(public_key) < 100)
            OR (COALESCE(private_key_wrapped, '') <> '' AND LENGTH(private_key_wrapped) < 100)
            OR (COALESCE(private_key_salt, '') <> '' AND LENGTH(private_key_salt) < 8)
        """
    )
    db.execute(
        """
        DELETE FROM user_devices
        WHERE user_id IN (
            SELECT id
            FROM users
            WHERE COALESCE(public_key, '') = ''
              AND COALESCE(private_key_wrapped, '') = ''
              AND COALESCE(private_key_salt, '') = ''
        )
        """
    )


def _normalize_query(query):
    normalized = query.replace("?", "%s")
    had_insert_ignore = "INSERT OR IGNORE INTO" in normalized.upper()
    if had_insert_ignore:
        normalized = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", normalized, flags=re.IGNORECASE)
        if "ON CONFLICT" not in normalized.upper():
            normalized = f"{normalized.rstrip()} ON CONFLICT DO NOTHING"
    return normalized, had_insert_ignore


def _fetch_lastrowid(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get("id")
    return row[0]


class CursorResult:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = getattr(cursor, "rowcount", None)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()


def execute_schema_script(db, script):
    for statement in [chunk.strip() for chunk in script.split(";") if chunk.strip()]:
        db.execute(statement)


def get_db():
    # Keep one database connection per request and expose rows like dictionaries.
    if "db" not in g:
        g.db = psycopg.connect(app.config["DATABASE_DSN"], row_factory=dict_row)
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
    normalized_query, _ = _normalize_query(query)
    rows = get_db().execute(normalized_query, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def execute_db(query, args=()):
    # Shared helper for INSERT/UPDATE/DELETE queries with an immediate commit.
    db = get_db()
    normalized_query, _ = _normalize_query(query)
    upper_query = normalized_query.lstrip().upper()
    insert_query = upper_query.startswith("INSERT INTO")
    if insert_query and "RETURNING" not in upper_query:
        normalized_query = f"{normalized_query.rstrip()} RETURNING id"
    cursor = db.execute(normalized_query, args)
    lastrowid = getattr(cursor, "lastrowid", None)
    if insert_query:
        lastrowid = _fetch_lastrowid(cursor.fetchone())
    db.commit()
    return CursorResult(cursor, lastrowid=lastrowid)


def query_user_by_email(email, *, one=True):
    # Match email addresses case-insensitively so migrated or legacy accounts can still sign in.
    cleaned_email = (email or "").strip().lower()
    if not cleaned_email:
        return None if one else []
    return query_db("SELECT * FROM users WHERE LOWER(email) = ?", (cleaned_email,), one=one)


def table_exists(db, table_name):
    # Detect whether the base schema table already exists before running migrations.
    row = db.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_schema_updates(db):
    # Backfill new columns for local databases created before recent features existed.
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS public_key TEXT DEFAULT ''")
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS private_key_wrapped TEXT DEFAULT ''")
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS private_key_salt TEXT DEFAULT ''")
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_photo_path TEXT DEFAULT ''")
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_url TEXT DEFAULT ''")
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS linkedin_url TEXT DEFAULT ''")
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS certifications TEXT DEFAULT ''")
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_setup_completed INTEGER NOT NULL DEFAULT 1")
    db.execute("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS proposed_time TEXT DEFAULT ''")
    db.execute("ALTER TABLE exchange_requests ADD COLUMN IF NOT EXISTS duration_minutes INTEGER NOT NULL DEFAULT 60")
    db.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP")
    db.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS attachment_name TEXT DEFAULT ''")
    db.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS attachment_path TEXT DEFAULT ''")
    db.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS attachment_kind TEXT DEFAULT ''")
    db.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS attachment_mime TEXT DEFAULT ''")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_devices (
            id SERIAL PRIMARY KEY,
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
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            actor_id INTEGER,
            request_id INTEGER,
            message_id INTEGER,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            href TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            read_at TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(actor_id) REFERENCES users(id),
            FOREIGN KEY(request_id) REFERENCES exchange_requests(id),
            FOREIGN KEY(message_id) REFERENCES messages(id)
        )
        """
    )


def user_initials(name):
    # Used by the UI anywhere we need a simple text avatar.
    parts = [part for part in name.split() if part]
    return "".join(part[0].upper() for part in parts[:2]) or "U"


def init_db():
    # Load the canonical schema from disk and seed the default admin account once.
    db = psycopg.connect(app.config["DATABASE_DSN"], row_factory=dict_row)
    try:
        ensure_database_ready(db)
    finally:
        db.close()


def seed_default_admin(db):
    # Keep the default admin account available for local development.
    if not env_flag("SEED_DEFAULT_ADMIN", DEBUG_MODE):
        return
    lookup_query, _ = _normalize_query("SELECT id FROM users WHERE LOWER(email) = ?")
    admin = db.execute(lookup_query, ("admin@skillx.local",)).fetchone()
    if admin is None:
        insert_query, _ = _normalize_query(
            "INSERT INTO users (name, email, password_hash, bio, is_admin) VALUES (?, ?, ?, ?, 1)"
        )
        db.execute(
            insert_query,
            ("Admin", "admin@skillx.local", generate_password_hash("admin123"), "Platform administrator"),
        )


def bootstrap_state_ready(db):
    # Keep schema bootstrap state in the database so multiple worker processes share one source of truth.
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
    return bool(row["schema_ready"])


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
    # Serialize schema bootstrap through the database so multi-process workers do not race each other.
    global SCHEMA_BOOTSTRAPPED
    if SCHEMA_BOOTSTRAPPED and not force_schema_bootstrap:
        return
    with SCHEMA_BOOTSTRAP_LOCK:
        if SCHEMA_BOOTSTRAPPED and not force_schema_bootstrap:
            return
        db.execute("SELECT pg_advisory_xact_lock(%s)", (POSTGRES_BOOTSTRAP_LOCK_ID,))
        if bootstrap_state_ready(db) and not force_schema_bootstrap:
            db.commit()
            SCHEMA_BOOTSTRAPPED = True
            return
        if force_schema_bootstrap or not table_exists(db, "users") or not table_exists(db, "exchange_requests"):
            with open(os.path.join(BASE_DIR, "schema_postgres.sql"), "r", encoding="utf-8") as schema_file:
                execute_schema_script(db, schema_file.read())
        ensure_schema_updates(db)
        repair_legacy_secure_chat_state(db)
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
            expects_json = request.is_json or request.headers.get("X-Requested-With") == "fetch"
            if expects_json:
                return jsonify({"error": "Your session expired. Please try again."}), 400
            flash("Your session expired. Please try again.", "danger")
            return redirect(request.referrer or url_for("index"))


@app.before_request
def redirect_admins_to_admin_panel():
    # Keep admin sessions inside the admin workspace even if a stale member URL opens after login.
    if g.get("user") is None or not g.user["is_admin"]:
        return None
    if request.method not in {"GET", "HEAD"}:
        return None
    if request.blueprint == "static" or request.path.startswith("/static/") or request.path.startswith("/socket.io/"):
        return None
    if request.endpoint in {None, "admin", "toggle_admin", "logout"}:
        return None
    member_endpoints = {
        "index",
        "dashboard",
        "browse",
        "matches",
        "chat",
        "notifications",
        "notifications_read_all",
        "notification_read",
        "profile",
        "profile_setup",
        "skills",
        "requests_view",
    }
    if request.endpoint in member_endpoints:
        return redirect(url_for("admin"))
    return None


@app.context_processor
def inject_helpers():
    # Expose current_user and csrf_token() to every template automatically.
    from services.notifications import actionable_request_count, notification_unread_count, unread_message_count

    unread_total = unread_message_count(g.user["id"]) if g.get("user") else 0
    request_alert_total = actionable_request_count(g.user["id"]) if g.get("user") else 0
    notification_total = notification_unread_count(g.user["id"]) if g.get("user") else 0
    return {
        "current_user": g.get("user"),
        "csrf_token": get_csrf_token,
        "style_version": STYLE_VERSION,
        "user_initials": user_initials,
        "chat_unread_total": unread_total,
        "request_alert_total": request_alert_total,
        "notification_unread_total": notification_total,
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
    # Database timestamps are stored in UTC, so convert them to the local display timezone.
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
