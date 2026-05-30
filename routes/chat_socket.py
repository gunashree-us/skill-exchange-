import secrets

from flask import request, session
from flask_socketio import emit, join_room, leave_room

from core import (
    ACTIVE_CHAT_ROOMS,
    ACTIVE_CONFERENCE_ROOMS,
    SID_CONFERENCE_ROOMS,
    SID_ROOMS,
    execute_db,
    query_db,
    socketio,
    validate_socket_csrf,
)
from services.encryption import validate_message_body
from services.notifications import (
    can_chat_with,
    cleanup_conference_room,
    conference_room_members,
    create_notification,
    emit_chat_notification,
    emit_incoming_call_notification,
    mark_conversation_read,
    room_name_for_users,
    serialize_message,
    user_room_name,
)


@socketio.on("join_chat")
def join_chat_room(data):
    # Join the live room for one accepted chat conversation and mark visible messages as read.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    if not user_id or partner_id <= 0 or not can_chat_with(user_id, partner_id):
        emit("chat_error", {"error": "Unable to open that conversation."})
        return

    room = room_name_for_users(user_id, partner_id)
    join_room(room)
    join_room(user_room_name(user_id))
    ACTIVE_CHAT_ROOMS[room].add(user_id)
    SID_ROOMS[request.sid] = {"room": room, "user_id": user_id}
    read_ids = mark_conversation_read(user_id, partner_id)
    if read_ids:
        emit("message_status", {"ids": read_ids, "status": "Read"}, room=room)
    emit_chat_notification(user_id, partner_id=partner_id)
    emit_chat_notification(partner_id, partner_id=user_id)


@socketio.on("register_notifications")
def register_notifications(data=None):
    # Every logged-in page joins a personal room so unread badges can update live.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    if not user_id:
        return
    join_room(user_room_name(user_id))
    emit_chat_notification(user_id)


@socketio.on("leave_chat")
def leave_chat_room(data):
    # Leave the active live-chat room when the client switches conversations.
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    if not user_id or partner_id <= 0:
        return
    room = room_name_for_users(user_id, partner_id)
    leave_room(room)
    ACTIVE_CHAT_ROOMS[room].discard(user_id)
    if not ACTIVE_CHAT_ROOMS[room]:
        ACTIVE_CHAT_ROOMS.pop(room, None)
    SID_ROOMS.pop(request.sid, None)


@socketio.on("typing")
def chat_typing(data):
    # Broadcast lightweight typing signals only to the active partner room.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    if not user_id or partner_id <= 0 or not can_chat_with(user_id, partner_id):
        return
    room = room_name_for_users(user_id, partner_id)
    emit(
        "typing",
        {"user_id": user_id, "is_typing": bool((data or {}).get("is_typing"))},
        room=room,
        include_self=False,
    )


@socketio.on("mark_read")
def chat_mark_read(data):
    # Mark visible inbound messages as read and push status updates to both clients.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    up_to_id = int((data or {}).get("up_to_id", 0))
    if not user_id or partner_id <= 0 or not can_chat_with(user_id, partner_id):
        return
    room = room_name_for_users(user_id, partner_id)
    read_ids = mark_conversation_read(user_id, partner_id, up_to_id=up_to_id or None)
    if read_ids:
        emit("message_status", {"ids": read_ids, "status": "Read"}, room=room)
    emit_chat_notification(user_id, partner_id=partner_id)
    emit_chat_notification(partner_id, partner_id=user_id)


def emit_call_event(event_name, user_id, partner_id, payload=None):
    # Shared relay for all WebRTC signaling messages within an accepted chat room.
    if not user_id or not partner_id or not can_chat_with(user_id, partner_id):
        emit("chat_error", {"error": "Unable to use video call for that conversation."})
        return False
    room = room_name_for_users(user_id, partner_id)
    emit(
        event_name,
        {"user_id": user_id, "partner_id": partner_id, **(payload or {})},
        room=room,
        include_self=False,
    )
    return True


def extract_signal_payload(data, field_name):
    # Relay either encrypted signaling envelopes or plain WebRTC signaling fields.
    payload = data or {}
    if payload.get("encrypted") and payload.get("secure_payload"):
        return {
            "encrypted": True,
            "secure_payload": payload.get("secure_payload"),
        }
    return {field_name: payload.get(field_name)}


def emit_conference_event(event_name, *, room_id, target_user_id=None, payload=None, include_self=False):
    # Relay conference signaling either to the room or to one invited participant.
    if target_user_id is not None:
        socketio.emit(event_name, {"room_id": room_id, **(payload or {})}, room=user_room_name(target_user_id))
    else:
        socketio.emit(event_name, {"room_id": room_id, **(payload or {})}, room=room_id, include_self=include_self)


@socketio.on("video_call_invite")
def video_call_invite(data):
    # Notify the accepted partner that a live video call is being started.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    if emit_call_event("video_call_invite", user_id, partner_id):
        caller = query_db("SELECT name FROM users WHERE id = ?", (user_id,), one=True) if user_id else None
        emit_incoming_call_notification(
            partner_id,
            caller_id=user_id,
            caller_name=(caller["name"] if caller else "Your exchange partner"),
        )


@socketio.on("video_call_accept")
def video_call_accept(data):
    # Tell the caller that the partner is ready to begin negotiation.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    emit_call_event("video_call_accept", user_id, partner_id)


@socketio.on("video_call_decline")
def video_call_decline(data):
    # Allow the partner to decline an incoming call request.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    emit_call_event("video_call_decline", user_id, partner_id)


@socketio.on("video_call_offer")
def video_call_offer(data):
    # Relay WebRTC SDP offers through the existing authenticated Socket.IO room.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    emit_call_event("video_call_offer", user_id, partner_id, extract_signal_payload(data, "offer"))


@socketio.on("video_call_answer")
def video_call_answer(data):
    # Relay WebRTC SDP answers back to the caller.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    emit_call_event("video_call_answer", user_id, partner_id, extract_signal_payload(data, "answer"))


@socketio.on("video_call_ice_candidate")
def video_call_ice_candidate(data):
    # Forward ICE candidates so peers can discover a direct connection path.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    emit_call_event("video_call_ice_candidate", user_id, partner_id, extract_signal_payload(data, "candidate"))


@socketio.on("video_call_end")
def video_call_end(data):
    # End the current video call for both participants.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    emit_call_event("video_call_end", user_id, partner_id)


@socketio.on("conference_start")
def conference_start(data):
    # Start a new group conference room and notify the selected accepted partners.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    participant_ids = []
    seen_participant_ids = set()
    for raw_partner_id in ((data or {}).get("participant_ids") or []):
        try:
            parsed_partner_id = int(raw_partner_id)
        except (TypeError, ValueError):
            emit("chat_error", {"error": "Conference participant list is invalid."})
            return
        if parsed_partner_id <= 0 or parsed_partner_id in seen_participant_ids:
            continue
        seen_participant_ids.add(parsed_partner_id)
        participant_ids.append(parsed_partner_id)
    room_id = (data or {}).get("room_id") or secrets.token_urlsafe(10)
    room_label = ((data or {}).get("room_label") or "").strip() or "SkillSwap Conference"
    if not user_id:
        emit("chat_error", {"error": "Please log in again to start a conference."})
        return
    if len(participant_ids) < 2:
        emit("chat_error", {"error": "Choose at least two partners to start a conference."})
        return
    invalid = [partner_id for partner_id in participant_ids if not can_chat_with(user_id, partner_id)]
    if invalid:
        emit("chat_error", {"error": "One or more selected partners are not available for conferencing."})
        return

    room = ACTIVE_CONFERENCE_ROOMS.setdefault(
        room_id,
        {
            "host_id": user_id,
            "allowed": set([user_id, *participant_ids]),
            "participants": set(),
            "label": room_label,
        },
    )
    room["allowed"].update(participant_ids)
    room["label"] = room_label

    join_room(room_id)
    room["participants"].add(user_id)
    SID_CONFERENCE_ROOMS[request.sid].add(room_id)
    emit(
        "conference_joined",
        {
            "room_id": room_id,
            "room_label": room_label,
            "members": conference_room_members(room_id),
            "host_id": user_id,
        },
    )

    inviter = query_db("SELECT id, name FROM users WHERE id = ?", (user_id,), one=True)
    for partner_id in participant_ids:
        emit_conference_event(
            "conference_invite",
            room_id=room_id,
            target_user_id=partner_id,
            payload={
                "room_label": room_label,
                "host_id": user_id,
                "host_name": inviter["name"] if inviter else "A partner",
            },
        )


@socketio.on("conference_join")
def conference_join(data):
    # Join an invited conference room and share the current roster with the client.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    room_id = (data or {}).get("room_id")
    room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
    if not user_id or not room or user_id not in room["allowed"]:
        emit("chat_error", {"error": "Unable to join that conference."})
        return

    join_room(room_id)
    room["participants"].add(user_id)
    SID_CONFERENCE_ROOMS[request.sid].add(room_id)
    members = conference_room_members(room_id)
    emit(
        "conference_joined",
        {
            "room_id": room_id,
            "room_label": room["label"],
            "members": members,
            "host_id": room["host_id"],
        },
    )
    emit_conference_event(
        "conference_participant_joined",
        room_id=room_id,
        payload={"participant": next((member for member in members if member["id"] == user_id), None)},
        include_self=False,
    )


@socketio.on("conference_decline")
def conference_decline(data):
    # Let the host know an invited participant declined the conference invite.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    room_id = (data or {}).get("room_id")
    room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
    if not user_id or not room:
        return
    emit_conference_event(
        "conference_declined",
        room_id=room_id,
        target_user_id=room["host_id"],
        payload={"user_id": user_id},
    )


@socketio.on("conference_offer")
def conference_offer(data):
    # Relay one peer's SDP offer to another participant in the same conference room.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    room_id = (data or {}).get("room_id")
    target_user_id = int((data or {}).get("target_user_id", 0))
    room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
    if not user_id or not room or user_id not in room["participants"] or target_user_id not in room["participants"]:
        return
    emit_conference_event(
        "conference_offer",
        room_id=room_id,
        target_user_id=target_user_id,
        payload={"sender_id": user_id, **extract_signal_payload(data, "offer")},
    )


@socketio.on("conference_answer")
def conference_answer(data):
    # Relay one peer's SDP answer back to the offer sender.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    room_id = (data or {}).get("room_id")
    target_user_id = int((data or {}).get("target_user_id", 0))
    room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
    if not user_id or not room or user_id not in room["participants"] or target_user_id not in room["participants"]:
        return
    emit_conference_event(
        "conference_answer",
        room_id=room_id,
        target_user_id=target_user_id,
        payload={"sender_id": user_id, **extract_signal_payload(data, "answer")},
    )


@socketio.on("conference_ice_candidate")
def conference_ice_candidate(data):
    # Forward ICE candidates between conference participants.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    room_id = (data or {}).get("room_id")
    target_user_id = int((data or {}).get("target_user_id", 0))
    room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
    if not user_id or not room or user_id not in room["participants"] or target_user_id not in room["participants"]:
        return
    emit_conference_event(
        "conference_ice_candidate",
        room_id=room_id,
        target_user_id=target_user_id,
        payload={"sender_id": user_id, **extract_signal_payload(data, "candidate")},
    )


@socketio.on("conference_leave")
def conference_leave(data):
    # Leave the conference room and notify the rest of the participants.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    room_id = (data or {}).get("room_id")
    room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
    if not user_id or not room:
        return
    leave_room(room_id)
    room["participants"].discard(user_id)
    SID_CONFERENCE_ROOMS[request.sid].discard(room_id)
    emit_conference_event("conference_participant_left", room_id=room_id, payload={"user_id": user_id})
    cleanup_conference_room(room_id)


@socketio.on("send_message")
def socket_send_message(data):
    # Persist a chat message once, then fan it out to both participants in real time.
    if not validate_socket_csrf(data):
        return
    user_id = session.get("user_id")
    partner_id = int((data or {}).get("partner_id", 0))
    if not user_id or partner_id <= 0 or not can_chat_with(user_id, partner_id):
        emit("chat_error", {"error": "Unable to send that message."})
        return

    try:
        body = validate_message_body((data or {}).get("body"))
    except ValueError as exc:
        emit("chat_error", {"error": str(exc)})
        return

    room = room_name_for_users(user_id, partner_id)
    partner_online = partner_id in ACTIVE_CHAT_ROOMS.get(room, set())
    if partner_online:
        cursor = execute_db(
            "INSERT INTO messages (sender_id, receiver_id, body, delivered_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (user_id, partner_id, body),
        )
    else:
        cursor = execute_db(
            "INSERT INTO messages (sender_id, receiver_id, body) VALUES (?, ?, ?)",
            (user_id, partner_id, body),
        )

    message = dict(query_db("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,), one=True))
    sender = query_db("SELECT name FROM users WHERE id = ?", (user_id,), one=True)
    emit("chat_message", serialize_message(message, user_id), to=request.sid)
    emit(
        "chat_message",
        serialize_message(message, partner_id),
        room=room,
        skip_sid=request.sid,
    )
    if partner_online:
        emit("message_status", {"ids": [message["id"]], "status": "Delivered"}, room=user_room_name(user_id))
    create_notification(
        partner_id,
        "chat",
        f"New chat message from {sender['name'] if sender else 'your exchange partner'}",
        body="Open chat to read the latest message.",
        href=f"/chat?partner_id={user_id}",
        actor_id=user_id,
        message_id=message["id"],
    )
    emit_chat_notification(user_id, partner_id=partner_id)
    emit_chat_notification(partner_id, partner_id=user_id)


@socketio.on("disconnect")
def disconnect_socket(_reason=None):
    # Clean up room membership when a browser tab closes or refreshes.
    payload = SID_ROOMS.pop(request.sid, None)
    if not payload:
        user_id = session.get("user_id")
    else:
        room = payload["room"]
        ACTIVE_CHAT_ROOMS[room].discard(payload["user_id"])
        if not ACTIVE_CHAT_ROOMS[room]:
            ACTIVE_CHAT_ROOMS.pop(room, None)
        user_id = payload["user_id"]

    conference_rooms = SID_CONFERENCE_ROOMS.pop(request.sid, set())
    for room_id in conference_rooms:
        room = ACTIVE_CONFERENCE_ROOMS.get(room_id)
        if not room or not user_id:
            continue
        room["participants"].discard(user_id)
        emit_conference_event("conference_participant_left", room_id=room_id, payload={"user_id": user_id})
        cleanup_conference_room(room_id)
