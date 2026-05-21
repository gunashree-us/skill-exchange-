import sqlite3

from flask import flash, g, redirect, render_template, request, url_for

from core import (
    DEFAULT_CATEGORIES,
    MAX_CATEGORY_LENGTH,
    MAX_SKILL_NAME_LENGTH,
    app,
    execute_db,
    login_required,
    query_db,
    update_profile_fields,
    validate_text,
)
from uploads import validate_profile_uploads


@app.route("/skills", methods=["GET", "POST"])
@login_required
def skills():
    # Skills management page plus profile sidebar updates for convenience.
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
            flash("Profile updated.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        return redirect(url_for("skills"))

    user_skills = query_db(
        """
        SELECT us.id, us.skill_type, us.level, s.name, s.category
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ?
        ORDER BY us.skill_type, s.name
        """,
        (g.user["id"],),
    )
    teach_skills = [skill for skill in user_skills if skill["skill_type"] == "teach"]
    learn_skills = [skill for skill in user_skills if skill["skill_type"] == "learn"]
    return render_template(
        "skills.html",
        user_skills=user_skills,
        teach_skills=teach_skills,
        learn_skills=learn_skills,
        categories=DEFAULT_CATEGORIES,
        active_page="skills",
    )


@app.route("/skills/add", methods=["POST"])
@login_required
def add_skill():
    # Create a reusable skill entry if needed, then link it to the current user.
    try:
        name = validate_text(request.form.get("name"), "Skill name", MAX_SKILL_NAME_LENGTH, min_length=2, required=True)
        category = validate_text(request.form.get("category"), "Category", MAX_CATEGORY_LENGTH, min_length=2, required=True)
        name = " ".join(name.split())
        category = " ".join(category.split())
        skill_type = request.form.get("skill_type")
        level = request.form.get("level")
        if skill_type not in {"teach", "learn"}:
            raise ValueError("Skill type must be teach or learn.")
        if level not in {"Beginner", "Intermediate", "Advanced"}:
            raise ValueError("Choose a valid skill level.")
        skill = query_db(
            "SELECT id, name, category FROM skills WHERE lower(name) = lower(?) AND lower(category) = lower(?)",
            (name, category),
            one=True,
        )
        if skill is None:
            execute_db("INSERT OR IGNORE INTO skills (name, category) VALUES (?, ?)", (name, category))
            skill = query_db(
                "SELECT id, name, category FROM skills WHERE lower(name) = lower(?) AND lower(category) = lower(?)",
                (name, category),
                one=True,
            )
        try:
            execute_db(
                "INSERT INTO user_skills (user_id, skill_id, skill_type, level) VALUES (?, ?, ?, ?)",
                (g.user["id"], skill["id"], skill_type, level),
            )
            flash("Skill added.", "success")
        except sqlite3.IntegrityError:
            flash("That skill is already listed in your profile.", "warning")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("skills"))


@app.route("/skills/<int:user_skill_id>/delete", methods=["POST"])
@login_required
def delete_skill(user_skill_id):
    # Remove one user-skill relationship owned by the current user.
    execute_db("DELETE FROM user_skills WHERE id = ? AND user_id = ?", (user_skill_id, g.user["id"]))
    flash("Skill removed.", "success")
    return redirect(url_for("skills"))
