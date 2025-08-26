# Requirements: Streamlit is not installed by default.
# You need to install it before running this script.
#
# Local run:
#     pip install streamlit psycopg2-binary
#     streamlit run app.py
#
# On Streamlit Cloud:
# - Add a `requirements.txt` with:  
#       streamlit
#       psycopg2-binary
# - Set a secret/env var `DATABASE_URL` to your Supabase Postgres connection string, e.g.:  
#       postgresql://postgres:<PASSWORD>@db.<PROJECT>.supabase.co:5432/postgres?sslmode=require
#
# If DATABASE_URL is not set, the app falls back to a local SQLite file `test_mgmt.db`.

import os
import streamlit as st
import sqlite3
import json
from datetime import datetime
from typing import List, Optional

DB_PATH = "test_mgmt.db"
DB_URL = os.getenv("DATABASE_URL", "").strip()
DB_BACKEND = "postgres" if DB_URL.lower().startswith("postgres") else "sqlite"

# ---------------------------
# Database utilities
# ---------------------------

def _conn_sqlite():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _conn_postgres():
    try:
        import psycopg2  # installed via psycopg2-binary
    except Exception as e:
        raise RuntimeError("psycopg2-binary is required for Postgres/Supabase. Add it to requirements.txt") from e
    return psycopg2.connect(DB_URL)


def get_conn():
    return _conn_postgres() if DB_BACKEND == "postgres" else _conn_sqlite()


def q(sql: str) -> str:
    """Adapt placeholder style between sqlite (?) and postgres (%s)."""
    if DB_BACKEND == "postgres":
        return sql.replace("?", "%s")
    return sql


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    if DB_BACKEND == "sqlite":
        # users
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('tester','testlead'))
            );
            """
        )
        # sessions
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # test_cases
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS test_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT,
                title TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                expected_result TEXT NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('integration','studio')),
                author_id INTEGER,
                FOREIGN KEY(author_id) REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )
        # ensure external_id column exists (upgrade path) and unique index
        cur.execute("PRAGMA table_info(test_cases)")
        cols = [r[1] for r in cur.fetchall()]
        if "external_id" not in cols:
            cur.execute("ALTER TABLE test_cases ADD COLUMN external_id TEXT")
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_test_cases_external_id
            ON test_cases(external_id)
            WHERE external_id IS NOT NULL
            """
        )
        # runs
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_case_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                runner_id INTEGER,
                url TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('FT','SIT','UAT')),
                status TEXT NOT NULL CHECK(status IN ('passed','failed')),
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(test_case_id) REFERENCES test_cases(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                FOREIGN KEY(runner_id) REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )
        # failures
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER UNIQUE NOT NULL,
                severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
                noted_by INTEGER,
                noted_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES test_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(noted_by) REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )

    else:  # postgres (Supabase)
        # users
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('tester','testlead'))
            );
            """
        )
        # sessions
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # test_cases
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS test_cases (
                id SERIAL PRIMARY KEY,
                external_id TEXT UNIQUE,
                title TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                expected_result TEXT NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('integration','studio')),
                author_id INTEGER REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )
        # runs
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS test_runs (
                id SERIAL PRIMARY KEY,
                test_case_id INTEGER NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                runner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                url TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('FT','SIT','UAT')),
                status TEXT NOT NULL CHECK(status IN ('passed','failed')),
                comment TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        # failures
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS failures (
                id SERIAL PRIMARY KEY,
                run_id INTEGER UNIQUE NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
                severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
                noted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                noted_at TEXT NOT NULL
            );
            """
        )

    conn.commit()
    conn.close()


# ---------------------------
# Helper functions
# ---------------------------

def get_next_external_id() -> str:
    """Generate next external test case ID like 'TC-1', 'TC-2', ..."""
    conn = get_conn()
    cur = conn.cursor()
    if DB_BACKEND == "postgres":
        cur.execute(
            """
            SELECT MAX(CAST(SUBSTRING(external_id FROM 4) AS INTEGER))
            FROM test_cases
            WHERE external_id LIKE 'TC-%'
            """
        )
    else:
        cur.execute(
            """
            SELECT MAX(CAST(SUBSTR(external_id, 4) AS INTEGER))
            FROM test_cases
            WHERE external_id LIKE 'TC-%'
            """
        )
    row = cur.fetchone()
    conn.close()
    next_num = (row[0] or 0) + 1
    return f"TC-{next_num}"


def upsert_user(name: str, role: str):
    conn = get_conn()
    cur = conn.cursor()
    try:
        if DB_BACKEND == "postgres":
            cur.execute(
                """
                INSERT INTO users(name, role) VALUES (%s, %s)
                ON CONFLICT(name) DO UPDATE SET role = EXCLUDED.role
                """,
                (name.strip(), role),
            )
        else:  # sqlite
            cur.execute("INSERT OR IGNORE INTO users(name, role) VALUES (?,?)", (name.strip(), role))
            if cur.rowcount == 0:
                cur.execute("UPDATE users SET role=? WHERE name=?", (role, name.strip()))
        conn.commit()
    finally:
        conn.close()


def list_users():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, role FROM users ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    return rows


def role_of(user_id: Optional[int]) -> Optional[str]:
    if user_id is None:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(q("SELECT role FROM users WHERE id=?"), (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def create_session(name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        q("INSERT INTO sessions(name, created_at, closed) VALUES (?,?,0)"),
        (name.strip(), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def list_sessions(include_closed=True):
    conn = get_conn()
    cur = conn.cursor()
    if include_closed:
        cur.execute("SELECT id, name, created_at, closed FROM sessions ORDER BY id DESC")
    else:
        cur.execute("SELECT id, name, created_at, closed FROM sessions WHERE closed=0 ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def close_session(session_id: int) -> bool:
    """A session can be closed if each test case has at least one PASSED run in this session."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        q(
            """
            SELECT COUNT(*)
            FROM test_cases tc
            WHERE NOT EXISTS (
                SELECT 1 FROM test_runs r
                WHERE r.test_case_id = tc.id AND r.session_id = ? AND r.status = 'passed'
            )
            """
        ),
        (session_id,),
    )
    missing_pass = cur.fetchone()[0]
    if missing_pass > 0:
        conn.close()
        return False

    cur.execute(q("UPDATE sessions SET closed=1 WHERE id=?"), (session_id,))
    conn.commit()
    conn.close()
    return True


def add_test_case(title: str, steps: List[str], expected: str, category: str, author_id: Optional[int]):
    conn = get_conn()
    cur = conn.cursor()
    steps_json = json.dumps(steps)
    external_id = get_next_external_id()
    cur.execute(
        q(
            """
            INSERT INTO test_cases(external_id, title, steps_json, expected_result, category, author_id)
            VALUES (?,?,?,?,?,?)
            """
        ),
        (external_id, title.strip(), steps_json, expected.strip(), category, author_id),
    )
    conn.commit()
    conn.close()
    return external_id


def list_test_cases():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT tc.id, tc.external_id, tc.title, tc.category, tc.expected_result, tc.steps_json, u.name AS author
        FROM test_cases tc LEFT JOIN users u ON u.id = tc.author_id
        ORDER BY tc.id DESC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def record_test_run(test_case_id: int, session_id: int, runner_id: Optional[int], url: str, phase: str, status: str, comment: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        q(
            """
            INSERT INTO test_runs(test_case_id, session_id, runner_id, url, phase, status, comment, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """
        ),
        (test_case_id, session_id, runner_id, url.strip(), phase, status, comment.strip(), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def list_test_runs(session_id: Optional[int] = None, only_failed: bool = False):
    conn = get_conn()
    cur = conn.cursor()
    qparts = [
        "SELECT r.id, r.test_case_id, r.session_id, r.url, r.phase, r.status, r.comment, r.created_at,",
        "tc.title, tc.category, u.name as runner, f.severity, tc.external_id",
        "FROM test_runs r",
        "JOIN test_cases tc ON tc.id = r.test_case_id",
        "LEFT JOIN users u ON u.id = r.runner_id",
        "LEFT JOIN failures f ON f.run_id = r.id",
        "WHERE 1=1",
    ]
    params = []
    if session_id is not None:
        qparts.append("AND r.session_id = ?")
        params.append(session_id)
    if only_failed:
        qparts.append("AND r.status = 'failed'")
    qparts.append("ORDER BY r.created_at DESC")
    sql = "\n".join(qparts)
    cur.execute(q(sql), tuple(params))
    rows = cur.fetchall()
    conn.close()
    return rows


def classify_failure(run_id: int, severity: str, user_id: Optional[int]):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        q("INSERT OR REPLACE INTO failures(run_id, severity, noted_by, noted_at) VALUES (?,?,?,?)")
        if DB_BACKEND == "sqlite"
        else "INSERT INTO failures(run_id, severity, noted_by, noted_at) VALUES (%s,%s,%s,%s)\n             ON CONFLICT(run_id) DO UPDATE SET severity=EXCLUDED.severity, noted_by=EXCLUDED.noted_by, noted_at=EXCLUDED.noted_at",
        (run_id, severity, user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def counts_for_session(session_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(q("SELECT COUNT(*) FROM test_runs WHERE session_id=?"), (session_id,))
    total_runs = cur.fetchone()[0]

    cur.execute(q("SELECT COUNT(*) FROM test_runs WHERE session_id=? AND status='failed'"), (session_id,))
    failed_runs = cur.fetchone()[0]

    cur.execute(
        q(
            """
            SELECT COUNT(*) FROM test_cases tc
            WHERE NOT EXISTS (
                SELECT 1 FROM test_runs r
                WHERE r.test_case_id = tc.id AND r.session_id = ? AND r.status='passed'
            )
            """
        ),
        (session_id,),
    )
    to_execute = cur.fetchone()[0]

    cur.execute(
        q(
            """
            SELECT COALESCE(SUM(CASE WHEN f.severity='minor' THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN f.severity='major' THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN f.severity='critical' THEN 1 ELSE 0 END),0)
            FROM failures f JOIN test_runs r ON r.id=f.run_id WHERE r.session_id=?
            """
        ),
        (session_id,),
    )
    minor, major, critical = cur.fetchone()

    cur.execute(
        q(
            """
            SELECT tc.id, tc.external_id, tc.title, tc.category, COALESCE(u.name,'—')
            FROM test_cases tc
            LEFT JOIN users u ON u.id = tc.author_id
            WHERE NOT EXISTS (
                SELECT 1 FROM test_runs r
                WHERE r.test_case_id = tc.id AND r.session_id = ? AND r.status='passed'
            )
            ORDER BY tc.id ASC
            """
        ),
        (session_id,),
    )
    needing_pass = cur.fetchall()

    conn.close()
    return {
        "total_runs": total_runs,
        "failed_runs": failed_runs,
        "to_execute": to_execute,
        "minor": minor,
        "major": major,
        "critical": critical,
        "needing_pass": needing_pass,
    }


# ---------------------------
# UI Helpers
# ---------------------------

def require_current_user():
    st.sidebar.subheader("Current User")
    users = list_users()
    names = [u[1] for u in users]
    name_to_id = {u[1]: u[0] for u in users}
    selected = st.sidebar.selectbox("Pick your user", options=["<anonymous>"] + names)
    if selected == "<anonymous>":
        return None
    return name_to_id[selected]


# ---------------------------
# Pages
# ---------------------------

def page_overview():
    st.title("Test Management App — Overview")
    st.markdown(
        """
        **What you can do here**

        - **Users**: Add testers and test leads.
        - **Test Cases** *(test leads)*: Create cases; each one gets an auto-generated **Test Case ID** like `TC-1`, `TC-2`, ... with up to five steps, expected result, and category (**integration** or **studio**).
        - **Run Tests**: Select a case, provide a URL, choose phase (**FT / SIT / UAT**), set status (**passed / failed**), and add an optional comment.
        - **Failures** *(test leads)*: Review failed runs and classify them as **minor / major / critical** (reporting only).
        - **Dashboard**: See totals and a live list of test cases that **still need a PASS** this session, and run them directly from there.
        - **Sessions**: Create a test session and close it once **every test case has at least one PASSED run** in that session.

        Use the sidebar to select your **user** and the **active session**.
        """
    )


def page_users():
    st.title("Manage Users")
    with st.form("add_user"):
        name = st.text_input("Name")
        role = st.selectbox("Role", options=["tester", "testlead"])
        submitted = st.form_submit_button("Add / Update User")
        if submitted:
            if not name.strip():
                st.error("Name is required")
            else:
                try:
                    upsert_user(name, role)
                    st.success(f"User '{name}' set as {role}.")
                except Exception as e:
                    st.error(f"Failed to add/update user: {e}")

    st.subheader("Current Users")
    rows = list_users()
    if not rows:
        st.info("No users yet.")
    else:
        st.table({
            "ID": [r[0] for r in rows],
            "Name": [r[1] for r in rows],
            "Role": [r[2] for r in rows],
        })


def page_sessions(current_user_id: Optional[int]):
    st.title("Sessions")
    st.caption("Create and close sessions. You can close a session only when each test case has at least one PASSED run in it.")

    with st.form("new_session"):
        s_name = st.text_input("New Session Name (e.g. '2025-08 SIT Cycle 3')")
        ok = st.form_submit_button("Create Session")
        if ok:
            if not s_name.strip():
                st.error("Session name is required.")
            else:
                try:
                    create_session(s_name)
                    st.success(f"Session '{s_name}' created.")
                except Exception as e:
                    st.error(f"Failed to create session: {e}")

    st.subheader("All Sessions")
    rows = list_sessions(include_closed=True)
    if rows:
        for sid, name, created_at, closed in rows:
            cols = st.columns([4,2,2,3])
            cols[0].markdown(f"**{name}**  ")
            cols[1].markdown(f"Created: {created_at[:19].replace('T',' ')}")
            cols[2].markdown("Status: ✅ Closed" if closed else "Status: 🟢 Open")
            with cols[3]:
                if not closed:
                    if st.button("Close session", key=f"close_{sid}"):
                        if close_session(sid):
                            st.success(f"Session '{name}' closed.")
                        else:
                            st.error("Cannot close: At least one test case has no PASSED run in this session.")
    else:
        st.info("No sessions yet.")


def page_test_cases(current_user_id: Optional[int]):
    r = role_of(current_user_id)
    st.title("Test Cases")
    if r != "testlead":
        st.warning("Only test leads can add test cases. You can still browse existing cases below.")

    with st.expander("Add a Test Case", expanded=(r=="testlead")):
        with st.form("add_tc"):
            st.caption("The ID will be assigned automatically (e.g. TC-1, TC-2).")
            title = st.text_input("Title")
            category = st.selectbox("Category", options=["integration","studio"])
            num_steps = st.number_input("Number of steps (1-5)", min_value=1, max_value=5, value=1, step=1)
            steps = []
            for i in range(num_steps):
                steps.append(st.text_area(f"Step {i+1}", key=f"step_{i}"))
            expected = st.text_area("Expected Result")
            submitted = st.form_submit_button("Create Test Case")
            if submitted:
                if not title.strip():
                    st.error("Title is required.")
                elif any(not s.strip() for s in steps):
                    st.error("All steps must be filled.")
                elif not expected.strip():
                    st.error("Expected result is required.")
                else:
                    try:
                        new_id = add_test_case(title, steps, expected, category, current_user_id)
                        st.success(f"Test case created with ID: {new_id}")
                    except Exception as e:
                        st.error(f"Failed to create test case: {e}")

    st.subheader("Existing Test Cases")
    rows = list_test_cases()
    if not rows:
        st.info("No test cases yet.")
    else:
        for (tc_pk, ext_id, title, category, expected, steps_json, author) in rows:
            with st.container():
                st.markdown(f"**[{ext_id or tc_pk}] {title}** — _{category}_  ")
                steps = json.loads(steps_json)
                for i, s in enumerate(steps, start=1):
                    st.markdown(f"**Step {i}.** {s}")
                st.markdown(f"**Expected:** {expected}")
                st.caption(f"Author: {author or '—'}")


def page_run_tests(current_user_id: Optional[int], active_session_id: Optional[int]):
    st.title("Run Tests")
    if active_session_id is None:
        st.error("Select or create an active session in the sidebar first.")
        return

    rows = list_test_cases()
    if not rows:
        st.info("No test cases available.")
        return

    with st.form("run_form"):
        tc_options = {f"[{r[1] or r[0]}] {r[2]} ({r[3]})": r[0] for r in rows}
        tc_label = st.selectbox("Test Case", options=list(tc_options.keys()))
        url = st.text_input("URL under test")
        phase = st.selectbox("Phase", options=["FT","SIT","UAT"])
        status = st.selectbox("Status", options=["passed","failed"])
        comment = st.text_area("Comment (optional)")
        submitted = st.form_submit_button("Record Run")
        if submitted:
            if not url.strip():
                st.error("URL is required.")
            else:
                try:
                    record_test_run(
                        test_case_id=tc_options[tc_label],
                        session_id=active_session_id,
                        runner_id=current_user_id,
                        url=url,
                        phase=phase,
                        status=status,
                        comment=comment or "",
                    )
                    st.success("Run recorded.")
                except Exception as e:
                    st.error(f"Failed to record run: {e}")

    st.subheader("Recent Runs (this session)")
    for row in list_test_runs(session_id=active_session_id):
        (run_id, tc_id, sess_id, url, phase, status, comment, created_at, title, tc_cat, runner, severity, ext_id) = row
        with st.container():
            st.markdown(f"**Run #{run_id}** — {created_at[:19].replace('T',' ')}  ")
            st.markdown(f"**TC [{ext_id or tc_id}]** {title} _({tc_cat})_  ")
            st.markdown(f"Phase: **{phase}** | Status: **{status.upper()}** | URL: {url}")
            if comment:
                st.caption(comment)
            if severity:
                st.warning(f"Failure classified: {severity.upper()}")


def page_failures(current_user_id: Optional[int], active_session_id: Optional[int]):
    st.title("Review & Classify Failures (Test Leads)")
    if role_of(current_user_id) != "testlead":
        st.error("Only test leads can classify failures.")
        return
    if active_session_id is None:
        st.error("Select or create an active session in the sidebar first.")
        return

    rows = list_test_runs(session_id=active_session_id, only_failed=True)
    if not rows:
        st.info("No failed runs in this session.")
        return

    for row in rows:
        (run_id, tc_id, sess_id, url, phase, status, comment, created_at, title, tc_cat, runner, severity, ext_id) = row
        with st.container():
            st.markdown(f"**Run #{run_id}** — {created_at[:19].replace('T',' ')} | TC [{ext_id or tc_id}] {title} ({tc_cat}) | Runner: {runner or '—'}")
            st.markdown(f"URL: {url} | Phase: {phase}")
            if comment:
                st.caption(f"Comment: {comment}")
            cols = st.columns([3,2])
            with cols[0]:
                sev = st.selectbox(
                    "Severity",
                    options=["minor","major","critical"],
                    index=["minor","major","critical"].index(severity) if severity in ("minor","major","critical") else 0,
                    key=f"sev_{run_id}",
                )
            with cols[1]:
                if st.button("Save classification", key=f"save_{run_id}"):
                    try:
                        classify_failure(run_id, sev, current_user_id)
                        st.success("Saved.")
                    except Exception as e:
                        st.error(f"Failed to save: {e}")


def page_dashboard(active_session_id: Optional[int], current_user_id: Optional[int]):
    st.title("Dashboard")
    if active_session_id is None:
        st.error("Select or create an active session in the sidebar first.")
        return
    c = counts_for_session(active_session_id)

    col1, col2, col3 = st.columns(3)
    col1.metric("All test runs", c["total_runs"])
    col2.metric("Failed runs", c["failed_runs"])
    col3.metric("Need a PASS", c["to_execute"])

    st.subheader("Failure severity in session (info)")
    s1, s2, s3 = st.columns(3)
    s1.metric("Minor", c["minor"])
    s2.metric("Major", c["major"])
    s3.metric("Critical", c["critical"])

    st.subheader("Test cases without a PASS yet (this session)")
    needing = c["needing_pass"]
    if not needing:
        st.success("All test cases have at least one PASSED run in this session. 🎉")
        return

    table_rows = [{
        "TC ID": (ext_id or pk),
        "Title": title,
        "Category": category,
        "Author": author,
    } for (pk, ext_id, title, category, author) in needing]
    st.table(table_rows)

    st.caption("Run any of these directly:")

    for (pk, ext_id, title, category, author) in needing:
        key_prefix = f"needpass_{pk}"
        with st.expander(f"Run [{ext_id or pk}] {title} (by {author})"):
            with st.form(f"run_{key_prefix}"):
                url = st.text_input("URL under test", key=f"url_{key_prefix}")
                phase = st.selectbox("Phase", options=["FT","SIT","UAT"], key=f"phase_{key_prefix}")
                status = st.selectbox("Status", options=["passed","failed"], index=0, key=f"status_{key_prefix}")
                comment = st.text_area("Comment (optional)", key=f"comment_{key_prefix}")
                submitted = st.form_submit_button("Record Run")
                if submitted:
                    if not url.strip():
                        st.error("URL is required.")
                    else:
                        try:
                            record_test_run(
                                test_case_id=pk,
                                session_id=active_session_id,
                                runner_id=current_user_id,
                                url=url,
                                phase=phase,
                                status=status,
                                comment=comment or "",
                            )
                            st.success("Run recorded.")
                        except Exception as e:
                            st.error(f"Failed to record run: {e}")


# ---------------------------
# App bootstrap
# ---------------------------

def main():
    st.set_page_config(page_title="Test Management App", layout="wide")
    init_db()

    st.sidebar.title("Navigation")
    st.sidebar.caption(f"DB backend: {DB_BACKEND}")
    page = st.sidebar.radio(
        "Go to",
        [
            "Overview",
            "Users",
            "Sessions",
            "Test Cases",
            "Run Tests",
            "Failures",
            "Dashboard",
        ],
    )

    # Current user
    current_user_id = require_current_user()

    # Active session selector
    st.sidebar.subheader("Active Session")
    sessions = list_sessions(include_closed=False)
    if sessions:
        names = [f"[{s[0]}] {s[1]}" for s in sessions]
        sel = st.sidebar.selectbox("Open sessions", options=names)
        active_session_id = int(sel.split(']')[0][1:])
    else:
        st.sidebar.info("No open sessions. Create one in the Sessions page.")
        active_session_id = None

    if page == "Overview":
        page_overview()
    elif page == "Users":
        page_users()
    elif page == "Sessions":
        page_sessions(current_user_id)
    elif page == "Test Cases":
        page_test_cases(current_user_id)
    elif page == "Run Tests":
        page_run_tests(current_user_id, active_session_id)
    elif page == "Failures":
        page_failures(current_user_id, active_session_id)
    elif page == "Dashboard":
        page_dashboard(active_session_id, current_user_id)


if __name__ == "__main__":
    main()
