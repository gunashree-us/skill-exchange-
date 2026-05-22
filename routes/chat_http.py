import secrets

from flask import flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import emit, join_room, leave_room

from core import (
    ACTIVE_CHAT_ROOMS,
    ACTIVE_CONFERENCE_ROOMS,
    MAX_E2EE_REWRAP_BATCH,
    SID_CONFERENCE_ROOMS,
    SID_ROOMS,
    app,
    delete_file_if_exists,
    get_webrtc_ice_servers,
    get_db,
    login_required,
    query_db,
    execute_db,
    socketio,
    validate_socket_csrf,
)
from services.encryption import is_encrypted_message_body, validate_message_body
from services.notifications import (
    accepted_chat_partners,
    active_devices_for_users,
    can_chat_with,
    cleanup_conference_room,
    conference_room_members,
    emit_chat_notification,
    load_messages,
    mark_conversation_read,
    room_name_for_users,
    serialize_message,
    unread_thread_count,
    user_room_name,
)
from uploads import save_chat_media, validate_chat_media_upload


@app.route("/chat")
@login_required
def chat():
    # Show real conversations for accepted exchange partners.
    threads = accepted_chat_partners(g.user["id"])
    selected_partner_id = request.args.get("partner_id", type=int)
    if selected_partner_id is None and threads:
        selected_partner_id = threads[0]["id"]
    if selected_partner_id is not None and not can_chat_with(g.user["id"], selected_partner_id):
        flash("You can only chat with accepted exchange partners.", "danger")
        return redirect(url_for("chat"))

    active_thread = None
    for thread in threads:
        thread["active"] = thread["id"] == selected_partner_id
        if thread["active"]:
            active_thread = thread

    messages = []
    if active_thread:
        mark_conversation_read(g.user["id"], active_thread["id"])
        messages = [serialize_message(message, g.user["id"]) for message in load_messages(g.user["id"], active_thread["id"])]

    return render_template(
        "chat.html",
        threads=threads,
        active_thread=active_thread,
        messages=messages,
        chat_limit=20,
        webrtc_ice_servers=get_webrtc_ice_servers(),
        active_page="chat",
    )


@app.route("/chat/messages/<int:partner_id>")
@login_required
def chat_messages(partner_id):
    # JSON endpoint used for initial lazy loading or older-message fetches.
    if not can_chat_with(g.user["id"], partner_id):
        return jsonify({"error": "Chat partner not found."}), 404
    before_id = request.args.get("before_id", type=int)
    limit = min(request.args.get("limit", type=int) or 20, 50)
    if before_id is None:
        mark_conversation_read(g.user["id"], partner_id)
    messages = load_messages(g.user["id"], partner_id, limit=limit, before_id=before_id)
    return jsonify(
        {
            "messages": [serialize_message(message, g.user["id"]) for message in messages],
            "has_more": len(messages) == limit,
        }
    )


@app.route("/chat/send/<int:partner_id>", methods=["POST"])
@login_required
def send_chat_message(partner_id):
    # Accept text and optional attachment uploads, then fan the saved message out live.
    if not can_chat_with(g.user["id"], partner_id):
        if request.is_json:
            return jsonify({"error": "Chat partner not found."}), 404
        flash("You can only chat with accepted exchange partners.", "danger")
        return redirect(url_for("chat"))

    payload = request.get_json(silent=True) if request.is_json else request.form
    raw_body = payload.get("body") if payload else ""
    media_file = None if request.is_json else request.files.get("media")
    try:
        body = validate_message_body(raw_body, allow_empty=True)
        validate_chat_media_upload(media_file)
    except ValueError as exc:
        if request.is_json:
            return jsonify({"error": str(exc)}), 400
        flash(str(exc), "danger")
        return redirect(url_for("chat", partner_id=partner_id))
    attachment = save_chat_media(media_file) if media_file and media_file.filename else None
    if not body and not attachment:
        error_message = "Add a message or choose a file to send."
        if request.is_json:
            return jsonify({"error": error_message}), 400
        flash(error_message, "danger")
        return redirect(url_for("chat", partner_id=partner_id))

    room = room_name_for_users(g.user["id"], partner_id)
    partner_online = partner_id in ACTIVE_CHAT_ROOMS.get(room, set())
    delivered_at = "CURRENT_TIMESTAMP" if partner_online else "NULL"

    try:
        cursor = execute_db(
            f"""
            INSERT INTO messages (
                sender_id, receiver_id, body, attachment_name, attachment_path, attachment_kind, attachment_mime, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, {delivered_at})
            """,
            (
                g.user["id"],
                partner_id,
                body,
                (attachment or {}).get("name", ""),
                (attachment or {}).get("path", ""),
                (attachment or {}).get("kind", ""),
                (attachment or {}).get("mime", ""),
            ),
        )
    except Exception:
        if attachment:
            delete_file_if_exists(attachment["path"])
        raise
    message = dict(query_db("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,), one=True))
    socket_id = str((payload or {}).get("socket_id") or "").strip()
    socketio.emit(
        "chat_message",
        serialize_message(message, partner_id),
        to=room,
        skip_sid=socket_id or None,
    )
    if partner_online:
        socketio.emit("message_status", {"ids": [message["id"]], "status": "Delivered"}, room=user_room_name(g.user["id"]))
    emit_chat_notification(g.user["id"], partner_id=partner_id)
    emit_chat_notification(partner_id, partner_id=g.user["id"])
    if request.is_json or request.headers.get("X-Requested-With") == "fetch":
        return jsonify(serialize_message(message, g.user["id"]))
    return redirect(url_for("chat", partner_id=partner_id))


@app.route("/api/e2ee/register-device", methods=["POST"])
@login_required
def register_device():
    # Register or refresh the account-level chat key so encrypted chats can open on any signed-in device.
    payload = request.get_json(silent=True) or {}
    public_key = str(payload.get("public_key") or "").strip()
    private_key_wrapped = str(payload.get("private_key_wrapped") or "").strip()
    private_key_salt = str(payload.get("private_key_salt") or "").strip()
    if not public_key:
        return jsonify({"error": "Public key is required."}), 400
    if len(public_key) > 4096:
        return jsonify({"error": "Public key is too large."}), 400
    if len(private_key_wrapped) > 20000:
        return jsonify({"error": "Wrapped private key is too large."}), 400
    if len(private_key_salt) > 512:
        return jsonify({"error": "Private key salt is too large."}), 400

    existing_user = query_db(
        "SELECT private_key_wrapped, private_key_salt FROM users WHERE id = ?",
        (g.user["id"],),
        one=True,
    )
    wrapped_to_store = private_key_wrapped or (existing_user["private_key_wrapped"] if existing_user else "")
    salt_to_store = private_key_salt or (existing_user["private_key_salt"] if existing_user else "")
    if not wrapped_to_store or not salt_to_store:
        return jsonify({"error": "Wrapped account key is required."}), 400

    execute_db(
        """
        UPDATE users
        SET public_key = ?, private_key_wrapped = ?, private_key_salt = ?
        WHERE id = ?
        """,
        (public_key, wrapped_to_store, salt_to_store, g.user["id"]),
    )
    socketio.emit(
        "device_registered",
        {"user_id": g.user["id"], "device_id": g.user["id"]},
        room=user_room_name(g.user["id"]),
    )
    return jsonify(
        {
            "ok": True,
            "device_id": g.user["id"],
            "private_key_wrapped": wrapped_to_store,
            "private_key_salt": salt_to_store,
        }
    )


@app.route("/api/e2ee/device-peers", methods=["POST"])
@login_required
def device_peers():
    # Share account chat public keys for the current user and accepted chat partners only.
    payload = request.get_json(silent=True) or {}
    requested_ids = []
    for raw_user_id in (payload.get("user_ids") or []):
        try:
            parsed_user_id = int(raw_user_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid user id list."}), 400
        if parsed_user_id > 0:
            requested_ids.append(parsed_user_id)
    allowed_ids = {g.user["id"]}
    allowed_ids.update(thread["id"] for thread in accepted_chat_partners(g.user["id"]))
    filtered_ids = [user_id for user_id in requested_ids if user_id in allowed_ids]
    if g.user["id"] not in filtered_ids:
        filtered_ids.append(g.user["id"])
    placeholders = ", ".join("?" for _ in filtered_ids)
    rows = query_db(
        f"""
        SELECT id, public_key, private_key_wrapped, private_key_salt
        FROM users
        WHERE id IN ({placeholders})
        """,
        tuple(filtered_ids),
    )
    devices = {user_id: [] for user_id in filtered_ids}
    account_keys = {}
    for row in rows:
        if row["public_key"]:
            devices[row["id"]].append(
                {
                    "id": row["id"],
                    "label": "Account key",
                    "public_key": row["public_key"],
                }
            )
        account_keys[row["id"]] = {
            "public_key": row["public_key"] or "",
            "private_key_wrapped": row["private_key_wrapped"] or "",
            "private_key_salt": row["private_key_salt"] or "",
        }
    return jsonify({"devices": devices, "account_keys": account_keys})


@app.route("/api/e2ee/messages/rewrap", methods=["POST"])
@login_required
def rewrap_message_keys():
    # Let one trusted device add newly registered same-user devices to existing encrypted messages.
    payload = request.get_json(silent=True) or {}
    try:
        partner_id = int(payload.get("partner_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid chat partner."}), 400
    if partner_id <= 0 or not can_chat_with(g.user["id"], partner_id):
        return jsonify({"error": "Chat partner not found."}), 404

    raw_updates = payload.get("updates") or []
    if not isinstance(raw_updates, list) or len(raw_updates) > MAX_E2EE_REWRAP_BATCH:
        return jsonify({"error": "Invalid sync batch."}), 400

    updates = []
    message_ids = []
    for item in raw_updates:
        if not isinstance(item, dict):
            return jsonify({"error": "Invalid sync payload."}), 400
        try:
            message_id = int(item.get("id") or 0)
            body = validate_message_body(item.get("body"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid encrypted message sync payload."}), 400
        if message_id <= 0 or not is_encrypted_message_body(body):
            return jsonify({"error": "Only encrypted messages can be synced."}), 400
        updates.append((message_id, body))
        message_ids.append(message_id)

    if not updates:
        return jsonify({"ok": True, "updated": 0})

    placeholders = ", ".join("?" for _ in message_ids)
    valid_rows = query_db(
        f"""
        SELECT id
        FROM messages
        WHERE id IN ({placeholders})
          AND ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
        """,
        tuple(message_ids) + (g.user["id"], partner_id, partner_id, g.user["id"]),
    )
    valid_ids = {row["id"] for row in valid_rows}
    if len(valid_ids) != len(set(message_ids)):
        return jsonify({"error": "One or more messages could not be synced."}), 400

    db = get_db()
    db.executemany(
        "UPDATE messages SET body = ? WHERE id = ?",
        [(body, message_id) for message_id, body in updates if message_id in valid_ids],
    )
    db.commit()
    return jsonify({"ok": True, "updated": len(valid_ids)})
