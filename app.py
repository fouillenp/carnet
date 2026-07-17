import os
import sqlite3
from datetime import datetime
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory, session

DB_PATH = os.environ.get("DB_PATH", "/data/carnet.db")
APP_PIN = os.environ.get("APP_PIN", "1234")
SECRET_KEY = os.environ.get("SECRET_KEY", "changeme-carnet-golf")

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = SECRET_KEY


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _table_columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # migration depuis l'ancien schéma courses(pars) : préserve les données existantes
    # (parcours + parties déjà créés par l'utilisateur) en les convertissant vers course_holes.
    existing_tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    legacy_courses = None
    if "courses" in existing_tables and "pars" in _table_columns(db, "courses"):
        legacy_courses = db.execute("SELECT id, name, holes_count, created_at, pars FROM courses").fetchall()
        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript(
            """
            CREATE TABLE courses_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                holes_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO courses_new (id, name, holes_count, created_at)
                SELECT id, name, holes_count, created_at FROM courses;
            DROP TABLE courses;
            ALTER TABLE courses_new RENAME TO courses;
            """
        )
        db.commit()

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            holes_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS course_holes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            hole_number INTEGER NOT NULL,
            par INTEGER NOT NULL,
            hole_index INTEGER,
            UNIQUE(course_id, hole_number)
        );

        CREATE TABLE IF NOT EXISTS course_tees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(course_id, name)
        );

        CREATE TABLE IF NOT EXISTS course_tee_distances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            tee_id INTEGER NOT NULL REFERENCES course_tees(id) ON DELETE CASCADE,
            hole_number INTEGER NOT NULL,
            distance INTEGER NOT NULL,
            UNIQUE(tee_id, hole_number)
        );

        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            tee_id INTEGER REFERENCES course_tees(id) ON DELETE SET NULL,
            played_on TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hole_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
            hole_number INTEGER NOT NULL,
            par INTEGER NOT NULL,
            score INTEGER NOT NULL,
            putts INTEGER,
            drive_distance INTEGER,
            remaining_distance INTEGER,
            bunker INTEGER NOT NULL DEFAULT 0,
            approach INTEGER NOT NULL DEFAULT 0,
            putt_sur_green INTEGER NOT NULL DEFAULT 0,
            UNIQUE(round_id, hole_number)
        );
        """
    )

    if "rounds" in existing_tables and "tee_id" not in _table_columns(db, "rounds"):
        db.execute("ALTER TABLE rounds ADD COLUMN tee_id INTEGER REFERENCES course_tees(id) ON DELETE SET NULL")

    if legacy_courses:
        for row in legacy_courses:
            pars = [int(p) for p in row["pars"].split(",") if p.strip()]
            for i, par in enumerate(pars, start=1):
                db.execute(
                    "INSERT OR IGNORE INTO course_holes (course_id, hole_number, par, hole_index) VALUES (?, ?, ?, NULL)",
                    (row["id"], i, par),
                )
        db.commit()
        db.execute("PRAGMA foreign_keys = ON")

    db.commit()
    db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return jsonify({"error": "auth_required"}), 401
        return view(*args, **kwargs)

    return wrapped


# ---------- auth ----------

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    if data.get("pin") == APP_PIN:
        session["authed"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"error": "invalid_pin"}), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/session")
def check_session():
    return jsonify({"authed": bool(session.get("authed"))})


# ---------- courses ----------

@app.get("/api/courses")
@login_required
def list_courses():
    db = get_db()
    rows = db.execute(
        """
        SELECT c.*, COALESCE(SUM(h.par), 0) AS total_par,
               (SELECT GROUP_CONCAT(name, ',') FROM course_tees WHERE course_id = c.id) AS tee_names
        FROM courses c
        LEFT JOIN course_holes h ON h.course_id = c.id
        GROUP BY c.id
        ORDER BY c.name
        """
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["tees"] = d["tee_names"].split(",") if d["tee_names"] else []
        del d["tee_names"]
        result.append(d)
    return jsonify(result)


@app.post("/api/courses")
@login_required
def create_course():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    holes = data.get("holes") or []
    tees = data.get("tees") or []
    distances = data.get("distances") or {}
    if not name or not holes:
        return jsonify({"error": "name_and_holes_required"}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO courses (name, holes_count, created_at) VALUES (?, ?, ?)",
        (name, len(holes), datetime.utcnow().isoformat()),
    )
    course_id = cur.lastrowid

    for i, h in enumerate(holes, start=1):
        db.execute(
            "INSERT INTO course_holes (course_id, hole_number, par, hole_index) VALUES (?, ?, ?, ?)",
            (course_id, i, int(h.get("par", 4)), h.get("index")),
        )

    tee_ids = {}
    for order, tee_name in enumerate(tees):
        tcur = db.execute(
            "INSERT INTO course_tees (course_id, name, sort_order) VALUES (?, ?, ?)",
            (course_id, tee_name, order),
        )
        tee_ids[tee_name] = tcur.lastrowid

    for tee_name, dist_list in distances.items():
        tee_id = tee_ids.get(tee_name)
        if not tee_id:
            continue
        for i, d in enumerate(dist_list, start=1):
            if d in (None, ""):
                continue
            db.execute(
                "INSERT INTO course_tee_distances (course_id, tee_id, hole_number, distance) VALUES (?, ?, ?, ?)",
                (course_id, tee_id, i, int(d)),
            )

    db.commit()
    return jsonify({"id": course_id}), 201


@app.get("/api/courses/<int:course_id>")
@login_required
def get_course(course_id):
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if not course:
        return jsonify({"error": "not_found"}), 404
    holes = db.execute(
        "SELECT hole_number, par, hole_index FROM course_holes WHERE course_id = ? ORDER BY hole_number", (course_id,)
    ).fetchall()
    tees = db.execute(
        "SELECT id, name FROM course_tees WHERE course_id = ? ORDER BY sort_order", (course_id,)
    ).fetchall()
    distances = {}
    for t in tees:
        rows = db.execute(
            "SELECT hole_number, distance FROM course_tee_distances WHERE tee_id = ? ORDER BY hole_number", (t["id"],)
        ).fetchall()
        distances[t["id"]] = {row["hole_number"]: row["distance"] for row in rows}
    return jsonify(
        {
            **dict(course),
            "holes": [dict(h) for h in holes],
            "tees": [dict(t) for t in tees],
            "distances": distances,
        }
    )


@app.delete("/api/courses/<int:course_id>")
@login_required
def delete_course(course_id):
    db = get_db()
    db.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- rounds ----------

@app.get("/api/rounds")
@login_required
def list_rounds():
    db = get_db()
    rows = db.execute(
        """
        SELECT r.*, c.name AS course_name, c.holes_count,
               (SELECT COALESCE(SUM(score), 0) FROM hole_entries WHERE round_id = r.id) AS total_score,
               (SELECT COALESCE(SUM(par), 0) FROM hole_entries WHERE round_id = r.id) AS total_par,
               (SELECT COUNT(*) FROM hole_entries WHERE round_id = r.id) AS holes_played
        FROM rounds r
        JOIN courses c ON c.id = r.course_id
        ORDER BY r.played_on DESC, r.id DESC
        """
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/rounds")
@login_required
def create_round():
    data = request.get_json(force=True)
    course_id = data.get("course_id")
    played_on = data.get("played_on") or datetime.utcnow().date().isoformat()
    if not course_id:
        return jsonify({"error": "course_id_required"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO rounds (course_id, tee_id, played_on, notes, created_at) VALUES (?, ?, ?, ?, ?)",
        (course_id, data.get("tee_id"), played_on, data.get("notes"), datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.get("/api/rounds/<int:round_id>")
@login_required
def get_round(round_id):
    db = get_db()
    r = db.execute(
        """
        SELECT r.*, c.name AS course_name, c.holes_count
        FROM rounds r JOIN courses c ON c.id = r.course_id
        WHERE r.id = ?
        """,
        (round_id,),
    ).fetchone()
    if not r:
        return jsonify({"error": "not_found"}), 404

    holes = db.execute(
        "SELECT hole_number, par, hole_index FROM course_holes WHERE course_id = ? ORDER BY hole_number",
        (r["course_id"],),
    ).fetchall()

    tee_distances = {}
    if r["tee_id"]:
        rows = db.execute(
            "SELECT hole_number, distance FROM course_tee_distances WHERE tee_id = ?", (r["tee_id"],)
        ).fetchall()
        tee_distances = {row["hole_number"]: row["distance"] for row in rows}

    entries = db.execute(
        "SELECT * FROM hole_entries WHERE round_id = ? ORDER BY hole_number", (round_id,)
    ).fetchall()

    result = dict(r)
    result["holes"] = [dict(h) for h in holes]
    result["tee_distances"] = tee_distances
    result["entries"] = [dict(e) for e in entries]
    return jsonify(result)


@app.delete("/api/rounds/<int:round_id>")
@login_required
def delete_round(round_id):
    db = get_db()
    db.execute("DELETE FROM rounds WHERE id = ?", (round_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- hole entries ----------

@app.put("/api/rounds/<int:round_id>/holes/<int:hole_number>")
@login_required
def upsert_hole(round_id, hole_number):
    data = request.get_json(force=True)
    db = get_db()
    round_row = db.execute("SELECT id FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row:
        return jsonify({"error": "round_not_found"}), 404

    fields = dict(
        par=int(data.get("par", 4)),
        score=int(data.get("score", 0)),
        putts=data.get("putts"),
        drive_distance=data.get("drive_distance"),
        remaining_distance=data.get("remaining_distance"),
        bunker=1 if data.get("bunker") else 0,
        approach=1 if data.get("approach") else 0,
        putt_sur_green=1 if data.get("putt_sur_green") else 0,
    )

    db.execute(
        """
        INSERT INTO hole_entries
            (round_id, hole_number, par, score, putts, drive_distance, remaining_distance, bunker, approach, putt_sur_green)
        VALUES (:round_id, :hole_number, :par, :score, :putts, :drive_distance, :remaining_distance, :bunker, :approach, :putt_sur_green)
        ON CONFLICT(round_id, hole_number) DO UPDATE SET
            par=excluded.par, score=excluded.score, putts=excluded.putts,
            drive_distance=excluded.drive_distance, remaining_distance=excluded.remaining_distance,
            bunker=excluded.bunker, approach=excluded.approach, putt_sur_green=excluded.putt_sur_green
        """,
        {**fields, "round_id": round_id, "hole_number": hole_number},
    )
    db.commit()
    return jsonify({"ok": True})


# ---------- stats ----------

@app.get("/api/stats")
@login_required
def stats():
    db = get_db()
    rounds = db.execute(
        """
        SELECT r.id, r.played_on, c.name AS course_name,
               SUM(h.score) AS total_score, SUM(h.par) AS total_par, COUNT(h.id) AS holes_played
        FROM rounds r
        JOIN courses c ON c.id = r.course_id
        JOIN hole_entries h ON h.round_id = r.id
        GROUP BY r.id
        ORDER BY r.played_on
        """
    ).fetchall()

    agg = db.execute(
        """
        SELECT
            COUNT(*) AS holes_total,
            AVG(drive_distance) AS avg_drive,
            AVG(remaining_distance) AS avg_remaining,
            AVG(putts) AS avg_putts,
            SUM(bunker) AS bunker_count,
            SUM(approach) AS approach_count,
            SUM(putt_sur_green) AS gir_count
        FROM hole_entries
        """
    ).fetchone()

    return jsonify(
        {
            "rounds": [dict(r) for r in rounds],
            "aggregate": dict(agg) if agg else {},
        }
    )


# ---------- static frontend ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
