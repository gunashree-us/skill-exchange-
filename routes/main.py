from flask import g, redirect, render_template, request, url_for

from core import DEFAULT_TESTIMONIALS, REQUEST_DURATIONS, app, get_webrtc_ice_servers, query_db
from services.matching import (
    accepted_exchange_counts_by_skill,
    layered_recommendations_for_user,
    onboarding_steps_for_user,
    recommended_teachers_for_user,
    normalize_skill_name,
)
from services.notifications import can_chat_with, mark_conversation_read, load_messages, serialize_message, rating_value, accepted_chat_partners
from core import login_required, user_initials


@app.route("/")
def index():
    # Public landing page with real aggregate platform numbers only.
    if g.user is not None:
        return redirect(url_for("dashboard"))

    stats = {
        "users": query_db("SELECT COUNT(*) AS count FROM users", one=True)["count"],
        "skills": query_db("SELECT COUNT(*) AS count FROM skills", one=True)["count"],
        "requests": query_db("SELECT COUNT(*) AS count FROM exchange_requests", one=True)["count"],
        "platform_rating": query_db("SELECT ROUND(AVG(rating), 1) AS avg_rating FROM reviews", one=True)["avg_rating"],
    }
    featured_skills = query_db(
        """
        SELECT s.name, s.category, COUNT(us.id) AS total
        FROM skills s
        LEFT JOIN user_skills us ON us.skill_id = s.id
        GROUP BY s.id
        ORDER BY total DESC, s.name ASC
        LIMIT 6
        """
    )
    categories = query_db(
        """
        SELECT category, COUNT(*) AS total
        FROM skills
        GROUP BY category
        ORDER BY total DESC, category ASC
        LIMIT 8
        """
    )
    return render_template(
        "index.html",
        stats=stats,
        featured_skills=featured_skills,
        categories=categories,
        testimonials=DEFAULT_TESTIMONIALS,
        active_page="home",
    )

@app.route("/dashboard")
@login_required
def dashboard():
    # Member dashboard summarizing skills, requests, recommendations, and matches.
    teach_skills = [
        dict(item)
        for item in query_db(
        """
        SELECT us.id, s.id AS skill_id, s.name, s.category, us.level
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ? AND us.skill_type = 'teach'
        ORDER BY s.name
        """,
        (g.user["id"],),
        )
    ]
    learn_skills = [
        dict(item)
        for item in query_db(
        """
        SELECT us.id, s.id AS skill_id, s.name, s.category, us.level
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ? AND us.skill_type = 'learn'
        ORDER BY s.name
        """,
        (g.user["id"],),
        )
    ]
    incoming = query_db(
        """
        SELECT er.id, u.name AS sender_name, ts.name AS teach_skill, ls.name AS learn_skill, er.status
        FROM exchange_requests er
        JOIN users u ON u.id = er.sender_id
        JOIN skills ts ON ts.id = er.teach_skill_id
        JOIN skills ls ON ls.id = er.learn_skill_id
        WHERE er.receiver_id = ?
        ORDER BY er.created_at DESC
        LIMIT 5
        """,
        (g.user["id"],),
    )
    rating = query_db(
        "SELECT ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS total FROM reviews WHERE reviewee_id = ?",
        (g.user["id"],),
        one=True,
    )
    total_requests = query_db(
        "SELECT COUNT(*) AS count FROM exchange_requests WHERE sender_id = ? OR receiver_id = ?",
        (g.user["id"], g.user["id"]),
        one=True,
    )["count"]
    accepted_count = query_db(
        "SELECT COUNT(*) AS count FROM exchange_requests WHERE (sender_id = ? OR receiver_id = ?) AND status = 'Accepted'",
        (g.user["id"], g.user["id"]),
        one=True,
    )["count"]
    stats = {
        "skills_offered": len(teach_skills),
        "active_exchanges": accepted_count,
        "students": query_db(
            """
            SELECT COUNT(DISTINCT CASE WHEN sender_id = ? THEN receiver_id ELSE sender_id END) AS count
            FROM exchange_requests
            WHERE status = 'Accepted' AND (sender_id = ? OR receiver_id = ?)
            """,
            (g.user["id"], g.user["id"], g.user["id"]),
            one=True,
        )["count"],
        "rating": rating["avg_rating"] or 0,
    }
    skill_counts = accepted_exchange_counts_by_skill(g.user["id"])
    for skill in teach_skills:
        skill["students_count"] = skill_counts.get(skill["skill_id"], 0)
    recommendation_feed = layered_recommendations_for_user(g.user, limit=3)
    recommended = recommended_teachers_for_user(g.user["id"], limit=3)
    suggested_partners = recommendation_feed["mutual_matches"]
    onboarding_steps = onboarding_steps_for_user(
        g.user["id"],
        len(teach_skills),
        len(learn_skills),
        total_requests,
        accepted_count,
        g.user["bio"],
        g.user["availability"],
        g.user["github_url"],
        g.user["linkedin_url"],
        g.user["certifications"],
    )
    show_onboarding = not all(step["done"] for step in onboarding_steps)
    my_teach = query_db(
        """
        SELECT s.id, s.name
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ? AND us.skill_type = 'teach'
        ORDER BY s.name
        """,
        (g.user["id"],),
    )
    my_learn = query_db(
        """
        SELECT s.id, s.name
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ? AND us.skill_type = 'learn'
        ORDER BY s.name
        """,
        (g.user["id"],),
    )
    return render_template(
        "dashboard.html",
        teach_skills=teach_skills,
        learn_skills=learn_skills,
        incoming=incoming,
        rating=rating,
        stats=stats,
        recommended=recommended,
        suggested_partners=suggested_partners,
        recommendation_feed=recommendation_feed,
        my_teach=my_teach,
        my_learn=my_learn,
        durations=REQUEST_DURATIONS,
        onboarding_steps=onboarding_steps,
        show_onboarding=show_onboarding,
        active_page="dashboard",
    )


@app.route("/browse")
@login_required
def browse():
    # Broad member browse page with grouped user cards and skill-aware search.
    category = request.args.get("category", "").strip()
    term = request.args.get("q", "").strip()
    people_rows = query_db(
        """
        SELECT
            u.id,
            u.name,
            u.bio,
            u.availability,
            COALESCE(r.avg_rating, 0) AS avg_rating,
            COALESCE(r.review_total, 0) AS review_total,
            s.name AS skill_name,
            s.category,
            us.skill_type,
            us.level
        FROM users u
        LEFT JOIN (
            SELECT reviewee_id, ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS review_total
            FROM reviews
            GROUP BY reviewee_id
        ) r ON r.reviewee_id = u.id
        LEFT JOIN user_skills us ON us.user_id = u.id
        LEFT JOIN skills s ON s.id = us.skill_id
        WHERE u.id != ?
        ORDER BY u.name, us.skill_type, s.name
        """,
        (g.user["id"],),
    )
    grouped_people = {}
    for row in people_rows:
        user_id = row["id"]
        person = grouped_people.setdefault(
            user_id,
            {
                "id": user_id,
                "name": row["name"],
                "bio": row["bio"] or "No bio yet.",
                "availability": row["availability"] or "Flexible",
                "avg_rating": row["avg_rating"],
                "review_total": row["review_total"],
                "teach_skills": [],
                "learn_skills": [],
                "teach_skill_names": set(),
                "learn_skill_names": set(),
                "categories": set(),
            },
        )
        if row["skill_name"]:
            skill_entry = {"name": row["skill_name"], "category": row["category"], "level": row["level"]}
            if row["skill_type"] == "teach":
                normalized_name = normalize_skill_name(row["skill_name"])
                if normalized_name not in person["teach_skill_names"]:
                    person["teach_skill_names"].add(normalized_name)
                    person["teach_skills"].append(skill_entry)
            elif row["skill_type"] == "learn":
                normalized_name = normalize_skill_name(row["skill_name"])
                if normalized_name not in person["learn_skill_names"]:
                    person["learn_skill_names"].add(normalized_name)
                    person["learn_skills"].append(skill_entry)
            if row["category"]:
                person["categories"].add(row["category"])

    people = []
    lowered_term = term.lower()
    for person in grouped_people.values():
        if category and category not in person["categories"]:
            continue
        searchable = " ".join(
            [
                person["name"],
                person["bio"],
                person["availability"],
                *[item["name"] for item in person["teach_skills"]],
                *[item["name"] for item in person["learn_skills"]],
            ]
        ).lower()
        if lowered_term and lowered_term not in searchable:
            continue
        person["initials"] = user_initials(person["name"])
        person["categories"] = sorted(person["categories"])
        person.pop("teach_skill_names", None)
        person.pop("learn_skill_names", None)
        people.append(person)

    people.sort(key=lambda item: (item["name"].lower(), -(item["avg_rating"] or 0)))
    categories = query_db("SELECT DISTINCT category FROM skills ORDER BY category")
    return render_template("browse.html", people=people, categories=categories, selected_category=category, term=term, active_page="match")


@app.route("/matches")
@login_required
def matches():
    # Show layered recommendations so discovery feels more like a modern social feed.
    term = request.args.get("q", "").strip().lower()
    selected_category = request.args.get("category", "").strip()
    selected_level = request.args.get("level", "").strip()
    min_rating = request.args.get("min_rating", type=float) or 0
    availability_filter = request.args.get("availability", "").strip()
    recommendation_feed = layered_recommendations_for_user(
        g.user,
        term=term,
        category=selected_category,
        level=selected_level,
        min_rating=min_rating,
        availability=availability_filter,
    )
    matches = recommendation_feed["mutual_matches"]
    categories = recommendation_feed["categories"]
    my_teach = query_db(
        """
        SELECT s.id, s.name
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ? AND us.skill_type = 'teach'
        ORDER BY s.name
        """,
        (g.user["id"],),
    )
    my_learn = query_db(
        """
        SELECT s.id, s.name
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id = ? AND us.skill_type = 'learn'
        ORDER BY s.name
        """,
        (g.user["id"],),
    )
    return render_template(
        "matches.html",
        matches=matches,
        suggested_teachers=recommendation_feed["suggested_teachers"],
        suggested_learners=recommendation_feed["suggested_learners"],
        trending_people=recommendation_feed["trending"],
        my_teach=my_teach,
        my_learn=my_learn,
        categories=categories,
        selected_category=selected_category,
        selected_level=selected_level,
        min_rating=min_rating,
        availability_filter=availability_filter,
        durations=REQUEST_DURATIONS,
        term=term,
        active_page="match",
    )

