from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from core import MAX_EMAIL_LENGTH, MAX_NAME_LENGTH, MAX_PASSWORD_LENGTH, app, execute_db, query_db, validate_text


@app.route("/register", methods=["GET", "POST"])
def register():
    # Create a new account after validating the submitted form fields.
    if request.method == "POST":
        try:
            name = validate_text(request.form.get("name"), "Name", MAX_NAME_LENGTH, min_length=2, required=True)
            email = validate_text(request.form.get("email"), "Email", MAX_EMAIL_LENGTH, required=True).lower()
            password = validate_text(request.form.get("password"), "Password", MAX_PASSWORD_LENGTH, min_length=8, required=True)
            if "@" not in email or "." not in email.split("@")[-1]:
                raise ValueError("Enter a valid email address.")
            if query_db("SELECT id FROM users WHERE email = ?", (email,), one=True):
                raise ValueError("An account with that email already exists.")
            cursor = execute_db(
                "INSERT INTO users (name, email, password_hash, profile_setup_completed) VALUES (?, ?, ?, 0)",
                (name, email, generate_password_hash(password)),
            )
            session.clear()
            session["user_id"] = cursor.lastrowid
            flash("Account created. Let’s finish setting up your profile.", "success")
            return redirect(url_for("profile_setup"))
        except ValueError as exc:
            flash(str(exc), "danger")
    return render_template("register.html", active_page="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    # Authenticate the user and persist their id in the Flask session.
    if request.method == "POST":
        email = validate_text(request.form.get("email"), "Email", MAX_EMAIL_LENGTH, required=True).lower()
        password = validate_text(request.form.get("password"), "Password", MAX_PASSWORD_LENGTH, required=True)
        user = query_db("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "danger")
        else:
            session.clear()
            session["user_id"] = user["id"]
            flash("Logged in successfully.", "success")
            if not user["profile_setup_completed"]:
                return redirect(url_for("profile_setup"))
            return redirect(url_for("dashboard"))
    return render_template("login.html", active_page="login")


@app.route("/logout")
def logout():
    # Remove the authenticated session.
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))
