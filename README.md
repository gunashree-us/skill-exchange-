# Skill Exchange Platform

A Flask + PostgreSQL app for peer-to-peer skill barter.

## Features

- User registration and login
- Profile management with skills offered and wanted
- Mutual skill matching
- Exchange requests with accept/reject flow
- Reviews and trust rating
- Basic admin dashboard

## Database

The app uses PostgreSQL only.

Create the database manually with:

```bash
python init_db.py
```

This bootstraps your PostgreSQL database using [`schema_postgres.sql`](/Applications/skill%20exchange/schema_postgres.sql).

Example:

```bash
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/skill_exchange
```

## Run locally

1. Create a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Set a local secret key using either:

```bash
export SECRET_KEY=local-dev-secret
```

or create a `.env` file in the project root:

```env
SECRET_KEY=local-dev-secret
```

4. Set your PostgreSQL connection string:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/skill_exchange
```

5. Initialize the database with `python init_db.py`.
6. Start the app with `python app.py`.
7. Open `http://127.0.0.1:5000`.

Recommended full setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
export SECRET_KEY=local-dev-secret
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/skill_exchange
python3 init_db.py
python3 app.py
```

## Video calls over Cloudflare / mobile networks

Video calls use browser WebRTC. The app defaults to public STUN servers, which works on many local Wi-Fi networks but can fail on mobile data, college Wi-Fi, strict NATs, or Cloudflare Tunnel demos.

For a permanent fix, configure TURN. The app now supports two options:

1. Static TURN provider credentials

```env
WEBRTC_ICE_SERVERS=[{"urls":["stun:stun.l.google.com:19302"]},{"urls":["turn:YOUR_TURN_HOST:3478"],"username":"YOUR_TURN_USERNAME","credential":"YOUR_TURN_PASSWORD"}]
```

2. Dynamic Cloudflare TURN credentials

Create a TURN key in Cloudflare Realtime, then add:

```env
CLOUDFLARE_TURN_KEY_ID=your_turn_key_id
CLOUDFLARE_TURN_API_TOKEN=your_turn_api_token
CLOUDFLARE_TURN_TTL_SECONDS=86400
```

When these Cloudflare variables are set, the app fetches fresh short-lived TURN `iceServers` from Cloudflare at runtime and uses them for video calls automatically.

After adding either setup, restart `python3 app.py`.

Optional local default admin account:

Set this only for local development if you want a seeded admin user:

```env
SEED_DEFAULT_ADMIN=true
```

When enabled, the seeded admin account is:

- Email: `admin@skillx.local`
- Password: `admin123`
