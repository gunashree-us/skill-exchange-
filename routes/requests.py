import sqlite3

from flask import flash, g, redirect, render_template, request, url_for

from core import (
    MAX_MESSAGE_LENGTH,
    MAX_REVIEW_LENGTH,
    MAX_SCHEDULE_LENGTH,
    REQUEST_DURATIONS,
    app,
    execute_db,
    login_required,
    query_db,
    validate_text,
)


@app.route("/requests/send/<int:receiver_id>", methods=["POST"])
@login_required
def send_request(receiver_id):
    # Create an exchange request only when both sides' selected skills still line up.
    try:
        teach_skill_id = int(request.form.get("teach_skill_id", "0"))
        learn_skill_id = int(request.form.get("learn_skill_id", "0"))
        request_mode = (request.form.get("request_mode") or "mutual").strip().lower()
        message = validate_text(request.form.get("message"), "Message", MAX_MESSAGE_LENGTH)
        schedule_note = validate_text(request.form.get("schedule_note"), "Schedule note", MAX_SCHEDULE_LENGTH)
        proposed_time = validate_text(request.form.get("proposed_time"), "Proposed time", 32)
        duration_minutes = int(request.form.get("duration_minutes", "60"))
        if teach_skill_id <= 0 or learn_skill_id <= 0:
            raise ValueError("Select both the skill you offer and the skill you want.")
        if receiver_id == g.user["id"]:
            raise ValueError("You cannot send a request to yourself.")
        receiver = query_db("SELECT 1 FROM users WHERE id = ?", (receiver_id,), one=True)
        if receiver is None:
            raise ValueError("That member is no longer available.")
        if duration_minutes not in REQUEST_DURATIONS:
            raise ValueError("Choose a valid session duration.")
        owned_teach = query_db(
            "SELECT 1 FROM user_skills WHERE user_id = ? AND skill_id = ? AND skill_type = 'teach'",
            (g.user["id"], teach_skill_id),
            one=True,
        )
        target_wants = query_db(
            "SELECT 1 FROM user_skills WHERE user_id = ? AND skill_id = ? AND skill_type = 'learn'",
            (receiver_id, teach_skill_id),
            one=True,
        )
        target_teaches = query_db(
            "SELECT 1 FROM user_skills WHERE user_id = ? AND skill_id = ? AND skill_type = 'teach'",
            (receiver_id, learn_skill_id),
            one=True,
        )
        requested_skill_exists = query_db(
            "SELECT 1 FROM skills WHERE id = ?",
            (learn_skill_id,),
            one=True,
        )
        own_learn = query_db(
            "SELECT 1 FROM user_skills WHERE user_id = ? AND skill_id = ? AND skill_type = 'learn'",
            (g.user["id"], learn_skill_id),
            one=True,
        )
        if request_mode == "intro":
            if any(item is None for item in (owned_teach, target_teaches, requested_skill_exists)):
                raise ValueError("The selected skills are no longer valid for this introduction request.")
        elif any(item is None for item in (owned_teach, target_wants, target_teaches, own_learn)):
            raise ValueError("The selected skills are no longer valid for this exchange.")
        existing_request = query_db(
            """
            SELECT id, status
            FROM exchange_requests
            WHERE (
                (
                    sender_id = ?
                    AND receiver_id = ?
                    AND teach_skill_id = ?
                    AND learn_skill_id = ?
                )
                OR (
                    sender_id = ?
                    AND receiver_id = ?
                    AND teach_skill_id = ?
                    AND learn_skill_id = ?
                )
            )
              AND status IN ('Pending', 'Countered', 'Accepted')
            ORDER BY CASE status WHEN 'Accepted' THEN 0 WHEN 'Pending' THEN 1 ELSE 2 END,
                     created_at DESC,
                     id DESC
            LIMIT 1
            """,
            (
                g.user["id"], receiver_id, teach_skill_id, learn_skill_id,
                receiver_id, g.user["id"], learn_skill_id, teach_skill_id,
            ),
            one=True,
        )
        if existing_request is not None:
            raise ValueError(f"This exchange request is already {existing_request['status'].lower()}.")
        try:
            execute_db(
                """
                INSERT INTO exchange_requests (
                    sender_id, receiver_id, teach_skill_id, learn_skill_id, message, schedule_note, proposed_time, duration_minutes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (g.user["id"], receiver_id, teach_skill_id, learn_skill_id, message, schedule_note, proposed_time, duration_minutes),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("The selected skills are no longer valid for this exchange.") from exc
        flash("Exchange request sent.", "success")
    except (TypeError, ValueError) as exc:
        flash(str(exc) if str(exc) else "Unable to send exchange request.", "danger")
    return redirect(url_for("requests_view"))


@app.route("/requests")
@login_required
def requests_view():
    # Split requests into received, sent, and pending-review sections.
    received = query_db(
        """
        WITH ranked_requests AS (
            SELECT er.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY sender_id, receiver_id, teach_skill_id, learn_skill_id
                       ORDER BY CASE status WHEN 'Accepted' THEN 0 WHEN 'Countered' THEN 1 WHEN 'Pending' THEN 2 ELSE 3 END,
                                created_at DESC,
                                id DESC
                   ) AS request_rank
            FROM exchange_requests er
            WHERE er.receiver_id = ?
        )
        SELECT er.*, u.name AS sender_name, ts.name AS offer_skill, ls.name AS request_skill
        FROM ranked_requests er
        JOIN users u ON u.id = er.sender_id
        JOIN skills ts ON ts.id = er.teach_skill_id
        JOIN skills ls ON ls.id = er.learn_skill_id
        WHERE er.request_rank = 1
        ORDER BY er.created_at DESC
        """,
        (g.user["id"],),
    )
    sent = query_db(
        """
        WITH ranked_requests AS (
            SELECT er.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY sender_id, receiver_id, teach_skill_id, learn_skill_id
                       ORDER BY CASE status WHEN 'Accepted' THEN 0 WHEN 'Countered' THEN 1 WHEN 'Pending' THEN 2 ELSE 3 END,
                                created_at DESC,
                                id DESC
                   ) AS request_rank
            FROM exchange_requests er
            WHERE er.sender_id = ?
        )
        SELECT er.*, u.name AS receiver_name, ts.name AS offer_skill, ls.name AS request_skill
        FROM ranked_requests er
        JOIN users u ON u.id = er.receiver_id
        JOIN skills ts ON ts.id = er.teach_skill_id
        JOIN skills ls ON ls.id = er.learn_skill_id
        WHERE er.request_rank = 1
        ORDER BY er.created_at DESC
        """,
        (g.user["id"],),
    )
    reviewable = query_db(
        """
        SELECT er.id, er.sender_id, er.receiver_id, u.name AS partner_name
        FROM exchange_requests er
        JOIN users u ON u.id = CASE WHEN er.sender_id = ? THEN er.receiver_id ELSE er.sender_id END
        WHERE (er.sender_id = ? OR er.receiver_id = ?)
          AND er.status = 'Accepted'
          AND er.id NOT IN (SELECT request_id FROM reviews WHERE reviewer_id = ?)
        ORDER BY er.created_at DESC
        """,
        (g.user["id"], g.user["id"], g.user["id"], g.user["id"]),
    )
    return render_template(
        "requests.html",
        received=received,
        sent=sent,
        reviewable=reviewable,
        durations=REQUEST_DURATIONS,
        active_page="requests",
    )


@app.route("/requests/<int:request_id>/status", methods=["POST"])
@login_required
def update_request_status(request_id):
    # The relevant decision-maker can accept or reject the current proposal state.
    status = request.form.get("status")
    if status not in {"Accepted", "Rejected"}:
        flash("Invalid request status.", "danger")
        return redirect(url_for("requests_view"))
    exchange_request = query_db("SELECT * FROM exchange_requests WHERE id = ?", (request_id,), one=True)
    if exchange_request is None:
        flash("Request not found.", "danger")
        return redirect(url_for("requests_view"))
    allowed_user_id = exchange_request["receiver_id"] if exchange_request["status"] == "Pending" else exchange_request["sender_id"]
    if exchange_request["status"] not in {"Pending", "Countered"} or allowed_user_id != g.user["id"]:
        flash("You cannot update this request right now.", "danger")
        return redirect(url_for("requests_view"))
    execute_db("UPDATE exchange_requests SET status = ? WHERE id = ?", (status, request_id))
    flash(f"Request {status.lower()}.", "success")
    return redirect(url_for("requests_view"))


@app.route("/requests/<int:request_id>/counter", methods=["POST"])
@login_required
def counter_request(request_id):
    # Let the receiver propose a concrete alternative time and duration instead of a binary reject.
    exchange_request = query_db("SELECT * FROM exchange_requests WHERE id = ?", (request_id,), one=True)
    if exchange_request is None or exchange_request["status"] != "Pending" or exchange_request["receiver_id"] != g.user["id"]:
        flash("Counter-offer is not available for this request.", "danger")
        return redirect(url_for("requests_view"))

    try:
        proposed_time = validate_text(request.form.get("proposed_time"), "Counter time", 32)
        schedule_note = validate_text(request.form.get("schedule_note"), "Counter note", MAX_SCHEDULE_LENGTH)
        duration_minutes = int(request.form.get("duration_minutes", "60"))
        if duration_minutes not in REQUEST_DURATIONS:
            raise ValueError("Choose a valid session duration.")
        execute_db(
            """
            UPDATE exchange_requests
            SET status = 'Countered', proposed_time = ?, duration_minutes = ?, schedule_note = ?
            WHERE id = ?
            """,
            (proposed_time, duration_minutes, schedule_note, request_id),
        )
        flash("Counter-offer sent.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("requests_view"))


@app.route("/reviews/add/<int:request_id>", methods=["POST"])
@login_required
def add_review(request_id):
    # Reviews are only allowed after an accepted exchange involving the current user.
    req = query_db(
        "SELECT * FROM exchange_requests WHERE id = ? AND (sender_id = ? OR receiver_id = ?) AND status = 'Accepted'",
        (request_id, g.user["id"], g.user["id"]),
        one=True,
    )
    if req is None:
        flash("Review not allowed for this exchange.", "danger")
        return redirect(url_for("requests_view"))

    reviewee_id = req["receiver_id"] if req["sender_id"] == g.user["id"] else req["sender_id"]
    try:
        rating = int(request.form.get("rating", "0"))
        feedback = validate_text(request.form.get("feedback"), "Feedback", MAX_REVIEW_LENGTH)
        if rating not in {1, 2, 3, 4, 5}:
            raise ValueError("Rating must be between 1 and 5.")
        try:
            execute_db(
                "INSERT INTO reviews (reviewer_id, reviewee_id, request_id, rating, feedback) VALUES (?, ?, ?, ?, ?)",
                (g.user["id"], reviewee_id, request_id, rating, feedback),
            )
            flash("Review submitted.", "success")
        except sqlite3.IntegrityError:
            flash("You already reviewed this exchange.", "warning")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("requests_view"))

