from flask import flash, g, redirect, render_template, request, url_for

from core import (
    app,
    delete_file_if_exists,
    execute_db,
    format_timestamp,
    login_required,
    query_db,
    update_profile_fields,
)
from services.encryption import key_fingerprint
from services.notifications import active_devices_for_users, rating_value
from uploads import is_previewable_image, save_profile_assets, validate_profile_uploads


@app.route("/profile/setup", methods=["GET", "POST"])
@login_required
def profile_setup():
    # First-run setup collects profile trust signals right after signup.
    if request.method == "POST":
        try:
            validate_profile_uploads(
                request.files.get("profile_photo"),
                request.files.getlist("certificate_files"),
            )
            update_profile_fields(
                g.user["id"],
                request.form.get("bio"),
                request.form.get("availability"),
                request.form.get("github_url"),
                request.form.get("linkedin_url"),
                request.form.get("certifications"),
            )
            uploaded_count = save_profile_assets(
                g.user["id"],
                request.files.get("profile_photo"),
                request.files.getlist("certificate_files"),
            )
            execute_db("UPDATE users SET profile_setup_completed = 1 WHERE id = ?", (g.user["id"],))
            flash(
                "Profile setup saved. Your account is ready."
                + (f" Added {uploaded_count} certificate file{'s' if uploaded_count != 1 else ''}." if uploaded_count else ""),
                "success",
            )
            return redirect(url_for("dashboard"))
        except ValueError as exc:
            flash(str(exc), "danger")
    return render_template("profile_setup.html", active_page="profile")


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    # Profile page focuses on member identity, review history, and listed skills.
    if request.method == "POST":
        try:
            validate_profile_uploads(
                request.files.get("profile_photo"),
                request.files.getlist("certificate_files"),
            )
            update_profile_fields(
                g.user["id"],
                request.form.get("bio"),
                request.form.get("availability"),
                request.form.get("github_url"),
                request.form.get("linkedin_url"),
                request.form.get("certifications"),
            )
            uploaded_count = save_profile_assets(
                g.user["id"],
                request.files.get("profile_photo"),
                request.files.getlist("certificate_files"),
            )
            flash(
                "Profile updated."
                + (f" Added {uploaded_count} certificate file{'s' if uploaded_count != 1 else ''}." if uploaded_count else ""),
                "success",
            )
        except ValueError as exc:
            flash(str(exc), "danger")
        return redirect(url_for("profile"))

    teach_skills = query_db(
        """
        SELECT us.id, s.name, s.category, us.level
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ? AND us.skill_type = 'teach'
        ORDER BY s.name
        """,
        (g.user["id"],),
    )
    learn_skills = query_db(
        """
        SELECT us.id, s.name, s.category, us.level
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ? AND us.skill_type = 'learn'
        ORDER BY s.name
        """,
        (g.user["id"],),
    )
    reviews = query_db(
        """
        SELECT u.name, r.rating, r.feedback, strftime('%m', r.created_at) AS month_num, strftime('%Y', r.created_at) AS year
        FROM reviews r
        JOIN users u ON u.id = r.reviewer_id
        WHERE r.reviewee_id = ?
        ORDER BY r.created_at DESC
        LIMIT 3
        """,
        (g.user["id"],),
    )
    rating_avg, rating_total = rating_value(g.user["id"])
    profile_stats = {
        "total_exchanges": query_db(
            "SELECT COUNT(*) AS count FROM exchange_requests WHERE sender_id = ? OR receiver_id = ?",
            (g.user["id"], g.user["id"]),
            one=True,
        )["count"],
        "active_exchanges": query_db(
            "SELECT COUNT(*) AS count FROM exchange_requests WHERE (sender_id = ? OR receiver_id = ?) AND status = 'Accepted'",
            (g.user["id"], g.user["id"]),
            one=True,
        )["count"],
        "rating": rating_avg,
        "rating_total": rating_total,
    }
    learning_progress = [dict(skill) for skill in learn_skills]
    achievements = []
    if profile_stats["rating_total"] >= 1:
        achievements.append({"title": "Reviewed Teacher", "text": "At least one learner has left feedback."})
    if profile_stats["active_exchanges"] >= 1:
        achievements.append({"title": "Active Exchanger", "text": "Currently participating in an accepted exchange."})
    if profile_stats["total_exchanges"] >= 5:
        achievements.append({"title": "Experienced Member", "text": "Completed or requested five or more exchanges."})
    month_names = {
        "01": "January", "02": "February", "03": "March", "04": "April", "05": "May", "06": "June",
        "07": "July", "08": "August", "09": "September", "10": "October", "11": "November", "12": "December",
    }
    reviews = [dict(review) for review in reviews]
    for review in reviews:
        review["date_label"] = f"{month_names.get(review['month_num'], 'Recent')} {review['year']}"
    trusted_devices = active_devices_for_users(g.user["id"]).get(g.user["id"], [])
    for device in trusted_devices:
        device["fingerprint"] = key_fingerprint(device["public_key"])
        device["created_label"] = format_timestamp(device["created_at"], include_date=True) if device.get("created_at") else "Recently"
        device["last_seen_label"] = format_timestamp(device["last_seen_at"], include_date=True) if device.get("last_seen_at") else "Recently"
    certificate_files = [
        dict(row) for row in query_db(
            """
            SELECT id, file_name, file_path, created_at
            FROM profile_certificates
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (g.user["id"],),
        )
    ]
    for certificate in certificate_files:
        certificate["created_label"] = format_timestamp(certificate["created_at"], include_date=True)
        certificate["is_image"] = is_previewable_image(certificate["file_name"])
        certificate["static_path"] = certificate["file_path"].replace("static/", "", 1)
    return render_template(
        "profile.html",
        teach_skills=teach_skills,
        learn_skills=learn_skills,
        learning_progress=learning_progress,
        profile_stats=profile_stats,
        achievements=achievements,
        reviews=reviews,
        trusted_devices=trusted_devices,
        certificate_files=certificate_files,
        active_page="profile",
    )

@app.route("/profile/devices/<int:device_id>/revoke", methods=["POST"])
@login_required
def revoke_device(device_id):
    # Remove a registered browser/device from the trusted encryption device list.
    device = query_db(
        "SELECT id, label FROM user_devices WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (device_id, g.user["id"]),
        one=True,
    )
    if device is None:
        flash("Trusted device not found.", "warning")
        return redirect(url_for("profile"))
    execute_db(
        "UPDATE user_devices SET revoked_at = CURRENT_TIMESTAMP WHERE id = ?",
        (device_id,),
    )
    flash(f"Revoked secure access for {device['label'] or 'that device'}.", "success")
    return redirect(url_for("profile"))


@app.route("/profile/certificates/<int:certificate_id>/delete", methods=["POST"])
@login_required
def delete_certificate(certificate_id):
    # Allow members to remove uploaded certificate files from their profile.
    certificate = query_db(
        "SELECT id, file_path, file_name FROM profile_certificates WHERE id = ? AND user_id = ?",
        (certificate_id, g.user["id"]),
        one=True,
    )
    if certificate is None:
        flash("Certificate file not found.", "warning")
        return redirect(url_for("profile"))
    execute_db("DELETE FROM profile_certificates WHERE id = ?", (certificate_id,))
    delete_file_if_exists(certificate["file_path"])
    flash(f"Removed {certificate['file_name']}.", "success")
    return redirect(url_for("profile"))


@app.route("/profile/photo/delete", methods=["POST"])
@login_required
def delete_profile_photo():
    # Let members remove the current profile image without touching other profile fields.
    user = query_db("SELECT profile_photo_path FROM users WHERE id = ?", (g.user["id"],), one=True)
    if user is None or not user["profile_photo_path"]:
        flash("No profile photo to remove.", "warning")
        return redirect(url_for("profile"))
    execute_db("UPDATE users SET profile_photo_path = '' WHERE id = ?", (g.user["id"],))
    delete_file_if_exists(user["profile_photo_path"])
    flash("Profile photo removed.", "success")
    return redirect(url_for("profile"))
