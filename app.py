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


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            holes_count INTEGER NOT NULL,
            pars TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
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
    rows = db.execute("SELECT * FROM courses ORDER BY name").fetchall()
    return jsonify([dict(r, pars=[int(p) for p in r["pars"].split(",")]) for r in rows])


@app.post("/api/courses")
@login_required
def create_course():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    pars = data.get("pars") or []
    if not name or not pars:
        return jsonify({"error": "name_and_pars_required"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO courses (name, holes_count, pars, created_at) VALUES (?, ?, ?, ?)",
        (name, len(pars), ",".join(str(int(p)) for p in pars), datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


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
        "INSERT INTO rounds (course_id, played_on, notes, created_at) VALUES (?, ?, ?, ?)",
        (course_id, played_on, data.get("notes"), datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.get("/api/rounds/<int:round_id>")
@login_required
def get_round(round_id):
    db = get_db()
    r = db.execute(
        """
        SELECT r.*, c.name AS course_name, c.holes_count, c.pars
        FROM rounds r JOIN courses c ON c.id = r.course_id
        WHERE r.id = ?
        """,
        (round_id,),
    ).fetchone()
    if not r:
        return jsonify({"error": "not_found"}), 404
    holes = db.execute(
        "SELECT * FROM hole_entries WHERE round_id = ? ORDER BY hole_number", (round_id,)
    ).fetchall()
    result = dict(r, pars=[int(p) for p in r["pars"].split(",")])
    result["holes"] = [dict(h) for h in holes]
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
