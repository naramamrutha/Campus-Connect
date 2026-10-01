import json
import os
import re
import ssl
import uuid
import certifi
from dotenv import load_dotenv
import sqlite3
from datetime import datetime, timezone
from urllib import error, request as urllib_request
from urllib.parse import urlencode, urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.utils import secure_filename

from database import DB_NAME, get_live_jobs_from_db, init_db, sync_live_jobs


load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "student-placement-system-secret")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(app.root_path, "uploads", "resumes"))

init_db()

LIVE_JOBS_CACHE_SECONDS = 1800


def is_valid_http_url(value):
    """Accept only real HTTP/S URLs and reject placeholders or fake links."""
    text = (value or "").strip()
    if not text:
        return False
    if not re.match(r"^https?://", text, re.IGNORECASE):
        return False
    parsed = urlparse(text)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname == "example.com" or hostname.endswith(".example.com"):
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def normalize_application_url(value):
    """Normalize and validate application URLs before storing or displaying them."""
    text = (value or "").strip()
    if not text:
        return ""
    if not is_valid_http_url(text):
        return ""
    return text


def format_salary(salary_min, salary_max):
    """Format salary values into a readable string."""
    if not salary_min and not salary_max:
        return "Salary not disclosed"

    try:
        if salary_min and salary_max:
            return f"₹{int(salary_min):,} - ₹{int(salary_max):,}"
        if salary_min:
            return f"₹{int(salary_min):,}+"
        if salary_max:
            return f"Up to ₹{int(salary_max):,}"
    except (TypeError, ValueError):
        pass

    return "Salary not disclosed"


def fetch_live_jobs_from_adzuna(limit=12):
    """Fetch fresher-friendly jobs from the Adzuna API."""
    app_id = os.getenv("ADZUNA_APP_ID")
    api_key = os.getenv("ADZUNA_API_KEY")

    if not app_id or not api_key:
        return {
            "jobs": [],
            "error": "Adzuna API credentials are not configured. Set ADZUNA_APP_ID and ADZUNA_API_KEY in the project .env file."
        }

    keywords="software developer"
    

    params = {
        "app_id": app_id,
        "app_key": api_key,
        "results_per_page": limit,
        "what": keywords,
        "where": "India",
        "sort_by": "date",
        "content-type": "application/json"
    }

    api_url = "https://api.adzuna.com/v1/api/jobs/in/search/1?" + urlencode(params)
    req = urllib_request.Request(api_url, headers={"Accept": "application/json"})

    try:
        ssl_context=ssl.create_default_context(cafile=certifi.where())
        with urllib_request.urlopen(req, timeout=20, context=ssl_context) as response:
            payload = json.load(response)
    except error.URLError as exc:
        return {
            "jobs": [],
            "error": f"Adzuna API connection error: {exc}"
        }
    except (ValueError, json.JSONDecodeError) as exc:
        return {"jobs": [], "error": f"Adzuna returned an invalid response: {exc}"}

    results = payload.get("results", [])
    jobs = []

    for item in results:
        company = item.get("company") or {}
        location_data = item.get("location") or {}
        location = location_data.get("display_name") or ""
        title = (item.get("title") or "").strip()
        company_name = (company.get("display_name") or "").strip()
        description = item.get("description") or ""
        posted_date = item.get("created") or ""
        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        category = item.get("category") or {}
        source_job_id = str(item.get("id") or f"{company_name}-{title}-{location}-{posted_date}")
        application_url = normalize_application_url(item.get("redirect_url"))

        if not title or not company_name or not application_url:
            continue

        jobs.append({
            "source_job_id": source_job_id,
            "company_name": company_name,
            "job_title": title,
            "location": location,
            "job_description": description,
            "required_skills": category.get("label") or "",
            "eligibility": "Open to eligible freshers and graduates",
            "salary": format_salary(salary_min, salary_max),
            "application_url": application_url,
            "posted_date": posted_date[:10] if posted_date else "",
            "drive_time": "",
            "venue": ""
        })

    return {"jobs": jobs, "error": None}


def should_refresh_live_jobs():
    """Only refresh Adzuna when cache is expired or no jobs exist."""
    live_jobs = get_live_jobs_from_db()
    if not live_jobs:
        return True

    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        row = connection.execute(
            "SELECT value FROM job_sync_state WHERE key = 'adzuna_last_sync'"
        ).fetchone()

    if not row or not row[0]:
        return True

    try:
        last_sync = datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return True

    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    else:
        last_sync = last_sync.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    return (now - last_sync).total_seconds() >= LIVE_JOBS_CACHE_SECONDS


def get_student_by_id(student_id):
    """Fetch the current logged-in student's record from the students table."""
    try:
        student_id = int(student_id)
    except (TypeError, ValueError):
        return None

    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, full_name, email, password, phone, branch, graduation_year, skills
            FROM students
            WHERE id = ?
            """,
            (student_id,)
        ).fetchone()

    if row is None:
        return None

    student = dict(row)
    if student.get("graduation_year") is not None:
        student["graduation_year"] = int(student["graduation_year"])
    return student


def get_student_preparation(student_id):
    """Return the current student's preparation tracker progress."""
    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        row = connection.execute(
            """
            SELECT aptitude, programming, technical, communication, interview, company_specific
            FROM student_preparation
            WHERE student_id = ?
            """,
            (student_id,)
        ).fetchone()

    if row is None:
        return {
            'aptitude': 0,
            'programming': 0,
            'technical': 0,
            'communication': 0,
            'interview': 0,
            'company_specific': 0,
        }

    return {
        'aptitude': int(row[0] or 0),
        'programming': int(row[1] or 0),
        'technical': int(row[2] or 0),
        'communication': int(row[3] or 0),
        'interview': int(row[4] or 0),
        'company_specific': int(row[5] or 0),
    }


def save_student_preparation(student_id, data):
    """Persist preparation progress for the logged-in student."""
    def parse_progress(value):
        try:
            return max(0, min(100, int(value or 0)))
        except (TypeError, ValueError):
            return 0

    values = {
        'aptitude': parse_progress(data.get('aptitude')),
        'programming': parse_progress(data.get('programming')),
        'technical': parse_progress(data.get('technical')),
        'communication': parse_progress(data.get('communication')),
        'interview': parse_progress(data.get('interview')),
        'company_specific': parse_progress(data.get('company_specific')),
    }

    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        connection.execute(
            """
            INSERT INTO student_preparation (
                student_id, aptitude, programming, technical, communication, interview, company_specific
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id) DO UPDATE SET
                aptitude = excluded.aptitude,
                programming = excluded.programming,
                technical = excluded.technical,
                communication = excluded.communication,
                interview = excluded.interview,
                company_specific = excluded.company_specific,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                student_id,
                values['aptitude'],
                values['programming'],
                values['technical'],
                values['communication'],
                values['interview'],
                values['company_specific'],
            )
        )
        connection.commit()

    return values


def get_preparation_summary(tracker):
    """Summarize the persisted preparation tracker for dashboard progress cards."""
    categories = ['aptitude', 'programming', 'technical', 'communication', 'interview', 'company_specific']
    values = [max(0, min(100, int(tracker.get(category, 0) or 0))) for category in categories]
    completed = sum(value >= 80 for value in values)
    return {
        'overall': round(sum(values) / len(values)) if values else 0,
        'completed': completed,
        'remaining': len(values) - completed,
        'categories': len(values),
    }


def get_career_guidance(student):
    """Provide structured, profile-based guidance with role-specific roadmap items."""
    branch = (student.get('branch') or '').strip()
    skills = (student.get('skills') or '').strip()
    branch_key = normalize_text(branch)
    skill_key = normalize_text(skills)

    def make_path(title, why, required, improve, roles):
        return {
            'title': title,
            'why': why,
            'required_skills': required,
            'skills_to_improve': improve,
            'entry_roles': roles,
        }

    if any(contains_keyword(branch_key, token) for token in ['computer science', 'cse', 'information technology', 'it']):
        recommendations = [
            make_path(
                'Software Developer',
                'Your branch and skills align closely with programming, problem solving, and application development.',
                ['Programming fundamentals', 'Data structures and algorithms', 'SQL and databases', 'Web development basics'],
                ['System design awareness', 'Testing and debugging', 'Communication and project presentation'],
                ['Junior Software Engineer', 'Web Developer Intern', 'Python/Java Developer']
            ),
            make_path(
                'Backend Developer',
                'Your profile suggests interest in logic-heavy development, APIs, and database-driven systems.',
                ['Python/Java', 'REST APIs', 'SQL', 'Core OOP principles'],
                ['Database optimization', 'API design', 'Production debugging'],
                ['Backend Developer Intern', 'API Engineer', 'Database Support Engineer']
            ),
            make_path(
                'Full Stack Developer',
                'This matches students with both coding and frontend development interests.',
                ['HTML/CSS/JavaScript', 'Python/Java', 'Database work', 'Version control'],
                ['UI/UX clarity', 'Performance tuning', 'Deployment workflows'],
                ['Frontend Developer Intern', 'Full Stack Intern', 'Product Engineer']
            ),
        ]
    elif any(contains_keyword(branch_key, token) for token in ['cyber', 'security']):
        recommendations = [
            make_path(
                'Cyber Security Analyst',
                'Your profile matches security-focused roles that require analytical thinking and network awareness.',
                ['Networking', 'Security fundamentals', 'Linux basics', 'Python scripting'],
                ['Threat analysis', 'Incident response', 'Security tools'],
                ['SOC Analyst Intern', 'Security Analyst', 'IT Security Associate']
            ),
            make_path(
                'SOC Analyst',
                'This role suits students who enjoy monitoring systems, detecting alerts, and understanding risk.',
                ['Log analysis', 'Networking', 'Basic scripting', 'Incident handling'],
                ['Cloud basics', 'Automation', 'Risk communication'],
                ['Junior SOC Analyst', 'Analyst Trainee', 'Security Operations Intern']
            ),
            make_path(
                'Security Engineer',
                'This path works well for students with a technical mindset and interest in system protection.',
                ['Linux', 'Networking', 'Security protocols', 'Python/SQL'],
                ['Pen testing basics', 'Firewall concepts', 'Cloud security'],
                ['Security Engineer Intern', 'Junior Security Engineer', 'Infrastructure Security Trainee']
            ),
        ]
    elif any(contains_keyword(branch_key, token) for token in ['ece', 'electronics', 'electronic']):
        recommendations = [
            make_path(
                'Embedded Engineer',
                'Electronics and embedded systems work aligns well with hardware-software integration and circuit understanding.',
                ['Embedded C', 'Microcontrollers', 'Circuit fundamentals', 'Electronics basics'],
                ['RTOS concepts', 'Testing skills', 'Communication'],
                ['Embedded Engineer Intern', 'Firmware Trainee', 'Electronics Engineer']
            ),
            make_path(
                'Electronics Engineer',
                'Your branch supports roles that combine circuit design, testing, and technical troubleshooting.',
                ['Electronic components', 'Circuit design', 'Debugging', 'MATLAB/Simulation basics'],
                ['PCB design', 'Signal analysis', 'Documentation'],
                ['Electronics Engineer Intern', 'Hardware Test Engineer', 'Design Trainee']
            ),
            make_path(
                'IoT Engineer',
                'This path is suitable for students who like practical hardware projects and connected systems.',
                ['Sensors', 'Microcontrollers', 'C/C++', 'Basic networking'],
                ['Cloud interfaces', 'Wireless protocols', 'System integration'],
                ['IoT Intern', 'Embedded Systems Trainee', 'Hardware Support Engineer']
            ),
        ]
    elif any(contains_keyword(branch_key, token) for token in ['eee', 'electrical']):
        recommendations = [
            make_path(
                'Electrical Engineer',
                'Your branch matches roles requiring power systems knowledge, maintenance, and technical problem solving.',
                ['Electrical fundamentals', 'Circuit analysis', 'Safety standards', 'MATLAB basics'],
                ['Automation', 'Power systems tools', 'Technical communication'],
                ['Junior Electrical Engineer', 'Power Systems Intern', 'Maintenance Engineer']
            ),
            make_path(
                'Power Systems Engineer',
                'This path fits students with interest in electrical design, networks, and systems operations.',
                ['Power systems', 'Transformers', 'Protection concepts', 'Electrical drawings'],
                ['Simulation tools', 'Load analysis', 'Project documentation'],
                ['Power Engineer Intern', 'Operations Trainee', 'Maintenance Engineer']
            ),
        ]
    elif contains_keyword(branch_key, 'mechanical'):
        recommendations = [
            make_path(
                'Design Engineer',
                'Mechanical engineering strengths align well with design, drafting, and product development.',
                ['CAD tools', 'Manufacturing basics', 'Design concepts', 'Materials knowledge'],
                ['Quality analysis', '3D modeling', 'Problem solving'],
                ['Design Engineer Intern', 'CAD Engineer', 'Production Trainee']
            ),
            make_path(
                'Manufacturing Engineer',
                'This route suits students interested in industrial processes, optimization, and production workflows.',
                ['Manufacturing process', 'Lean concepts', 'Quality control', 'Machine knowledge'],
                ['Automation basics', 'SAP/ERP exposure', 'Process documentation'],
                ['Manufacturing Intern', 'Production Trainee', 'Process Engineer']
            ),
        ]
    elif contains_keyword(branch_key, 'civil'):
        recommendations = [
            make_path(
                'Site Engineer',
                'Civil engineering backgrounds align strongly with field execution, site work, and project management.',
                ['Surveying', 'Construction basics', 'AutoCAD', 'Site planning'],
                ['Project management', 'Quantity estimation', 'Communication'],
                ['Site Engineer Intern', 'Junior Project Engineer', 'Construction Trainee']
            ),
            make_path(
                'Project Engineer',
                'This role matches students who can manage schedules, materials, and on-site coordination.',
                ['Construction methods', 'Cost estimation', 'Scheduling', 'Documentation'],
                ['Risk understanding', 'AutoCAD', 'Coordination skills'],
                ['Project Engineer Intern', 'Construction Engineer', 'Site Supervisor']
            ),
        ]
    else:
        recommendations = [
            make_path(
                'General Engineering Roles',
                'Your profile suggests a broad engineering foundation with room to grow into technical and operations roles.',
                ['Problem solving', 'Communication', 'Core technical basics', 'Teamwork'],
                ['Industry-specific tools', 'Project work', 'Interview readiness'],
                ['Junior Engineer', 'Operations Trainee', 'Support Engineer']
            ),
            make_path(
                'Technical Support Roles',
                'This matches students who are comfortable applying concepts to real workflows and supporting business operations.',
                ['Basic technical knowledge', 'Troubleshooting', 'Documentation', 'Customer handling'],
                ['Automation', 'Process mapping', 'Communication'],
                ['Technical Support Intern', 'Operations Associate', 'Junior Analyst']
            ),
        ]

    selected_path = recommendations[0]
    roadmap = format_roadmap_steps(build_roadmap(selected_path['title'], branch, skill_key))

    return {
        'title': 'Career Guidance',
        'branch': branch or 'Not specified',
        'selected_path': selected_path['title'],
        'recommended_paths': recommendations,
        'roadmap': roadmap,
    }


def build_roadmap(path_title, branch='', skill_key=''):
    """Return a structured roadmap that changes by the selected career path."""
    title = (path_title or '').lower()

    if 'software' in title or 'developer' in title or 'frontend' in title or 'backend' in title:
        return [
            {'step': '1. Fundamentals', 'title': 'Core Concepts', 'detail': 'Strengthen programming logic, DSA, operating systems, and basic computer science fundamentals.'},
            {'step': '2. Core Skills', 'title': 'Language + Problem Solving', 'detail': 'Deepen Python/Java, SQL, OOP, arrays, strings, recursion, and debugging practice.'},
            {'step': '3. Tools & Technologies', 'title': 'Stack Exposure', 'detail': 'Learn Git, APIs, databases, and basic frontend/tools depending on the target role.'},
            {'step': '4. Projects', 'title': 'Build Real Work', 'detail': 'Create 2–3 relevant projects with a clear problem statement, UI, and backend logic.'},
            {'step': '5. Practice', 'title': 'Hands-on Execution', 'detail': 'Solve coding challenges, practice SQL queries, and improve debugging speed weekly.'},
            {'step': '6. Resume & Certifications', 'title': 'Portfolio Boost', 'detail': 'Document projects, certifications, and internship work clearly on your resume.'},
            {'step': '7. Interview Preparation', 'title': 'Technical & HR Readiness', 'detail': 'Prepare for coding interviews, aptitude, technical rounds, and communication-based questions.'},
            {'step': '8. Job Applications', 'title': 'Targeted Search', 'detail': 'Apply to junior developer roles, internships, and campus drives aligned to your stack.'},
        ]

    if 'security' in title or 'cyber' in title or 'soc' in title:
        return [
            {'step': '1. Fundamentals', 'title': 'Security Basics', 'detail': 'Learn networking, operating systems, security principles, and common attack vectors.'},
            {'step': '2. Core Skills', 'title': 'Threat Awareness', 'detail': 'Practice log analysis, risk understanding, system monitoring, and security best practices.'},
            {'step': '3. Tools & Technologies', 'title': 'Security Stack', 'detail': 'Explore Wireshark, Linux, firewall concepts, SIEM basics, and scripting for automation.'},
            {'step': '4. Projects', 'title': 'Hands-on Labs', 'detail': 'Work through small labs on malware analysis, network scanning, or threat detection exercises.'},
            {'step': '5. Practice', 'title': 'Analytical Drill', 'detail': 'Review logs, understand alerts, and practice incident-response workflows in simulated scenarios.'},
            {'step': '6. Resume & Certifications', 'title': 'Credibility', 'detail': 'Highlight labs, projects, and beginner security certifications such as Google Cybersecurity.'},
            {'step': '7. Interview Preparation', 'title': 'Technical Rounds', 'detail': 'Prepare for networking, security concepts, scenario-based Q&A, and general aptitude.'},
            {'step': '8. Job Applications', 'title': 'Targeted Search', 'detail': 'Apply to SOC, security analyst, and junior cybersecurity support roles.'},
        ]

    if 'embedded' in title or 'electronics' in title or 'iot' in title:
        return [
            {'step': '1. Fundamentals', 'title': 'Core Electronics', 'detail': 'Reinforce analog/digital electronics, circuits, signals, and microcontroller basics.'},
            {'step': '2. Core Skills', 'title': 'Embedded Programming', 'detail': 'Practice C/C++, GPIO, interrupts, sensors, and basic embedded debugging.'},
            {'step': '3. Tools & Technologies', 'title': 'Hardware Setup', 'detail': 'Learn microcontrollers, simulators, oscilloscopes, and basic PCB/IoT tools.'},
            {'step': '4. Projects', 'title': 'Build Hardware Projects', 'detail': 'Create smart sensor, automation, or embedded-control projects with clear outcomes.'},
            {'step': '5. Practice', 'title': 'Testing and Debugging', 'detail': 'Work on hardware-software integration, troubleshooting, and repeated prototype cycles.'},
            {'step': '6. Resume & Certifications', 'title': 'Technical Portfolio', 'detail': 'Add circuit design projects, hardware demos, and any related coursework or labs.'},
            {'step': '7. Interview Preparation', 'title': 'Domain Readiness', 'detail': 'Prepare for electronics reasoning, circuit-based questions, and practical technical discussion.'},
            {'step': '8. Job Applications', 'title': 'Targeted Search', 'detail': 'Apply to embedded, electronics, and IoT-based internship and graduate roles.'},
        ]

    if 'electrical' in title or 'power' in title:
        return [
            {'step': '1. Fundamentals', 'title': 'Electrical Core', 'detail': 'Refresh circuits, power systems, machines, protection systems, and safety concepts.'},
            {'step': '2. Core Skills', 'title': 'Technical Analysis', 'detail': 'Practice electrical calculations, load analysis, and troubleshooting scenarios.'},
            {'step': '3. Tools & Technologies', 'title': 'System Tools', 'detail': 'Learn relevant simulation software, maintenance processes, and wiring diagrams.'},
            {'step': '4. Projects', 'title': 'Practical Work', 'detail': 'Create mini power-system, automation, or troubleshooting projects with documentation.'},
            {'step': '5. Practice', 'title': 'Hands-on Learning', 'detail': 'Improve speed in solving electrical problems, reading schematics, and debugging faults.'},
            {'step': '6. Resume & Certifications', 'title': 'Portfolio Growth', 'detail': 'Add project reports, internships, and relevant technical coursework to your resume.'},
            {'step': '7. Interview Preparation', 'title': 'Technical Rounds', 'detail': 'Prepare for technical aptitude, machine concepts, and workplace safety interview questions.'},
            {'step': '8. Job Applications', 'title': 'Opportunity Search', 'detail': 'Apply to electrical, maintenance, and operations roles suited to your background.'},
        ]

    if 'design' in title or 'manufacturing' in title or 'project' in title or 'civil' in title:
        return [
            {'step': '1. Fundamentals', 'title': 'Branch Core', 'detail': 'Reinforce engineering principles, design logic, project flow, and quality standards.'},
            {'step': '2. Core Skills', 'title': 'Process & Planning', 'detail': 'Revise drafting, estimation, materials handling, and design/project decision-making.'},
            {'step': '3. Tools & Technologies', 'title': 'Workflow Tools', 'detail': 'Practice CAD, design tools, planning methods, and relevant project software.'},
            {'step': '4. Projects', 'title': 'Project Exposure', 'detail': 'Build portfolio work around design, site planning, manufacturing flow, or technical documentation.'},
            {'step': '5. Practice', 'title': 'Execution Ready', 'detail': 'Improve hands-on design, process understanding, and real-world technical reasoning.'},
            {'step': '6. Resume & Certifications', 'title': 'Portfolio Build', 'detail': 'Add project reports, internships, and measurable outcomes to highlight your work.'},
            {'step': '7. Interview Preparation', 'title': 'Technical+HR', 'detail': 'Practice domain questions, case-based discussion, and communication for project roles.'},
            {'step': '8. Job Applications', 'title': 'Opportunity Search', 'detail': 'Apply to entry-level design, manufacturing, site, or project engineering roles.'},
        ]

    return [
        {'step': '1. Fundamentals', 'title': 'Core Foundations', 'detail': 'Strengthen your technical basics, branch concepts, and problem-solving ability.'},
        {'step': '2. Core Skills', 'title': 'Skill Development', 'detail': 'Improve practical knowledge, communication, and role-specific technical competency.'},
        {'step': '3. Tools & Technologies', 'title': 'Industry Exposure', 'detail': 'Learn the common tools, workflows, and systems used in your target role.'},
        {'step': '4. Projects', 'title': 'Portfolio Work', 'detail': 'Build projects that reflect the work expected in your chosen career path.'},
        {'step': '5. Practice', 'title': 'Skill Refinement', 'detail': 'Solve problems, practice interviews, and sharpen technical consistency.'},
        {'step': '6. Resume & Certifications', 'title': 'Positioning', 'detail': 'Prepare a clear resume with evidence of internships, projects, and certification work.'},
        {'step': '7. Interview Preparation', 'title': 'Readiness', 'detail': 'Practice technical, HR, and situational questions with confidence and clarity.'},
        {'step': '8. Job Applications', 'title': 'Apply Strategically', 'detail': 'Focus applications on roles that match your branch, skills, and internship/project exposure.'},
    ]


def format_roadmap_steps(steps):
    """Present every career roadmap in the portal's seven shared stages."""
    if len(steps) > 7:
        steps = steps[:5] + steps[-2:]
    stage_names = ['Fundamentals', 'Skills', 'Tools', 'Projects', 'Practice', 'Interview', 'Jobs']
    return [
        {**step, 'step': f"{index}. {stage_names[index - 1]}"}
        for index, step in enumerate(steps, start=1)
    ]


def get_selected_career_guidance(student, career_title):
    """Return guidance with a validated career selection for the roadmap page."""
    guidance = get_career_guidance(student)
    selected = next(
        (path for path in guidance['recommended_paths'] if path['title'] == career_title),
        guidance['recommended_paths'][0] if guidance['recommended_paths'] else None
    )
    if selected is None:
        return guidance
    guidance['selected_path'] = selected['title']
    guidance['selected_career'] = selected
    guidance['roadmap'] = format_roadmap_steps(build_roadmap(selected['title'], guidance['branch'], normalize_text(student.get('skills'))))
    return guidance


def normalize_text(value):
    """Normalize branch and job text for consistent matching."""
    if value is None:
        return ""
    value = str(value).lower()
    value = re.sub(r"[^a-z0-9\s+/&-]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def contains_keyword(text, keyword):
    """Match a word or phrase without treating short tokens as substrings."""
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


BRANCH_KEYWORDS = {
    "computer science": [
        "software developer", "software engineer", "software", "python", "java",
        "web developer", "frontend", "frontend developer", "backend", "full stack",
        "full-stack", "developer", "computer science", "cse", "information technology", "it"
    ],
    "cyber security": [
        "cybersecurity", "cyber security", "security analyst", "soc analyst",
        "information security", "security engineer", "security", "ethical hacker",
        "network security", "penetration tester"
    ],
    "electronics": [
        "electronics", "embedded", "embedded systems", "vlsi", "hardware",
        "electronic engineer", "electronics engineer", "circuit design", "pcb"
    ],
    "electrical": [
        "electrical", "electrical engineering", "power systems", "electrical engineer",
        "electrical design", "power electronics"
    ],
    "mechanical": [
        "mechanical", "mechanical engineer", "manufacturing", "production",
        "cad", "design engineer", "industrial engineering"
    ],
    "civil": [
        "civil", "civil engineering", "construction", "structural", "site engineer",
        "quantity surveyor", "project engineer"
    ],
}


def get_branch_keywords(branch_name):
    """Return the relevant keyword list for the student branch."""
    normalized = normalize_text(branch_name or "")
    if not normalized:
        return []

    if any(contains_keyword(normalized, token) for token in ["computer science", "cse", "computer science and engineering", "information technology", "it"]):
        return BRANCH_KEYWORDS["computer science"]
    if any(contains_keyword(normalized, token) for token in ["cyber", "security"]):
        return BRANCH_KEYWORDS["cyber security"]
    if any(contains_keyword(normalized, token) for token in ["ece", "electronics", "electronic"]):
        return BRANCH_KEYWORDS["electronics"]
    if any(contains_keyword(normalized, token) for token in ["eee", "electrical", "electrical and electronics"]):
        return BRANCH_KEYWORDS["electrical"]
    if contains_keyword(normalized, "mechanical"):
        return BRANCH_KEYWORDS["mechanical"]
    if contains_keyword(normalized, "civil"):
        return BRANCH_KEYWORDS["civil"]
    return []


def placement_matches_branch(placement_row, branch_name):
    """Check if a placement is relevant to the student's branch."""
    keywords = get_branch_keywords(branch_name)
    if not keywords:
        return True

    text = normalize_text(" ".join([
        placement_row.get("job_role") or "",
        placement_row.get("skills") or "",
        placement_row.get("eligibility") or "",
        placement_row.get("company_name") or "",
        placement_row.get("location") or "",
        placement_row.get("venue") or "",
        placement_row.get("job_description") or "",
        placement_row.get("description") or ""
    ]))
    return any(contains_keyword(text, keyword) for keyword in keywords)


def get_branch_relevant_placements(student_id):
    """Return placements relevant to the logged-in student's branch only."""
    student = get_student_by_id(student_id)
    if student is None:
        return []

    branch_name = (student.get("branch") or "").strip()

    return [job for job in get_live_jobs_from_db() if placement_matches_branch(job, branch_name)]


SKILL_ALIASES = {
    'html': 'html', 'html5': 'html', 'css': 'css', 'css3': 'css',
    'javascript': 'javascript', 'js': 'javascript', 'typescript': 'typescript',
    'python': 'python', 'java': 'java', 'c++': 'c++', 'cpp': 'c++',
    'c#': 'c#', 'csharp': 'c#', 'sql': 'sql', 'mysql': 'sql',
    'postgresql': 'sql', 'flask': 'flask', 'django': 'django',
    'react': 'react', 'angular': 'angular', 'node': 'node.js', 'node.js': 'node.js',
    'git': 'git', 'github': 'git', 'linux': 'linux', 'aws': 'aws',
    'azure': 'azure', 'docker': 'docker', 'kubernetes': 'kubernetes',
    'rest': 'rest api', 'api': 'rest api', 'apis': 'rest api',
    'communication': 'communication', 'problem solving': 'problem solving',
    'data structures': 'data structures', 'algorithms': 'algorithms',
}


def extract_skill_tokens(value):
    """Extract comparable skills from student and job text using known aliases."""
    normalized = normalize_text(value)
    return {canonical for phrase, canonical in SKILL_ALIASES.items() if phrase in normalized}


def get_job_match(student, job):
    """Calculate a transparent match score from actual profile and job fields."""
    student_skills = extract_skill_tokens(student.get('skills'))
    job_text = ' '.join([
        job.get('job_role') or '', job.get('skills') or '',
        job.get('eligibility') or '', job.get('job_description') or ''
    ])
    required_skills = extract_skill_tokens(job_text)
    matched_skills = sorted(student_skills & required_skills)
    missing_skills = sorted(required_skills - student_skills)
    branch_keywords = get_branch_keywords(student.get('branch'))
    branch_match = not branch_keywords or any(keyword in normalize_text(job_text) for keyword in branch_keywords)
    graduation_year = str(student.get('graduation_year') or '').strip()
    graduation_match = bool(graduation_year and graduation_year in job_text)
    skill_score = (len(matched_skills) / len(required_skills) * 70) if required_skills else 0
    score = round(skill_score + (20 if branch_match else 0) + (10 if graduation_match else 0))
    if matched_skills:
        reason = f"Matches your {', '.join(matched_skills[:3])} skills"
    elif branch_match:
        reason = f"Aligned with your {student.get('branch') or 'academic'} background"
    else:
        reason = "Build the listed skills to improve your fit"
    return {
        **job,
        'match_percentage': min(score, 100),
        'matched_skills': matched_skills,
        'missing_skills': missing_skills[:4],
        'match_reason': reason,
    }


def get_recommended_jobs(student_id, limit=6):
    """Return live jobs ranked by the deterministic profile match score."""
    student = get_student_by_id(student_id)
    if student is None:
        return []
    jobs = [get_job_match(student, job) for job in get_live_jobs_from_db()]
    jobs.sort(key=lambda job: (job['match_percentage'], job.get('drive_date') or ''), reverse=True)
    return jobs[:limit]


def get_career_recommendations(student):
    """Enrich existing career guidance with profile-based scores and technologies."""
    guidance = get_career_guidance(student)
    student_skills = extract_skill_tokens(student.get('skills'))
    for path in guidance['recommended_paths']:
        required = set()
        for skill in path['required_skills']:
            required.update(extract_skill_tokens(skill))
        overlap = len(required & student_skills)
        score = round((overlap / len(required)) * 100) if required else 0
        path['match_score'] = min(score + (20 if get_branch_keywords(student.get('branch')) else 0), 100)
        path['technologies'] = path['required_skills'][:3]
        path['projects'] = [
            f"Build a portfolio project for {path['title']}",
            'Document the project with a README, screenshots, and measurable outcomes',
        ]
        path['resources'] = [
            'Official documentation and guided practice',
            'Role-specific interview question practice',
        ]
        path['certifications'] = ['Beginner certification aligned to the selected technology stack']
    return guidance


INTERVIEW_TOPICS = [
    {'category': 'Technical', 'title': 'Explain a project you built', 'detail': 'Describe the problem, your technical choices, your contribution, and the measurable result.'},
    {'category': 'Technical', 'title': 'How do you debug a difficult issue?', 'detail': 'Explain how you reproduce, isolate, test, and document a problem before applying a fix.'},
    {'category': 'HR', 'title': 'Tell me about yourself', 'detail': 'Use a concise present-past-future structure focused on your branch, skills, and target role.'},
    {'category': 'Behavioral', 'title': 'Describe a team challenge', 'detail': 'Use the STAR method: situation, task, action, and result.'},
]


ASSESSMENT_CATEGORIES = [
    {'title': 'Aptitude', 'icon': '01', 'detail': 'Percentages, ratios, time and work, probability, and data interpretation.', 'tracker_key': 'aptitude'},
    {'title': 'Logical reasoning', 'icon': '02', 'detail': 'Sequences, syllogisms, arrangements, coding-decoding, and analytical puzzles.', 'tracker_key': 'technical'},
    {'title': 'Verbal ability', 'icon': '03', 'detail': 'Reading comprehension, grammar, vocabulary, sentence correction, and communication.', 'tracker_key': 'communication'},
    {'title': 'Coding practice', 'icon': '04', 'detail': 'Programming fundamentals, data structures, algorithms, SQL, and timed problem solving.', 'tracker_key': 'programming'},
]


def get_student_applications(student_id):
    """Return only the applications belonging to the current student."""
    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT a.id, a.student_id, a.placement_id, a.applied_date, a.status,
                   a.application_url, a.resume_path,
                   c.company_name, p.job_role, p.location, p.salary
            FROM applications a
            JOIN placements p ON p.id = a.placement_id
            JOIN companies c ON c.id = p.company_id
            WHERE a.student_id = ?
            ORDER BY a.applied_date DESC, a.id DESC
            """,
            (student_id,)
        ).fetchall()

    apps = []
    for row in rows:
        item = dict(row)
        item['application_url'] = normalize_application_url(item.get('application_url'))
        item['resume_url'] = ''
        if item.get('resume_path'):
            filename = os.path.basename(item['resume_path'])
            if filename:
                item['resume_url'] = url_for('serve_resume', filename=filename)
        apps.append(item)
    return apps


def get_all_placements():
    """Return all placement records without branch filtering."""
    return get_live_jobs_from_db()


def get_placement_details(placement_id):
    """Load placement and company data for application validation."""
    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT p.id, p.job_role, p.location, p.eligibility, p.skills, p.salary,
                   p.drive_date, p.drive_time, p.venue,
                   c.company_name,
                   e.application_url AS application_url
            FROM placements p
            JOIN companies c ON c.id = p.company_id
                        LEFT JOIN external_jobs e
                            ON e.company_name = c.company_name
                         AND e.job_title = p.job_role
                         AND COALESCE(e.location, '') = COALESCE(p.location, '')
                         AND COALESCE(e.posted_date, '') = COALESCE(p.drive_date, '')
            WHERE p.id = ?
            ORDER BY e.updated_at DESC
            LIMIT 1
            """,
            (placement_id,)
        ).fetchone()

    if row is None:
        return None
    return dict(row)


def get_no_store_response(response):
    """Prevent back-button access to protected pages after logout."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def render_student_page(template_page, student, **context):
    """Render a protected page with consistent defaults for the shared shell."""
    defaults = {
        'page': template_page,
        'student': student,
        'placement_jobs': [],
        'applications': [],
        'available_placements': 0,
        'total_applications': 0,
        'shortlisted': 0,
        'upcoming_drives': 0,
        'last_updated': None,
        'live_jobs_error': None,
        'profile_message': None,
        'page_message': None,
    }
    defaults.update(context)
    return get_no_store_response(make_response(render_template('index.html', **defaults)))


@app.after_request
def clear_cache_for_logged_in_student(response):
    """Ensure protected pages are not cached and cannot be reopened after logout."""
    if "student_id" in session:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---------------- LOGIN / ROOT / STUDENT DASHBOARD ----------------

@app.route('/')
def root():
    if "student_id" in session:
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/dashboard')
def index():
    if "student_id" not in session:
        return redirect(url_for('login'))

    student_id = session["student_id"]
    student = get_student_by_id(student_id)
    if student is None:
        session.clear()
        return redirect(url_for('login'))

    if should_refresh_live_jobs():
        live_jobs_response = fetch_live_jobs_from_adzuna(limit=20)
        if live_jobs_response.get("jobs"):
            sync_live_jobs(live_jobs_response["jobs"])

    placement_jobs = get_branch_relevant_placements(student_id)
    last_sync = None

    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        row = connection.execute(
            "SELECT value FROM job_sync_state WHERE key = 'adzuna_last_sync'"
        ).fetchone()
        if row:
            try:
                last_sync = row[0]
            except Exception:
                last_sync = None

    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        total_applications = connection.execute(
            "SELECT COUNT(*) FROM applications WHERE student_id = ?",
            (student_id,)
        ).fetchone()[0]

        shortlisted = connection.execute(
            """
            SELECT COUNT(*)
            FROM applications
            WHERE student_id = ? AND status = 'Shortlisted'
            """,
            (student_id,)
        ).fetchone()[0]

        total_companies = connection.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]

    recent_applications = get_student_applications(student_id)[:3]
    recommended_jobs = get_recommended_jobs(student_id)
    preparation_summary = get_preparation_summary(get_student_preparation(student_id))

    response = make_response(render_template(
        'index.html',
        page='dashboard',
        available_placements=len(placement_jobs),
        total_applications=total_applications,
        shortlisted=shortlisted,
        upcoming_drives=len(placement_jobs),
        total_companies=total_companies,
        placement_jobs=placement_jobs,
        recommended_jobs=recommended_jobs,
        recent_applications=recent_applications,
        preparation_summary=preparation_summary,
        last_updated=last_sync,
        live_jobs_error=None,
        student=student,
        profile_message=None,
        applications=[],
        page_message=None,
        career_guidance=get_career_recommendations(student)
    ))
    return get_no_store_response(response)


@app.route('/placements')
def placements_page():
    if "student_id" not in session:
        return redirect(url_for('login'))

    student_id = session["student_id"]
    student = get_student_by_id(student_id)
    if student is None:
        session.clear()
        return redirect(url_for('login'))

    if should_refresh_live_jobs():
        live_jobs_response = fetch_live_jobs_from_adzuna(limit=20)
        if live_jobs_response.get("jobs"):
            sync_live_jobs(live_jobs_response["jobs"])

    show_all = request.args.get("show_all") == "1"
    raw_jobs = get_all_placements() if show_all else get_branch_relevant_placements(student_id)
    placement_jobs = [get_job_match(student, job) for job in raw_jobs]
    query = normalize_text(request.args.get('q', ''))
    if query:
        placement_jobs = [job for job in placement_jobs if query in normalize_text(' '.join([
            job.get('job_role') or '', job.get('company_name') or '', job.get('location') or '', job.get('skills') or ''
        ]))]
    sort_by = request.args.get('sort', 'match')
    if sort_by == 'date':
        placement_jobs.sort(key=lambda job: job.get('drive_date') or '', reverse=True)
    elif sort_by == 'title':
        placement_jobs.sort(key=lambda job: job.get('job_role') or '')
    else:
        placement_jobs.sort(key=lambda job: job.get('match_percentage', 0), reverse=True)
    message = request.args.get("message")
    response = make_response(render_template(
        'index.html',
        page='placements',
        student=student,
        placement_jobs=placement_jobs,
        applications=[],
        available_placements=len(placement_jobs),
        total_applications=0,
        shortlisted=0,
        upcoming_drives=len(placement_jobs),
        last_updated=None,
        live_jobs_error=None,
        profile_message=None,
        page_message=message,
        show_all=show_all
    ))
    return get_no_store_response(response)


@app.route('/refresh-jobs', methods=['POST'])
def refresh_jobs():
    if "student_id" not in session:
        return redirect(url_for('login'))

    live_jobs_response = fetch_live_jobs_from_adzuna(limit=20)
    if live_jobs_response.get('error'):
        return redirect(url_for('placements_page', message=live_jobs_response['error']))

    sync_live_jobs(live_jobs_response['jobs'])
    return redirect(url_for('placements_page', message=f"Jobs refreshed from Adzuna: {len(live_jobs_response['jobs'])} opportunities."))


@app.route('/applications')
def applications_page():
    if "student_id" not in session:
        return redirect(url_for('login'))

    student_id = session["student_id"]
    student = get_student_by_id(student_id)
    if student is None:
        session.clear()
        return redirect(url_for('login'))

    applications = get_student_applications(student_id)
    message = request.args.get("message")
    response = make_response(render_template(
        'index.html',
        page='applications',
        student=student,
        placement_jobs=[],
        applications=applications,
        available_placements=0,
        total_applications=len(applications),
        shortlisted=sum(1 for app in applications if app.get("status") == "Shortlisted"),
        upcoming_drives=0,
        last_updated=None,
        live_jobs_error=None,
        profile_message=None,
        page_message=message
    ))
    return get_no_store_response(response)


@app.route('/profile')
def profile():
    if "student_id" not in session:
        return redirect(url_for('login'))

    student_id = session["student_id"]
    student = get_student_by_id(student_id)
    if student is None:
        session.clear()
        return redirect(url_for('login'))

    response = make_response(render_template(
        'index.html',
        page='profile',
        student=student,
        placement_jobs=[],
        applications=[],
        available_placements=0,
        total_applications=0,
        shortlisted=0,
        upcoming_drives=0,
        last_updated=None,
        live_jobs_error=None,
        profile_message=None,
        page_message=None
    ))
    return get_no_store_response(response)


@app.route('/resume/<path:filename>')
def serve_resume(filename):
    if "student_id" not in session:
        return redirect(url_for('login'))

    student_id = session["student_id"]
    upload_root = UPLOAD_DIR
    safe_dir = os.path.abspath(upload_root)
    candidate = os.path.abspath(os.path.join(upload_root, filename))

    if os.path.commonpath([safe_dir, candidate]) != safe_dir:
        return "Access denied.", 403

    if not os.path.exists(candidate):
        return render_template('index.html', page='applications', student=get_student_by_id(student_id), placement_jobs=[], applications=get_student_applications(student_id), available_placements=0, total_applications=0, shortlisted=0, upcoming_drives=0, last_updated=None, live_jobs_error=None, profile_message=None, page_message='The requested resume file was not found.')

    return send_from_directory(upload_root, filename, as_attachment=False)


@app.route('/career-guidance')
def career_guidance_page():
    if "student_id" not in session:
        return redirect(url_for('login'))

    student_id = session["student_id"]
    student = get_student_by_id(student_id)
    if student is None:
        session.clear()
        return redirect(url_for('login'))

    guidance = get_career_recommendations(student)
    response = make_response(render_template(
        'index.html',
        page='career_guidance',
        student=student,
        placement_jobs=[],
        applications=[],
        available_placements=0,
        total_applications=0,
        shortlisted=0,
        upcoming_drives=0,
        last_updated=None,
        live_jobs_error=None,
        profile_message=None,
        page_message=None,
        career_guidance=guidance,
        tracker=get_student_preparation(student_id)
    ))
    return get_no_store_response(response)


@app.route('/roadmap')
def roadmap_page():
    if "student_id" not in session:
        return redirect(url_for('login'))

    student = get_student_by_id(session['student_id'])
    if student is None:
        session.clear()
        return redirect(url_for('login'))

    guidance = get_selected_career_guidance(student, request.args.get('career', ''))
    return render_student_page('roadmap', student, career_guidance=guidance)


@app.route('/interview-preparation')
def interview_preparation_page():
    if "student_id" not in session:
        return redirect(url_for('login'))
    student = get_student_by_id(session['student_id'])
    if student is None:
        session.clear()
        return redirect(url_for('login'))
    response = make_response(render_template(
        'interview_preparation.html',
        student=student,
        interview_topics=INTERVIEW_TOPICS,
        tracker=get_student_preparation(session['student_id'])
    ))
    return get_no_store_response(response)


@app.route('/assessment-preparation')
def assessment_preparation_page():
    if "student_id" not in session:
        return redirect(url_for('login'))
    student = get_student_by_id(session['student_id'])
    if student is None:
        session.clear()
        return redirect(url_for('login'))
    response = make_response(render_template(
        'assessment_preparation.html',
        student=student,
        assessment_categories=ASSESSMENT_CATEGORIES,
        tracker=get_student_preparation(session['student_id'])
    ))
    return get_no_store_response(response)


@app.route('/preparation-tracker', methods=['GET', 'POST'])
def preparation_tracker():
    if "student_id" not in session:
        return redirect(url_for('login'))

    student_id = session["student_id"]
    student = get_student_by_id(student_id)
    if student is None:
        session.clear()
        return redirect(url_for('login'))

    if request.method == 'POST':
        data = {
            'aptitude': request.form.get('aptitude', 0),
            'programming': request.form.get('programming', 0),
            'technical': request.form.get('technical', 0),
            'communication': request.form.get('communication', 0),
            'interview': request.form.get('interview', 0),
            'company_specific': request.form.get('company_specific', 0),
        }
        tracker = save_student_preparation(student_id, data)
    else:
        tracker = get_student_preparation(student_id)

    response = make_response(render_template(
        'preparation_tracker.html',
        student=student,
        tracker=tracker
    ))
    return get_no_store_response(response)


@app.route('/apply', methods=['GET', 'POST'])
def apply_to_placement():
    if "student_id" not in session:
        return redirect(url_for('login'))

    student_id = session["student_id"]
    student = get_student_by_id(student_id)
    if student is None:
        session.clear()
        return redirect(url_for('login'))

    placement_id = request.form.get("placement_id") or request.args.get("placement_id")
    if not placement_id:
        return redirect(url_for('placements_page', message='Invalid placement selected.'))

    try:
        placement_id = int(placement_id)
    except (TypeError, ValueError):
        return redirect(url_for('placements_page', message='Invalid placement selected.'))

    placement = get_placement_details(placement_id)
    if placement is None:
        return redirect(url_for('placements_page', message='Selected placement was not found.'))

    if request.method == 'GET':
        return make_response(render_template(
            'index.html',
            page='application_form',
            student=student,
            placement=placement,
            placement_jobs=[],
            applications=[],
            available_placements=0,
            total_applications=0,
            shortlisted=0,
            upcoming_drives=0,
            last_updated=None,
            live_jobs_error=None,
            profile_message=None,
            page_message=None,
            form_error=None,
            form_data={
                'full_name': student.get('full_name') or '',
                'email': student.get('email') or '',
                'phone': student.get('phone') or '',
                'college': '',
                'branch': student.get('branch') or '',
                'graduation_year': student.get('graduation_year') or '',
                'skills': student.get('skills') or '',
                'gender': ''
            }
        ))

    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        existing = connection.execute(
            "SELECT id FROM applications WHERE student_id = ? AND placement_id = ?",
            (student_id, placement_id)
        ).fetchone()

        if existing:
            return redirect(url_for('applications_page', message='You have already applied for this placement.'))

    full_name = (request.form.get('full_name') or '').strip()
    email = (request.form.get('email') or '').strip()
    phone = (request.form.get('phone') or '').strip()
    gender = (request.form.get('gender') or '').strip()
    college = (request.form.get('college') or '').strip()
    branch = (request.form.get('branch') or '').strip()
    graduation_year = (request.form.get('graduation_year') or '').strip()
    skills = (request.form.get('skills') or '').strip()
    resume_file = request.files.get('resume')

    if not full_name or not email or not phone or not college or not branch or not graduation_year or not skills:
        return make_response(render_template(
            'index.html',
            page='application_form',
            student=student,
            placement=placement,
            placement_jobs=[],
            applications=[],
            available_placements=0,
            total_applications=0,
            shortlisted=0,
            upcoming_drives=0,
            last_updated=None,
            live_jobs_error=None,
            profile_message=None,
            page_message=None,
            form_error='Please complete all required fields before submitting your application.',
            form_data={
                'full_name': full_name,
                'email': email,
                'phone': phone,
                'college': college,
                'branch': branch,
                'graduation_year': graduation_year,
                'skills': skills,
                'gender': gender
            }
        ))

    try:
        graduation_year = int(graduation_year)
    except ValueError:
        return make_response(render_template(
            'index.html',
            page='application_form',
            student=student,
            placement=placement,
            placement_jobs=[],
            applications=[],
            available_placements=0,
            total_applications=0,
            shortlisted=0,
            upcoming_drives=0,
            last_updated=None,
            live_jobs_error=None,
            profile_message=None,
            page_message=None,
            form_error='Please enter a valid graduation year.',
            form_data={
                'full_name': full_name,
                'email': email,
                'phone': phone,
                'college': college,
                'branch': branch,
                'graduation_year': graduation_year,
                'skills': skills,
                'gender': gender
            }
        ))

    if resume_file is None or resume_file.filename == '':
        return make_response(render_template(
            'index.html',
            page='application_form',
            student=student,
            placement=placement,
            placement_jobs=[],
            applications=[],
            available_placements=0,
            total_applications=0,
            shortlisted=0,
            upcoming_drives=0,
            last_updated=None,
            live_jobs_error=None,
            profile_message=None,
            page_message=None,
            form_error='Please upload your resume in PDF, DOC, or DOCX format.',
            form_data={
                'full_name': full_name,
                'email': email,
                'phone': phone,
                'college': college,
                'branch': branch,
                'graduation_year': graduation_year,
                'skills': skills,
                'gender': gender
            }
        ))

    filename = secure_filename(resume_file.filename)
    ext = os.path.splitext(filename)[1].lower()
    allowed_exts = {'.pdf', '.doc', '.docx'}
    if ext not in allowed_exts:
        return make_response(render_template(
            'index.html',
            page='application_form',
            student=student,
            placement=placement,
            placement_jobs=[],
            applications=[],
            available_placements=0,
            total_applications=0,
            shortlisted=0,
            upcoming_drives=0,
            last_updated=None,
            live_jobs_error=None,
            profile_message=None,
            page_message=None,
            form_error='Unsupported resume format. Please upload PDF, DOC, or DOCX only.',
            form_data={
                'full_name': full_name,
                'email': email,
                'phone': phone,
                'college': college,
                'branch': branch,
                'graduation_year': graduation_year,
                'skills': skills,
                'gender': gender
            }
        ))

    resume_file.seek(0, os.SEEK_END)
    file_size = resume_file.tell()
    resume_file.seek(0)
    if file_size <= 0 or file_size > 2 * 1024 * 1024:
        return make_response(render_template(
            'index.html',
            page='application_form',
            student=student,
            placement=placement,
            placement_jobs=[],
            applications=[],
            available_placements=0,
            total_applications=0,
            shortlisted=0,
            upcoming_drives=0,
            last_updated=None,
            live_jobs_error=None,
            profile_message=None,
            page_message=None,
            form_error='Resume size must be between 1 byte and 2 MB.',
            form_data={
                'full_name': full_name,
                'email': email,
                'phone': phone,
                'college': college,
                'branch': branch,
                'graduation_year': graduation_year,
                'skills': skills,
                'gender': gender
            }
        ))

    upload_dir = UPLOAD_DIR
    os.makedirs(upload_dir, exist_ok=True)
    unique_name = f"student_{student_id}_placement_{placement_id}_{uuid.uuid4().hex}_{secure_filename(filename)}"
    resume_path = os.path.join(upload_dir, unique_name)
    resume_file.save(resume_path)
    relative_resume_path = os.path.join('uploads', 'resumes', unique_name).replace('\\', '/')

    application_url = normalize_application_url(placement.get('application_url'))

    with sqlite3.connect(DB_NAME, timeout=10) as connection:
        connection.execute(
            """
            INSERT INTO applications (
                student_id, placement_id, applied_date, status, resume_path,
                application_url, college, gender
            ) VALUES (?, ?, ?, 'Applied', ?, ?, ?, ?)
            """,
            (student_id, placement_id, datetime.now(timezone.utc).date().isoformat(), relative_resume_path, application_url, college, gender or None)
        )
        connection.commit()

    return redirect(url_for('applications_page', message='Application submitted successfully.'))


# ---------------- LOGIN ----------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if "student_id" in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()

        if not email or not password:
            return render_template(
                'login.html',
                message='Please enter email and password.'
            )

        with sqlite3.connect(DB_NAME, timeout=10) as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, full_name, email, password
                FROM students
                WHERE email = ? AND password = ?
                """,
                (email, password)
            )
            student = cursor.fetchone()

        if student:
            session['student_id'] = student[0]
            session['student_name'] = student[1]
            session['student_email'] = student[2]
            return redirect(url_for('index'))

        return render_template(
            'login.html',
            message='Invalid email or password.'
        )

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        phone = request.form.get('phone', '').strip()
        branch = request.form.get('branch', '').strip()
        graduation_year = request.form.get('graduation_year', '').strip()
        skills = request.form.get('skills', '').strip()

        if not full_name or not email or not password:
            return render_template(
                'register.html',
                message='Full name, email and password are required.'
            )

        try:
            with sqlite3.connect(DB_NAME, timeout=10) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """
                    INSERT INTO students (full_name, email, password, phone, branch, graduation_year, skills)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (full_name, email, password, phone, branch, int(graduation_year) if graduation_year else None, skills)
                )
                connection.commit()

                student = cursor.execute(
                    "SELECT id, full_name, email FROM students WHERE email = ?",
                    (email,)
                ).fetchone()
        except sqlite3.IntegrityError:
            return render_template(
                'register.html',
                message='This email is already registered. Please login instead.'
            )
        except ValueError:
            return render_template(
                'register.html',
                message='Please enter a valid graduation year.'
            )

        if student:
            session['student_id'] = student[0]
            session['student_name'] = student[1]
            session['student_email'] = student[2]
            return redirect(url_for('career_guidance_page'))

        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    response = redirect(url_for('login'))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route('/api/live-jobs')
def api_live_jobs():
    if "student_id" not in session:
        return jsonify({"success": False, "error": "Authentication required.", "jobs": []}), 401
    jobs = get_live_jobs_from_db()
    if jobs:
        return jsonify({"success": True, "jobs": jobs})

    live_jobs_response = fetch_live_jobs_from_adzuna(limit=20)
    if live_jobs_response.get("error"):
        return jsonify({"success": False, "error": live_jobs_response["error"], "jobs": []})

    synced_count = sync_live_jobs(live_jobs_response["jobs"])
    jobs = get_live_jobs_from_db()
    return jsonify({"success": True, "jobs": jobs, "synced_jobs": synced_count})


@app.route('/api/sync-jobs')
def sync_jobs():
    if "student_id" not in session:
        return jsonify({"success": False, "error": "Authentication required.", "synced_jobs": 0}), 401
    live_jobs_response = fetch_live_jobs_from_adzuna(limit=20)
    if live_jobs_response.get("error"):
        return jsonify({"success": False, "error": live_jobs_response["error"], "synced_jobs": 0})

    synced_count = sync_live_jobs(live_jobs_response["jobs"])
    return jsonify({"success": True, "synced_jobs": synced_count, "jobs": live_jobs_response["jobs"]})


# ---------------- START FLASK ----------------

if __name__ == '__main__':
    app.run(debug=True)