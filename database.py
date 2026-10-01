import os
import sqlite3
from datetime import datetime, timezone

DB_NAME = os.getenv("DATABASE_PATH", "placement.db")


def _connect_db():
    """Create a SQLite connection with a consistent timeout and write-lock settings."""
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ensure_application_columns():
    """Add optional application metadata columns without resetting the existing schema."""
    with _connect_db() as conn:
        cursor = conn.cursor()
        existing_columns = {
            row[1] for row in cursor.execute("PRAGMA table_info(applications)").fetchall()
        }

        if "resume_path" not in existing_columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN resume_path TEXT")
        if "application_url" not in existing_columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN application_url TEXT")
        if "college" not in existing_columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN college TEXT")
        if "gender" not in existing_columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN gender TEXT")

        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_student_placement ON applications(student_id, placement_id)"
        )
        conn.commit()


def init_db():
    """Create all required database tables and enable WAL mode for better concurrency."""
    with _connect_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                phone TEXT,
                branch TEXT,
                graduation_year INTEGER,
                skills TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                description TEXT,
                website TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS placements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                job_role TEXT NOT NULL,
                location TEXT,
                eligibility TEXT,
                skills TEXT,
                salary TEXT,
                drive_date TEXT,
                drive_time TEXT,
                venue TEXT,
                FOREIGN KEY (company_id) REFERENCES companies(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                placement_id INTEGER NOT NULL,
                applied_date TEXT NOT NULL,
                status TEXT DEFAULT 'Applied',
                FOREIGN KEY (student_id) REFERENCES students(id),
                FOREIGN KEY (placement_id) REFERENCES placements(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS external_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL DEFAULT 'adzuna',
                source_job_id TEXT NOT NULL UNIQUE,
                company_name TEXT NOT NULL,
                job_title TEXT NOT NULL,
                location TEXT,
                job_description TEXT,
                eligibility TEXT,
                skills TEXT,
                salary TEXT,
                application_url TEXT,
                posted_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_sync_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS student_preparation (
                student_id INTEGER PRIMARY KEY,
                aptitude INTEGER DEFAULT 0,
                programming INTEGER DEFAULT 0,
                technical INTEGER DEFAULT 0,
                communication INTEGER DEFAULT 0,
                interview INTEGER DEFAULT 0,
                company_specific INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (student_id) REFERENCES students(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS student_career_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                branch TEXT,
                skills TEXT,
                interests TEXT,
                preferred_work_area TEXT,
                recommendations TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (student_id) REFERENCES students(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS student_interview_prep (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                completed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (student_id) REFERENCES students(id)
            )
        """)

        conn.commit()

    ensure_application_columns()


def add_sample_data():
    """Legacy sample data retained for the existing project and not treated as live API jobs."""
    with _connect_db() as conn:
        cursor = conn.cursor()

        companies = [
            (
                "Tech Solutions Pvt Ltd",
                "Software development and technology solutions company.",
                ""
            ),
            (
                "SecureTech Solutions",
                "Cyber security and information security company.",
                ""
            ),
            (
                "Digital Works",
                "Web development and digital technology company.",
                ""
            )
        ]

        for company in companies:
            cursor.execute(
                """
                INSERT INTO companies
                (company_name, description, website)
                SELECT ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM companies
                    WHERE company_name = ?
                )
                """,
                (
                    company[0],
                    company[1],
                    company[2],
                    company[0]
                )
            )

        conn.commit()

        cursor.execute("SELECT id, company_name FROM companies")
        company_data = cursor.fetchall()
        company_ids = {name: company_id for company_id, name in company_data}

        placements = [
            (
                company_ids["Tech Solutions Pvt Ltd"],
                "Software Developer",
                "Bangalore",
                "B.Tech / CSE / IT",
                "Java, Python, HTML, CSS",
                "6 LPA",
                "2026-08-20",
                "10:00 AM",
                "College Seminar Hall"
            ),
            (
                company_ids["SecureTech Solutions"],
                "Cyber Security Analyst",
                "Hyderabad",
                "B.Tech Cyber Security / CSE",
                "Networking, Security, Python",
                "7 LPA",
                "2026-08-25",
                "11:00 AM",
                "Placement Cell"
            ),
            (
                company_ids["Digital Works"],
                "Web Developer",
                "Pune",
                "B.Tech / BCA / MCA",
                "HTML, CSS, JavaScript",
                "5 LPA",
                "2026-08-28",
                "10:30 AM",
                "College Seminar Hall"
            )
        ]

        for placement in placements:
            cursor.execute(
                """
                INSERT INTO placements
                (
                    company_id,
                    job_role,
                    location,
                    eligibility,
                    skills,
                    salary,
                    drive_date,
                    drive_time,
                    venue
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM placements
                    WHERE company_id = ?
                    AND job_role = ?
                )
                """,
                placement + (placement[0], placement[1])
            )

        conn.commit()


def set_last_sync_time(timestamp_value):
    with _connect_db() as conn:
        conn.execute(
            "INSERT INTO job_sync_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            ("adzuna_last_sync", timestamp_value, timestamp_value)
        )
        conn.commit()


def sync_live_jobs(jobs):
    """Save current Adzuna jobs to the live tracking table and keep the existing companies/placements tables updated."""
    if not jobs:
        return 0

    inserted_count = 0

    with _connect_db() as conn:
        cursor = conn.cursor()

        for job in jobs:
            source_job_id = (job.get("source_job_id") or "").strip()
            company_name = (job.get("company_name") or "").strip()
            job_title = (job.get("job_title") or "").strip()
            location = (job.get("location") or "").strip()
            description = (job.get("job_description") or "").strip()
            eligibility = (job.get("eligibility") or "Open to eligible freshers and graduates").strip()
            skills = (job.get("required_skills") or "").strip()
            salary = (job.get("salary") or "").strip()
            application_url = (job.get("application_url") or "").strip()
            if application_url and not application_url.startswith(("http://", "https://")):
                application_url = ""
            posted_date = (job.get("posted_date") or "").strip()

            if not company_name or not job_title or not application_url:
                continue

            if not source_job_id:
                source_job_id = f"{company_name}-{job_title}-{location}-{posted_date or 'undated'}"

            existing_job = cursor.execute(
                "SELECT id FROM external_jobs WHERE source_job_id = ?",
                (source_job_id,)
            ).fetchone()

            if existing_job:
                cursor.execute(
                    """
                    UPDATE external_jobs
                    SET company_name = ?, job_title = ?, location = ?, job_description = ?, eligibility = ?,
                        skills = ?, salary = ?, application_url = ?, posted_date = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (company_name, job_title, location, description, eligibility, skills, salary, application_url, posted_date, existing_job[0])
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO external_jobs (
                        source_name, source_job_id, company_name, job_title, location, job_description,
                        eligibility, skills, salary, application_url, posted_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("adzuna", source_job_id, company_name, job_title, location, description, eligibility, skills, salary, application_url, posted_date)
                )
                inserted_count += 1

            company_row = cursor.execute(
                "SELECT id, website FROM companies WHERE company_name = ?",
                (company_name,)
            ).fetchone()

            if company_row:
                company_id = company_row[0]
                if not company_row[1] and application_url:
                    cursor.execute(
                        "UPDATE companies SET website = ?, description = COALESCE(description, ?) WHERE id = ?",
                        (application_url, description, company_id)
                    )
            else:
                cursor.execute(
                    "INSERT INTO companies (company_name, description, website) VALUES (?, ?, ?)",
                    (company_name, description, application_url if application_url else "")
                )
                company_id = cursor.execute(
                    "SELECT id FROM companies WHERE company_name = ?",
                    (company_name,)
                ).fetchone()[0]

            duplicate = cursor.execute(
                """
                SELECT id
                FROM placements
                WHERE company_id = ?
                  AND job_role = ?
                  AND location = ?
                  AND drive_date = ?
                """,
                (company_id, job_title, location, posted_date)
            ).fetchone()

            if duplicate:
                continue

            cursor.execute(
                """
                INSERT INTO placements (
                    company_id,
                    job_role,
                    location,
                    eligibility,
                    skills,
                    salary,
                    drive_date,
                    drive_time,
                    venue
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    job_title,
                    location,
                    eligibility,
                    skills,
                    salary,
                    posted_date,
                    job.get("drive_time") or "",
                    job.get("venue") or "Remote / Online"
                )
            )

        sync_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO job_sync_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            ("adzuna_last_sync", sync_time, sync_time)
        )
        conn.commit()

    return inserted_count


def get_live_jobs_from_db():
    """Return jobs synced from the live Adzuna API only. Sample records are excluded."""
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                p.id,
                e.id AS external_job_id,
                e.company_name,
                e.job_title AS job_role,
                e.location,
                e.eligibility,
                e.skills,
                e.salary,
                e.posted_date AS drive_date,
                '' AS drive_time,
                e.application_url AS application_url,
                e.job_description,
                e.source_job_id,
                                e.updated_at,
                                e.posted_date
            FROM external_jobs e
                        JOIN companies c ON c.company_name = e.company_name
                        JOIN placements p
                            ON p.company_id = c.id
                         AND p.job_role = e.job_title
                         AND COALESCE(p.location, '') = COALESCE(e.location, '')
                         AND COALESCE(p.drive_date, '') = COALESCE(e.posted_date, '')
            ORDER BY e.posted_date DESC, e.id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


if __name__ == "__main__":
    init_db()
    add_sample_data()

    print("Database initialized successfully.")
    print("Companies and placements added successfully.")