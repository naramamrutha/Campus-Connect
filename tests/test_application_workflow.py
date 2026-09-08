import io
import sqlite3
import uuid

from app import app, DB_NAME


def test_student_can_submit_application_with_resume():
    unique = uuid.uuid4().hex[:8]
    email = f"student_{unique}@example.com"

    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute(
            "INSERT INTO students (full_name, email, password, phone, branch, graduation_year, skills) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"Student {unique}", email, "secret123", "9876543210", "Computer Science", 2026, "Python, SQL")
        )
        student_id = conn.execute("SELECT id FROM students WHERE email = ?", (email,)).fetchone()[0]

        conn.execute(
            "INSERT INTO companies (company_name, description, website) VALUES (?, ?, ?)",
            (f"Acme Test {unique}", "Test company", "https://example.com")
        )
        company_id = conn.execute("SELECT id FROM companies WHERE company_name = ?", (f"Acme Test {unique}",)).fetchone()[0]

        conn.execute(
            "INSERT INTO placements (company_id, job_role, location, eligibility, skills, salary, drive_date, drive_time, venue) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, f"Software Engineer {unique}", "Bangalore", "B.Tech CSE", "Python", "8 LPA", "2026-10-01", "10:00 AM", "Online")
        )
        placement_id = conn.execute("SELECT id FROM placements WHERE job_role = ? ORDER BY id DESC LIMIT 1", (f"Software Engineer {unique}",)).fetchone()[0]

        conn.execute("DELETE FROM applications WHERE student_id = ? AND placement_id = ?", (student_id, placement_id))
        conn.commit()

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["student_id"] = student_id

        response = client.get(f"/apply?placement_id={placement_id}")
        assert response.status_code == 200
        assert b"Application Form" in response.data

        resume = (io.BytesIO(b"resume content"), "resume.pdf")
        response = client.post(
            "/apply",
            data={
                "placement_id": placement_id,
                "full_name": f"Student {unique}",
                "email": email,
                "phone": "9876543210",
                "gender": "Female",
                "college": "ABC College",
                "branch": "Computer Science",
                "graduation_year": "2026",
                "skills": "Python, SQL",
                "resume": resume,
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        assert response.status_code in (302, 303)

        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            app_record = conn.execute(
                "SELECT student_id, placement_id, college, gender, resume_path FROM applications WHERE student_id = ? AND placement_id = ?",
                (student_id, placement_id),
            ).fetchone()

        assert app_record is not None
        assert app_record[2] == "ABC College"
        assert app_record[3] == "Female"
        assert app_record[4] is not None and "resume" in app_record[4].lower()
