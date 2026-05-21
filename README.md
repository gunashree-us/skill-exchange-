# Skill Exchange Platform

A Flask + SQLite MVP for peer-to-peer skill barter.

## Features

- User registration and login
- Profile management with skills offered and wanted
- Mutual skill matching
- Exchange requests with accept/reject flow
- Reviews and trust rating
- Basic admin dashboard

## Database

The project uses SQLite.

Create the database manually with:

```bash
python init_db.py
```

This creates `skill_exchange.db` using [`schema.sql`](/Applications/skill%20exchange/schema.sql).

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

4. Initialize the database with `python init_db.py`.
5. Start the app with `python app.py`.
6. Open `http://127.0.0.1:5000`.

Recommended full setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
export SECRET_KEY=local-dev-secret
python3 init_db.py
python3 app.py
```

## Video calls over Cloudflare / mobile networks

Video calls use browser WebRTC. The app defaults to public STUN servers, which works on many local Wi-Fi networks but can fail on mobile data, college Wi-Fi, strict NATs, or Cloudflare Tunnel demos. If a remote video tile stays blank or shows "Connection is unstable", configure a TURN relay:

```env
WEBRTC_ICE_SERVERS=[{"urls":["stun:stun.l.google.com:19302"]},{"urls":["turn:YOUR_TURN_HOST:3478"],"username":"YOUR_TURN_USERNAME","credential":"YOUR_TURN_PASSWORD"}]
```

Add that line to `.env`, replace the TURN values with your provider credentials, then restart `python3 app.py`.

Default admin account:

- Email: `admin@skillx.local`
- Password: `admin123`
