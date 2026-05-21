from flask import flash, g, redirect, render_template, request, url_for

from core import ADMIN_USERS_PER_PAGE, admin_required, app, execute_db, query_db


@app.route("/admin")
@admin_required
def admin():
    # Admin overview with user counts and recent platform activity.
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    total_users = query_db("SELECT COUNT(*) AS count FROM users", one=True)["count"]
    total_pages = max((total_users + ADMIN_USERS_PER_PAGE - 1) // ADMIN_USERS_PER_PAGE, 1)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * ADMIN_USERS_PER_PAGE
    users = query_db(
        """
        SELECT u.id, u.name, u.email, u.is_admin, u.created_at,
               COUNT(DISTINCT us.id) AS total_skills,
               COUNT(DISTINCT er.id) AS total_requests
        FROM users u
        LEFT JOIN user_skills us ON us.user_id = u.id
        LEFT JOIN exchange_requests er ON er.sender_id = u.id OR er.receiver_id = u.id
        GROUP BY u.id
        ORDER BY u.created_at DESC
        LIMIT ? OFFSET ?
        """
        ,
        (ADMIN_USERS_PER_PAGE, offset),
    )
    recent_requests = query_db(
        """
        SELECT er.id, er.status, er.created_at, s.name AS sender_name, r.name AS receiver_name
        FROM exchange_requests er
        JOIN users s ON s.id = er.sender_id
        JOIN users r ON r.id = er.receiver_id
        ORDER BY er.created_at DESC
        LIMIT 10
        """
    )
    return render_template(
        "admin.html",
        users=users,
        recent_requests=recent_requests,
        page=page,
        total_pages=total_pages,
        total_users=total_users,
        admin_users_per_page=ADMIN_USERS_PER_PAGE,
    )


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def toggle_admin(user_id):
    # Minimal admin role toggle; more moderation tooling still needs to be built.
    try:
        page = max(int(request.form.get("page", "1")), 1)
    except ValueError:
        page = 1
    if user_id == g.user["id"]:
        flash("You cannot change your own admin status.", "warning")
        return redirect(url_for("admin", page=page))
    user = query_db("SELECT is_admin FROM users WHERE id = ?", (user_id,), one=True)
    if user:
        execute_db("UPDATE users SET is_admin = ? WHERE id = ?", (0 if user["is_admin"] else 1, user_id))
        flash("User permissions updated.", "success")
    return redirect(url_for("admin", page=page))
