PRAGMA foreign_keys = ON;

-- Core user records for authentication, profile text, and admin access.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    public_key TEXT DEFAULT '',
    profile_photo_path TEXT DEFAULT '',
    bio TEXT DEFAULT '',
    availability TEXT DEFAULT '',
    github_url TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '',
    certifications TEXT DEFAULT '',
    profile_setup_completed INTEGER NOT NULL DEFAULT 1,
    is_admin INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Shared skill catalog so multiple users can point at the same skill entry.
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    UNIQUE(name, category)
);

-- Join table that marks whether a user teaches or wants a specific skill.
CREATE TABLE IF NOT EXISTS user_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    skill_type TEXT NOT NULL CHECK(skill_type IN ('teach', 'learn')),
    level TEXT NOT NULL CHECK(level IN ('Beginner', 'Intermediate', 'Advanced')),
    UNIQUE(user_id, skill_id, skill_type),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(skill_id) REFERENCES skills(id)
);

-- Exchange requests connect two users around a pair of complementary skills.
CREATE TABLE IF NOT EXISTS exchange_requests (
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

-- Direct messages between two users after an exchange relationship exists.
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    attachment_name TEXT DEFAULT '',
    attachment_path TEXT DEFAULT '',
    attachment_kind TEXT DEFAULT '',
    attachment_mime TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMP,
    read_at TIMESTAMP,
    FOREIGN KEY(sender_id) REFERENCES users(id),
    FOREIGN KEY(receiver_id) REFERENCES users(id)
);

-- Registered browser/device keys used for simplified multi-device end-to-end encryption.
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
);

-- Uploaded proof files that appear on a member's public profile.
CREATE TABLE IF NOT EXISTS profile_certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

-- Reviews are only tied to completed/accepted exchanges.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer_id INTEGER NOT NULL,
    reviewee_id INTEGER NOT NULL,
    request_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    feedback TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(reviewer_id, request_id),
    FOREIGN KEY(reviewer_id) REFERENCES users(id),
    FOREIGN KEY(reviewee_id) REFERENCES users(id),
    FOREIGN KEY(request_id) REFERENCES exchange_requests(id)
);
