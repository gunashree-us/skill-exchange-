from collections import defaultdict

from flask import url_for

from core import LEVEL_ORDER, query_db, user_initials


def normalize_availability(value):
    # Normalize free-text availability/location strings into comparable tokens.
    cleaned = (value or "").lower().replace(",", " ").replace("/", " ")
    return {token for token in cleaned.split() if len(token) >= 3}


def normalize_skill_name(value):
    # Treat case and repeated whitespace as cosmetic so similar skills can still match.
    return " ".join((value or "").strip().lower().split())


def room_name_for_users(user_a, user_b):
    # Stable room name so both participants join the same Socket.IO room.
    low, high = sorted((int(user_a), int(user_b)))
    return f"chat:{low}:{high}"


def load_skill_maps(user_ids):
    # Fetch skill rows for one or many users and group by teach/learn.
    if isinstance(user_ids, int):
        user_ids = [user_ids]
    user_ids = [int(user_id) for user_id in user_ids]
    if not user_ids:
        return {}

    placeholders = ", ".join("?" for _ in user_ids)
    rows = query_db(
        f"""
        SELECT us.user_id, us.skill_id, us.skill_type, us.level, s.name, s.category
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        WHERE us.user_id IN ({placeholders})
        ORDER BY s.name
        """,
        tuple(user_ids),
    )
    grouped = {
        user_id: {"teach": {}, "learn": {}}
        for user_id in user_ids
    }
    for row in rows:
        grouped[row["user_id"]][row["skill_type"]][row["skill_id"]] = {
            "id": row["skill_id"],
            "name": row["name"],
            "category": row["category"],
            "level": row["level"],
        }
    return grouped


def level_gap(first_level, second_level):
    # Smaller skill-level gaps are better teaching/learning fits.
    return abs(LEVEL_ORDER.get(first_level, 1) - LEVEL_ORDER.get(second_level, 1))


def compute_match_score(current_user, candidate, my_skills, their_skills):
    # Rank partners using real overlap, level fit, availability fit, and rating.
    my_learn_by_name = defaultdict(list)
    my_teach_by_name = defaultdict(list)
    their_teach_by_name = defaultdict(list)
    their_learn_by_name = defaultdict(list)

    for item in my_skills["learn"].values():
        my_learn_by_name[normalize_skill_name(item["name"])].append(item)
    for item in my_skills["teach"].values():
        my_teach_by_name[normalize_skill_name(item["name"])].append(item)
    for item in their_skills["teach"].values():
        their_teach_by_name[normalize_skill_name(item["name"])].append(item)
    for item in their_skills["learn"].values():
        their_learn_by_name[normalize_skill_name(item["name"])].append(item)

    offer_names = sorted(set(my_learn_by_name) & set(their_teach_by_name))
    want_names = sorted(set(my_teach_by_name) & set(their_learn_by_name))
    if not offer_names or not want_names:
        return None

    matched_offer_skills = []
    matched_want_skills = []
    level_diffs = []

    for skill_name in offer_names:
        my_skill = sorted(my_learn_by_name[skill_name], key=lambda item: LEVEL_ORDER.get(item["level"], 1))[0]
        their_skill = sorted(
            their_teach_by_name[skill_name],
            key=lambda item: level_gap(my_skill["level"], item["level"]),
        )[0]
        matched_offer_skills.append(their_skill)
        level_diffs.append(level_gap(my_skill["level"], their_skill["level"]))

    for skill_name in want_names:
        my_skill = sorted(my_teach_by_name[skill_name], key=lambda item: LEVEL_ORDER.get(item["level"], 1))[0]
        their_skill = sorted(
            their_learn_by_name[skill_name],
            key=lambda item: level_gap(my_skill["level"], item["level"]),
        )[0]
        matched_want_skills.append(their_skill)
        level_diffs.append(level_gap(my_skill["level"], their_skill["level"]))

    average_gap = sum(level_diffs) / len(level_diffs)
    availability_overlap = normalize_availability(current_user.get("availability")) & normalize_availability(candidate.get("availability"))
    availability_points = 8 if availability_overlap else (4 if not current_user.get("availability") or not candidate.get("availability") else 0)
    rating_points = min(float(candidate.get("avg_rating") or 0), 5) * 4
    overlap_points = min(len(offer_names), 3) * 16 + min(len(want_names), 3) * 16
    level_points = max(6, 18 - round(average_gap * 6))
    profile_points = 6 if candidate.get("bio") else 2
    score = min(99, round(overlap_points + level_points + availability_points + rating_points + profile_points))
    request_teach_skill = my_teach_by_name[want_names[0]][0]
    request_learn_skill = my_learn_by_name[offer_names[0]][0]
    reason_labels = [
        f"{len(offer_names)} skill{'s' if len(offer_names) != 1 else ''} they can teach you",
        f"{len(want_names)} skill{'s' if len(want_names) != 1 else ''} you can teach them",
    ]
    if availability_overlap:
        reason_labels.append("Availability overlap")
    if average_gap <= 0.5:
        reason_labels.append("Strong level fit")
    elif average_gap <= 1:
        reason_labels.append("Good level fit")
    if float(candidate.get("avg_rating") or 0) >= 4:
        reason_labels.append("Highly rated")

    return {
        "score": score,
        "offer_matches": matched_offer_skills,
        "want_matches": matched_want_skills,
        "request_teach_skill_id": request_teach_skill["id"],
        "request_teach_skill_name": request_teach_skill["name"],
        "request_learn_skill_id": request_learn_skill["id"],
        "request_learn_skill_name": request_learn_skill["name"],
        "level_fit": max(0, 100 - int(average_gap * 33)),
        "availability_fit": bool(availability_overlap),
        "mutual_exchange_count": len(offer_names) + len(want_names),
        "reason_labels": reason_labels[:4],
    }


def find_first_exchange_pair(my_skills, their_skills):
    # Find the first valid barter-ready skill pair between two users if one exists.
    my_learn_by_name = defaultdict(list)
    my_teach_by_name = defaultdict(list)
    their_teach_by_name = defaultdict(list)
    their_learn_by_name = defaultdict(list)

    for item in my_skills["learn"].values():
        my_learn_by_name[normalize_skill_name(item["name"])].append(item)
    for item in my_skills["teach"].values():
        my_teach_by_name[normalize_skill_name(item["name"])].append(item)
    for item in their_skills["teach"].values():
        their_teach_by_name[normalize_skill_name(item["name"])].append(item)
    for item in their_skills["learn"].values():
        their_learn_by_name[normalize_skill_name(item["name"])].append(item)

    offer_names = sorted(set(my_learn_by_name) & set(their_teach_by_name))
    want_names = sorted(set(my_teach_by_name) & set(their_learn_by_name))
    if not offer_names or not want_names:
        return None

    request_teach_skill = my_teach_by_name[want_names[0]][0]
    request_learn_skill = my_learn_by_name[offer_names[0]][0]
    return {
        "request_teach_skill_id": request_teach_skill["id"],
        "request_teach_skill_name": request_teach_skill["name"],
        "request_learn_skill_id": request_learn_skill["id"],
        "request_learn_skill_name": request_learn_skill["name"],
    }


def find_intro_request_pair(my_skills, their_skills):
    # Allow discovery cards to become actionable even before the other user explicitly wants your skill.
    strict_pair = find_first_exchange_pair(my_skills, their_skills)
    if strict_pair:
        return {**strict_pair, "request_mode": "mutual"}
    if not my_skills["teach"] or not their_skills["teach"]:
        return None
    my_teach_skill = sorted(my_skills["teach"].values(), key=lambda item: item["name"].lower())[0]
    their_teach_skill = sorted(their_skills["teach"].values(), key=lambda item: item["name"].lower())[0]
    return {
        "request_teach_skill_id": my_teach_skill["id"],
        "request_teach_skill_name": my_teach_skill["name"],
        "request_learn_skill_id": their_teach_skill["id"],
        "request_learn_skill_name": their_teach_skill["name"],
        "request_mode": "intro",
    }


def recommendation_candidate_rows(current_user_id):
    # Shared candidate query for layered recommendations.
    return query_db(
        """
        SELECT
            other.id,
            other.name,
            other.bio,
            other.availability,
            COALESCE(r.avg_rating, 0) AS avg_rating,
            COALESCE(r.review_total, 0) AS review_total
        FROM users other
        LEFT JOIN (
            SELECT reviewee_id, ROUND(AVG(rating), 1) AS avg_rating, COUNT(*) AS review_total
            FROM reviews
            GROUP BY reviewee_id
        ) r ON r.reviewee_id = other.id
        WHERE other.id != ?
        ORDER BY other.name
        """,
        (current_user_id,),
    )


def profile_completeness_points(candidate):
    # Reward members who give enough context to act on a recommendation.
    total = 0
    if candidate.get("bio"):
        total += 3
    if candidate.get("availability"):
        total += 3
    return total


def apply_recommendation_filters(entry, *, term="", category="", level="", min_rating=0, availability=""):
    # Keep filtering logic consistent across all recommendation layers.
    if category and category not in set(entry.get("categories") or []):
        return False
    if level and level not in set(entry.get("levels") or []):
        return False
    if min_rating and float(entry.get("avg_rating") or 0) < min_rating:
        return False
    if availability and availability not in (entry.get("availability") or "").lower():
        return False
    searchable = " ".join(
        [
            entry.get("name") or "",
            entry.get("bio") or "",
            entry.get("availability") or "",
            *entry.get("search_tokens", []),
            *entry.get("reason_labels", []),
        ]
    ).lower()
    return not term or term in searchable


def build_teacher_suggestions(current_user, candidate_rows, candidate_skill_maps, *, term="", category="", level="", min_rating=0, availability=""):
    # Suggest teachers even without a strict two-way barter yet.
    current_user = dict(current_user)
    my_skills_map = load_skill_maps(current_user["id"]).get(current_user["id"], {"teach": {}, "learn": {}})
    desired_by_name = defaultdict(list)
    for item in my_skills_map["learn"].values():
        desired_by_name[normalize_skill_name(item["name"])].append(item)

    if not desired_by_name:
        return []

    term = term.strip().lower()
    availability = availability.strip().lower()
    ranked = []

    for row in candidate_rows:
        candidate = dict(row)
        their_skills = candidate_skill_maps.get(candidate["id"], {"teach": {}, "learn": {}})
        request_pair = find_intro_request_pair(my_skills_map, their_skills)
        their_teach_by_name = defaultdict(list)
        for item in their_skills["teach"].values():
            their_teach_by_name[normalize_skill_name(item["name"])].append(item)
        overlap_names = sorted(set(desired_by_name) & set(their_teach_by_name))
        if not overlap_names:
            continue

        matched_teach = []
        level_diffs = []
        for skill_name in overlap_names:
            my_skill = sorted(desired_by_name[skill_name], key=lambda item: LEVEL_ORDER.get(item["level"], 1))[0]
            teacher_skill = sorted(
                their_teach_by_name[skill_name],
                key=lambda item: level_gap(my_skill["level"], item["level"]),
            )[0]
            matched_teach.append(teacher_skill)
            level_diffs.append(level_gap(my_skill["level"], teacher_skill["level"]))

        avg_gap = sum(level_diffs) / len(level_diffs)
        availability_overlap = normalize_availability(current_user.get("availability")) & normalize_availability(candidate.get("availability"))
        score = min(
            98,
            round(
                min(len(overlap_names), 4) * 18
                + max(8, 20 - round(avg_gap * 6))
                + (8 if availability_overlap else 3)
                + min(float(candidate.get("avg_rating") or 0), 5) * 4
                + profile_completeness_points(candidate)
            ),
        )
        reason_labels = [
            f"Teaches {len(overlap_names)} of your learning goal{'s' if len(overlap_names) != 1 else ''}",
        ]
        if availability_overlap:
            reason_labels.append("Availability overlap")
        if avg_gap <= 0.5:
            reason_labels.append("Strong level fit")
        if float(candidate.get("avg_rating") or 0) >= 4:
            reason_labels.append("Highly rated")

        entry = {
            "type": "teacher",
            "user_id": candidate["id"],
            "name": candidate["name"],
            "bio": candidate.get("bio") or "This member has not written a bio yet.",
            "availability": candidate.get("availability") or "Flexible schedule",
            "avg_rating": candidate["avg_rating"],
            "review_total": candidate["review_total"],
            "initials": user_initials(candidate["name"]),
            "offers": [item["name"] for item in matched_teach[:4]],
            "wants": [item["name"] for item in their_skills["learn"].values()][:3],
            "match_score": score,
            "reason_labels": reason_labels[:4],
            "categories": sorted({item["category"] for item in matched_teach}),
            "levels": sorted({item["level"] for item in matched_teach}),
            "search_tokens": [item["name"] for item in matched_teach[:4]],
            "shared_count": len(overlap_names),
            "can_request_exchange": bool(request_pair),
        }
        if request_pair:
            entry.update(request_pair)
        if apply_recommendation_filters(entry, term=term, category=category, level=level, min_rating=min_rating, availability=availability):
            ranked.append(entry)

    ranked.sort(key=lambda item: (-item["match_score"], -item["shared_count"], -(item["avg_rating"] or 0), item["name"].lower()))
    return ranked


def build_learner_suggestions(current_user, candidate_rows, candidate_skill_maps, *, term="", category="", level="", min_rating=0, availability=""):
    # Surface people who actively want what the current member can teach.
    current_user = dict(current_user)
    my_skills_map = load_skill_maps(current_user["id"]).get(current_user["id"], {"teach": {}, "learn": {}})
    teach_by_name = defaultdict(list)
    for item in my_skills_map["teach"].values():
        teach_by_name[normalize_skill_name(item["name"])].append(item)
    if not teach_by_name:
        return []

    term = term.strip().lower()
    availability = availability.strip().lower()
    ranked = []

    for row in candidate_rows:
        candidate = dict(row)
        their_skills = candidate_skill_maps.get(candidate["id"], {"teach": {}, "learn": {}})
        request_pair = find_intro_request_pair(my_skills_map, their_skills)
        their_learn_by_name = defaultdict(list)
        for item in their_skills["learn"].values():
            their_learn_by_name[normalize_skill_name(item["name"])].append(item)
        overlap_names = sorted(set(teach_by_name) & set(their_learn_by_name))
        if not overlap_names:
            continue

        matched_learn = []
        level_diffs = []
        for skill_name in overlap_names:
            my_skill = sorted(teach_by_name[skill_name], key=lambda item: LEVEL_ORDER.get(item["level"], 1))[0]
            learner_skill = sorted(
                their_learn_by_name[skill_name],
                key=lambda item: level_gap(my_skill["level"], item["level"]),
            )[0]
            matched_learn.append(learner_skill)
            level_diffs.append(level_gap(my_skill["level"], learner_skill["level"]))

        avg_gap = sum(level_diffs) / len(level_diffs)
        availability_overlap = normalize_availability(current_user.get("availability")) & normalize_availability(candidate.get("availability"))
        score = min(
            98,
            round(
                min(len(overlap_names), 4) * 18
                + max(8, 20 - round(avg_gap * 6))
                + (8 if availability_overlap else 3)
                + min(float(candidate.get("avg_rating") or 0), 5) * 3
                + profile_completeness_points(candidate)
            ),
        )
        reason_labels = [
            f"Wants {len(overlap_names)} of your teaching skill{'s' if len(overlap_names) != 1 else ''}",
        ]
        if availability_overlap:
            reason_labels.append("Availability overlap")
        if avg_gap <= 0.5:
            reason_labels.append("Strong level fit")
        if float(candidate.get("avg_rating") or 0) >= 4:
            reason_labels.append("Highly rated")

        entry = {
            "type": "learner",
            "user_id": candidate["id"],
            "name": candidate["name"],
            "bio": candidate.get("bio") or "This member has not written a bio yet.",
            "availability": candidate.get("availability") or "Flexible schedule",
            "avg_rating": candidate["avg_rating"],
            "review_total": candidate["review_total"],
            "initials": user_initials(candidate["name"]),
            "offers": [item["name"] for item in their_skills["teach"].values()][:3],
            "wants": [item["name"] for item in matched_learn[:4]],
            "match_score": score,
            "reason_labels": reason_labels[:4],
            "categories": sorted({item["category"] for item in matched_learn}),
            "levels": sorted({item["level"] for item in matched_learn}),
            "search_tokens": [item["name"] for item in matched_learn[:4]],
            "shared_count": len(overlap_names),
            "can_request_exchange": bool(request_pair),
        }
        if request_pair:
            entry.update(request_pair)
        if apply_recommendation_filters(entry, term=term, category=category, level=level, min_rating=min_rating, availability=availability):
            ranked.append(entry)

    ranked.sort(key=lambda item: (-item["match_score"], -item["shared_count"], -(item["avg_rating"] or 0), item["name"].lower()))
    return ranked


def build_trending_interest_recommendations(current_user, candidate_rows, candidate_skill_maps, *, term="", category="", level="", min_rating=0, availability=""):
    # Show active members in the same categories even when a strict barter path is not ready yet.
    current_user = dict(current_user)
    my_skills_map = load_skill_maps(current_user["id"]).get(current_user["id"], {"teach": {}, "learn": {}})
    my_categories = {
        item["category"]
        for skill_group in my_skills_map.values()
        for item in skill_group.values()
        if item.get("category")
    }
    if not my_categories:
        return []

    term = term.strip().lower()
    availability = availability.strip().lower()
    ranked = []

    for row in candidate_rows:
        candidate = dict(row)
        their_skills = candidate_skill_maps.get(candidate["id"], {"teach": {}, "learn": {}})
        request_pair = find_intro_request_pair(my_skills_map, their_skills)
        all_their_skills = list(their_skills["teach"].values()) + list(their_skills["learn"].values())
        shared_categories = sorted({item["category"] for item in all_their_skills if item.get("category") in my_categories})
        if not shared_categories:
            continue
        if not their_skills["teach"] and not their_skills["learn"]:
            continue
        category_skills = [item for item in all_their_skills if item.get("category") in shared_categories]
        score = min(
            96,
            round(
                len(shared_categories) * 20
                + min(len(category_skills), 5) * 8
                + min(float(candidate.get("avg_rating") or 0), 5) * 4
                + profile_completeness_points(candidate)
            ),
        )
        reason_labels = [
            f"Active in {len(shared_categories)} shared categor{'y' if len(shared_categories) == 1 else 'ies'}",
            f"{min(len(category_skills), 5)} relevant skill{'s' if len(category_skills) != 1 else ''}",
        ]
        if float(candidate.get("avg_rating") or 0) >= 4:
            reason_labels.append("Highly rated")

        entry = {
            "type": "trending",
            "user_id": candidate["id"],
            "name": candidate["name"],
            "bio": candidate.get("bio") or "This member has not written a bio yet.",
            "availability": candidate.get("availability") or "Flexible schedule",
            "avg_rating": candidate["avg_rating"],
            "review_total": candidate["review_total"],
            "initials": user_initials(candidate["name"]),
            "offers": [item["name"] for item in their_skills["teach"].values()][:4],
            "wants": [item["name"] for item in their_skills["learn"].values()][:4],
            "match_score": score,
            "reason_labels": reason_labels[:4],
            "categories": shared_categories,
            "levels": sorted({item["level"] for item in category_skills}),
            "search_tokens": [item["name"] for item in category_skills[:6]] + shared_categories,
            "shared_count": len(shared_categories),
            "can_request_exchange": bool(request_pair),
        }
        if request_pair:
            entry.update(request_pair)
        if apply_recommendation_filters(entry, term=term, category=category, level=level, min_rating=min_rating, availability=availability):
            ranked.append(entry)

    ranked.sort(key=lambda item: (-item["match_score"], -item["shared_count"], -(item["avg_rating"] or 0), item["name"].lower()))
    return ranked


def build_ranked_matches(current_user, *, term="", category="", level="", min_rating=0, availability=""):
    # Build one ranked match list that powers both the dashboard and layered match browser.
    my_skills_map = load_skill_maps(current_user["id"]).get(current_user["id"], {"teach": {}, "learn": {}})
    if not my_skills_map["teach"] or not my_skills_map["learn"]:
        return [], []

    candidate_rows = recommendation_candidate_rows(current_user["id"])
    candidate_ids = [row["id"] for row in candidate_rows]
    candidate_skill_maps = load_skill_maps(candidate_ids)

    term = term.strip().lower()
    category = category.strip()
    level = level.strip()
    availability = availability.strip().lower()
    ranked = []
    categories = set()

    for row in candidate_rows:
        candidate = dict(row)
        their_skills = candidate_skill_maps.get(candidate["id"], {"teach": {}, "learn": {}})
        score = compute_match_score(dict(current_user), candidate, my_skills_map, their_skills)
        if score is None:
            continue

        offers = score["offer_matches"]
        wants = score["want_matches"]
        offer_names = [item["name"] for item in offers[:3]]
        want_names = [item["name"] for item in wants[:3]]
        offer_categories = {item["category"] for item in offers}
        categories.update(offer_categories)

        if category and category not in offer_categories:
            continue
        if level and not any(item["level"] == level for item in offers):
            continue
        if min_rating and float(candidate["avg_rating"] or 0) < min_rating:
            continue
        if availability and availability not in (candidate.get("availability") or "").lower():
            continue
        searchable = " ".join([
            candidate["name"],
            candidate.get("bio") or "",
            candidate.get("availability") or "",
            *offer_names,
            *want_names,
        ]).lower()
        if term and term not in searchable:
            continue

        ranked.append(
            {
                "type": "mutual",
                "user_id": candidate["id"],
                "name": candidate["name"],
                "bio": candidate.get("bio") or "This member has not written a bio yet.",
                "availability": candidate.get("availability") or "Flexible schedule",
                "location": candidate.get("availability") or "Flexible schedule",
                "avg_rating": candidate["avg_rating"],
                "review_total": candidate["review_total"],
                "initials": user_initials(candidate["name"]),
                "offers": offer_names,
                "wants": want_names,
                "offer_name": offer_names[0],
                "want_name": want_names[0],
                "match_score": score["score"],
                "level_fit": score["level_fit"],
                "availability_fit": score["availability_fit"],
                "mutual_exchange_count": score["mutual_exchange_count"],
                "reason_labels": score["reason_labels"],
                "request_teach_skill_id": score["request_teach_skill_id"],
                "request_teach_skill_name": score["request_teach_skill_name"],
                "request_learn_skill_id": score["request_learn_skill_id"],
                "request_learn_skill_name": score["request_learn_skill_name"],
            }
        )

    ranked.sort(key=lambda item: (-item["match_score"], -(item["avg_rating"] or 0), item["name"].lower()))
    return ranked, sorted(categories)

def layered_recommendations_for_user(current_user, *, term="", category="", level="", min_rating=0, availability="", limit=None):
    # Assemble an Instagram-like feed with mutuals, teachers, learners, and trend-based discovery.
    current_user = dict(current_user)
    candidate_rows = recommendation_candidate_rows(current_user["id"])
    candidate_skill_maps = load_skill_maps([row["id"] for row in candidate_rows])
    mutual_matches, categories = build_ranked_matches(
        current_user,
        term=term,
        category=category,
        level=level,
        min_rating=min_rating,
        availability=availability,
    )
    suggested_teachers = build_teacher_suggestions(
        current_user,
        candidate_rows,
        candidate_skill_maps,
        term=term,
        category=category,
        level=level,
        min_rating=min_rating,
        availability=availability,
    )
    suggested_learners = build_learner_suggestions(
        current_user,
        candidate_rows,
        candidate_skill_maps,
        term=term,
        category=category,
        level=level,
        min_rating=min_rating,
        availability=availability,
    )
    trending = build_trending_interest_recommendations(
        current_user,
        candidate_rows,
        candidate_skill_maps,
        term=term,
        category=category,
        level=level,
        min_rating=min_rating,
        availability=availability,
    )
    seen_user_ids = set()

    def unique_people(entries):
        unique_entries = []
        for entry in entries:
            user_id = entry.get("user_id")
            if user_id in seen_user_ids:
                continue
            seen_user_ids.add(user_id)
            unique_entries.append(entry)
        return unique_entries

    mutual_matches = unique_people(mutual_matches)
    suggested_teachers = unique_people(suggested_teachers)
    suggested_learners = unique_people(suggested_learners)
    trending = unique_people(trending)
    if limit is not None:
        mutual_matches = mutual_matches[:limit]
        suggested_teachers = suggested_teachers[:limit]
        suggested_learners = suggested_learners[:limit]
        trending = trending[:limit]
    categories = sorted(set(categories) | {item for group in (suggested_teachers, suggested_learners, trending) for entry in group for item in entry.get("categories", [])})
    return {
        "mutual_matches": mutual_matches,
        "suggested_teachers": suggested_teachers,
        "suggested_learners": suggested_learners,
        "trending": trending,
        "categories": categories,
    }


def recommended_teachers_for_user(user_id, *, limit=3):
    # Suggest teachers even when skills were entered with different capitalization or category variants.
    my_skills = load_skill_maps(user_id).get(user_id, {"teach": {}, "learn": {}})
    desired_names = {normalize_skill_name(item["name"]) for item in my_skills["learn"].values()}
    if not desired_names:
        return []

    rows = query_db(
        """
        SELECT other.id AS teacher_id, other.name AS teacher, s.name AS skill_name
        FROM user_skills us
        JOIN skills s ON s.id = us.skill_id
        JOIN users other ON other.id = us.user_id
        WHERE us.skill_type = 'teach' AND us.user_id != ?
        ORDER BY other.name, s.name
        """,
        (user_id,),
    )
    recommended = []
    seen = set()
    for row in rows:
        normalized = normalize_skill_name(row["skill_name"])
        if normalized not in desired_names:
            continue
        key = (row["teacher_id"], normalized)
        if key in seen:
            continue
        seen.add(key)
        recommended.append({"name": row["skill_name"], "teacher": row["teacher"]})
        if len(recommended) >= limit:
            break
    return recommended


def accepted_exchange_counts_by_skill(user_id):
    # Count real accepted exchanges for each skill the member is actively teaching.
    counts = {}
    rows = query_db(
        """
        SELECT teach_skill_id AS skill_id, COUNT(*) AS total
        FROM exchange_requests
        WHERE sender_id = ? AND status = 'Accepted'
        GROUP BY teach_skill_id
        UNION ALL
        SELECT learn_skill_id AS skill_id, COUNT(*) AS total
        FROM exchange_requests
        WHERE receiver_id = ? AND status = 'Accepted'
        GROUP BY learn_skill_id
        """,
        (user_id, user_id),
    )
    for row in rows:
        counts[row["skill_id"]] = counts.get(row["skill_id"], 0) + row["total"]
    return counts


def onboarding_steps_for_user(
    user_id,
    teach_count,
    learn_count,
    total_requests,
    accepted_count,
    bio,
    availability,
    github_url="",
    linkedin_url="",
    certifications="",
):
    # Simple first-run checklist so new members know how to get to their first exchange.
    steps = [
        {"done": teach_count > 0, "title": "Add a skill you can teach", "href": url_for("skills"), "cta": "Add teaching skill"},
        {"done": learn_count > 0, "title": "Add a skill you want to learn", "href": url_for("skills"), "cta": "Add learning goal"},
        {
            "done": bool((bio or "").strip()) and bool((availability or "").strip()) and bool((github_url or "").strip() or (linkedin_url or "").strip() or (certifications or "").strip()),
            "title": "Complete your profile",
            "href": url_for("profile_setup"),
            "cta": "Complete profile",
        },
        {"done": total_requests > 0, "title": "Send your first exchange request", "href": url_for("matches"), "cta": "Find matches"},
        {"done": accepted_count > 0, "title": "Get your first accepted exchange", "href": url_for("requests_view"), "cta": "Review requests"},
    ]
    return steps


