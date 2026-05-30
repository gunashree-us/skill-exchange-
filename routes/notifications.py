from flask import flash, g, redirect, render_template, request, url_for

from core import app, login_required
from services.notifications import mark_all_notifications_read, mark_notification_read, recent_notifications


@app.route("/notifications")
@login_required
def notifications():
    # Show a simple inbox of stored alerts so members can revisit important updates.
    return render_template(
        "notifications.html",
        notifications=recent_notifications(g.user["id"]),
        active_page="notifications",
    )


@app.route("/notifications/read-all", methods=["POST"])
@login_required
def notifications_read_all():
    # Clear the unread badge once the user has reviewed their latest alerts.
    mark_all_notifications_read(g.user["id"])
    flash("All notifications marked as read.", "success")
    return redirect(url_for("notifications"))


@app.route("/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def notification_read(notification_id):
    # Allow a single alert to be acknowledged without clearing the whole inbox.
    next_url = request.form.get("next_url") or url_for("notifications")
    mark_notification_read(g.user["id"], notification_id)
    return redirect(next_url)
