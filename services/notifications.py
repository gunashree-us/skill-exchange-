from core import ACTIVE_CONFERENCE_ROOMS, execute_db, format_timestamp, query_db, socketio, user_initials
from services.encryption import is_encrypted_message_body, message_preview


def rating_value(user_id):
    # Centralize review average lookups so routes do not repeat the same query.
    rating = query_db(
        "SELECT ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS total FROM reviews WHERE reviewee_id = ?",
        (user_id,),
        one=True,
    )
    return rating["avg_rating"] or 0, rating["total"]


def unread_message_count(user_id):
    # Used by the navbar notification badge and live chat updates.
    row = query_db(
        "SELECT COUNT(*) AS count FROM messages WHERE receiver_id = ? AND read_at IS NULL",
        (user_id,),
        one=True,
    )
    return row["count"] if row else 0


def active_devices_for_users(user_ids):
    # Return currently active device public keys for one or many users.
    if isinstance(user_ids, int):
        user_ids = [user_ids]
    user_ids = [int(user_id) for user_id in user_ids if int(user_id) > 0]
    if not user_ids:
        return {}
    placeholders = ", ".join("?" for _ in user_ids)
    rows = query_db(
        f"""
        SELECT id, user_id, device_token, label, public_key, last_seen_at, created_at
        FROM user_devices
        WHERE user_id IN ({placeholders}) AND revoked_at IS NULL
        ORDER BY created_at ASC, id ASC
        """,
        tuple(user_ids),
    )
    grouped = {user_id: [] for user_id in user_ids}
    for row in rows:
        grouped[row["user_id"]].append(
            {
                "id": row["id"],
                "device_token": row["device_token"],
                "label": row["label"],
                "public_key": row["public_key"],
                "last_seen_at": row["last_seen_at"],
                "created_at": row["created_at"],
            }
        )
    return grouped


def unread_thread_count(user_id, partner_id):
    # Count unread messages for a single conversation thread.
    row = query_db(
        """
        SELECT COUNT(*) AS count
        FROM messages
        WHERE sender_id = ? AND receiver_id = ? AND read_at IS NULL
        """,
        (partner_id, user_id),
        one=True,
    )
    return row["count"] if row else 0


def room_name_for_users(user_a, user_b):
    # Stable room name so both participants join the same Socket.IO room.
    low, high = sorted((int(user_a), int(user_b)))
    return f"chat:{low}:{high}"


def user_room_name(user_id):
    # Each connected user joins a personal room for global chat notification updates.
    return f"user:{int(user_id)}"


def conference_room_members(room_id):
    # Return the current participant records for a live conference room.
    room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
    if not room:
        return []
    participant_ids = sorted(room["participants"])
    if not participant_ids:
        return []
    placeholders = ", ".join("?" for _ in participant_ids)
    users = query_db(
        f"SELECT id, name FROM users WHERE id IN ({placeholders})",
        tuple(participant_ids),
    )
    users_by_id = {user["id"]: user for user in users}
    members = []
    for member_id in participant_ids:
        user = users_by_id.get(member_id)
        if user:
            members.append({"id": user["id"], "name": user["name"], "initials": user_initials(user["name"])})
    return members


def cleanup_conference_room(room_id):
    # Drop in-memory conference rooms once everyone has left.
    room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
    if room and not room["participants"]:
        ACTIVE_CONFERENCE_ROOMS.pop(room_id, None)


def emit_chat_notification(user_id, *, partner_id=None):
    # Push the latest unread counts to a specific user's connected sessions.
    payload = {"total_unread": unread_message_count(user_id)}
    if partner_id is not None:
        payload["partner_id"] = int(partner_id)
        payload["thread_unread"] = unread_thread_count(user_id, partner_id)
    socketio.emit("chat_notification", payload, room=user_room_name(user_id))


def accepted_chat_partners(user_id):
    # Only accepted exchanges open a chat thread, so users cannot message strangers.
    partners = query_db(
        """
        SELECT
            partner.id AS partner_id,
            partner.name AS partner_name,
            MAX(COALESCE(m.created_at, er.created_at)) AS latest_activity,
            (
                SELECT body
                FROM messages latest
                WHERE (latest.sender_id = ? AND latest.receiver_id = partner.id)
                   OR (latest.sender_id = partner.id AND latest.receiver_id = ?)
                ORDER BY latest.created_at DESC, latest.id DESC
                LIMIT 1
            ) AS last_message,
            (
                SELECT attachment_kind
                FROM messages latest
                WHERE (latest.sender_id = ? AND latest.receiver_id = partner.id)
                   OR (latest.sender_id = partner.id AND latest.receiver_id = ?)
                ORDER BY latest.created_at DESC, latest.id DESC
                LIMIT 1
            ) AS last_attachment_kind,
            (
                SELECT COUNT(*)
                FROM messages unread
                WHERE unread.sender_id = partner.id
                  AND unread.receiver_id = ?
                  AND unread.read_at IS NULL
            ) AS unread_count
        FROM exchange_requests er
        JOIN users partner ON partner.id = CASE
            WHEN er.sender_id = ? THEN er.receiver_id
            ELSE er.sender_id
        END
        LEFT JOIN messages m ON (
            (m.sender_id = ? AND m.receiver_id = partner.id)
            OR (m.sender_id = partner.id AND m.receiver_id = ?)
        )
        WHERE er.status = 'Accepted'
          AND (er.sender_id = ? OR er.receiver_id = ?)
        GROUP BY partner.id, partner.name
        ORDER BY latest_activity DESC
        """,
        (user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id),
    )
    threads = []
    for index, partner in enumerate(partners):
        threads.append(
            {
                "id": partner["partner_id"],
                "name": partner["partner_name"],
                "initials": user_initials(partner["partner_name"]),
                "preview": message_preview(partner["last_message"], partner["last_attachment_kind"]),
                "time": format_timestamp(partner["latest_activity"]),
                "unread": partner["unread_count"],
                "active": index == 0,
            }
        )
    return threads


def can_chat_with(user_id, partner_id):
    # Guard used by page, JSON, and send routes.
    return query_db(
        """
        SELECT 1
        FROM exchange_requests
        WHERE status = 'Accepted'
          AND ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
        LIMIT 1
        """,
        (user_id, partner_id, partner_id, user_id),
        one=True,
    ) is not None


def mark_conversation_read(reader_id, partner_id, *, up_to_id=None):
    # Mark inbound messages as read when the recipient opens or views the thread.
    if up_to_id is None:
        cursor = execute_db(
            """
            UPDATE messages
            SET delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP),
                read_at = CURRENT_TIMESTAMP
            WHERE sender_id = ? AND receiver_id = ? AND read_at IS NULL
            """,
            (partner_id, reader_id),
        )
        rows = query_db(
            """
            SELECT id
            FROM messages
            WHERE sender_id = ? AND receiver_id = ? AND read_at = (
                SELECT MAX(read_at) FROM messages WHERE sender_id = ? AND receiver_id = ?
            )
            ORDER BY id DESC
            LIMIT ?
            """,
            (partner_id, reader_id, partner_id, reader_id, cursor.rowcount or 0),
        )
    else:
        execute_db(
            """
            UPDATE messages
            SET delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP),
                read_at = CURRENT_TIMESTAMP
            WHERE sender_id = ? AND receiver_id = ? AND read_at IS NULL AND id <= ?
            """,
            (partner_id, reader_id, up_to_id),
        )
        rows = query_db(
            """
            SELECT id
            FROM messages
            WHERE sender_id = ? AND receiver_id = ? AND id <= ? AND read_at IS NOT NULL
            ORDER BY id DESC
            """,
            (partner_id, reader_id, up_to_id),
        )
    return [row["id"] for row in rows]


def load_messages(user_id, partner_id, *, limit=20, before_id=None):
    # Return conversation chunks in chronological order for lazy loading.
    sql = """
        SELECT id, sender_id, receiver_id, body, attachment_name, attachment_path, attachment_kind, attachment_mime, created_at, delivered_at, read_at
        FROM messages
        WHERE ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
    """
    args = [user_id, partner_id, partner_id, user_id]
    if before_id is not None:
        sql += " AND id < ?"
        args.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = [dict(message) for message in query_db(sql, tuple(args))]
    rows.reverse()
    return rows


def serialize_message(message, current_user_id):
    # Normalize message payloads for templates and Socket.IO clients.
    is_self = message["sender_id"] == current_user_id
    status = "Read" if message.get("read_at") else ("Delivered" if message.get("delivered_at") else "Sent")
    attachment_path = message.get("attachment_path") or ""
    return {
        "id": message["id"],
        "body": "Encrypted message. Unlock with your shared key." if is_encrypted_message_body(message["body"]) else message["body"],
        "raw_body": message["body"],
        "encrypted": is_encrypted_message_body(message["body"]),
        "attachment_name": message.get("attachment_name") or "",
        "attachment_kind": message.get("attachment_kind") or "",
        "attachment_mime": message.get("attachment_mime") or "",
        "attachment_url": f"/{attachment_path}" if attachment_path else "",
        "has_attachment": bool(attachment_path),
        "created_at": format_timestamp(message["created_at"], include_date=True),
        "delivered_at": message.get("delivered_at"),
        "read_at": message.get("read_at"),
        "side": "right" if is_self else "left",
        "status": status if is_self else "",
    }
