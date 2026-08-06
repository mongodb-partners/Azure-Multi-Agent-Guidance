"""Sample Career Assistant dataset for the `multi-agent` database.

Small but coherent so the demo prompts return meaningful results. Documents that get
semantically searched carry an `embed_text` hint describing which fields to embed; the
seeder computes the `embedding` field from that text.
"""
from __future__ import annotations

CANDIDATES = [
    {
        "candidate_id": "cand-001",
        "name": "Alex Rivera",
        "headline": "Backend engineer, 6 yrs, Python + MongoDB",
        "skills": ["python", "fastapi", "mongodb", "docker", "azure", "rest apis", "pytest"],
        "years_experience": 6,
        "location": "Austin, TX",
        "preferences": {"remote": True, "min_salary": 150000, "target_role": "Staff Engineer"},
        "summary": (
            "Senior backend engineer with 6 years building Python microservices on Azure, "
            "MongoDB data modeling, and CI/CD. Looking to grow into a staff/architect role."
        ),
    },
]

JOBS = [
    {
        "job_id": "job-001",
        "title": "Senior Python Backend Engineer",
        "company": "Nimbus Data",
        "location": "Remote (US)",
        "remote": True,
        "seniority": "senior",
        "skills": ["python", "fastapi", "mongodb", "azure", "kubernetes"],
        "description": "Build high-throughput APIs on Azure with MongoDB Atlas. Own services end-to-end.",
        "salary_min": 155000,
        "salary_max": 190000,
    },
    {
        "job_id": "job-002",
        "title": "Staff Software Engineer, Platform",
        "company": "Helio Robotics",
        "location": "Austin, TX",
        "remote": False,
        "seniority": "staff",
        "skills": ["python", "distributed systems", "mongodb", "leadership", "architecture"],
        "description": "Lead platform architecture for a robotics data platform. Mentor senior engineers.",
        "salary_min": 195000,
        "salary_max": 240000,
    },
    {
        "job_id": "job-003",
        "title": "Backend Engineer (Data)",
        "company": "Marlin Health",
        "location": "Remote (US)",
        "remote": True,
        "seniority": "mid",
        "skills": ["python", "postgres", "etl", "aws"],
        "description": "Own ingestion pipelines for healthcare data. Strong SQL and data modeling.",
        "salary_min": 130000,
        "salary_max": 160000,
    },
    {
        "job_id": "job-004",
        "title": "Cloud Solutions Architect",
        "company": "Nimbus Data",
        "location": "Remote (US)",
        "remote": True,
        "seniority": "staff",
        "skills": ["azure", "architecture", "mongodb", "python", "networking"],
        "description": "Design multi-tenant SaaS architectures on Azure with MongoDB Atlas.",
        "salary_min": 180000,
        "salary_max": 225000,
    },
]

COMPANIES = [
    {
        "company_id": "co-001",
        "name": "Nimbus Data",
        "industry": "Cloud infrastructure",
        "size": "201-500",
        "hq": "Remote-first (US)",
        "founded": 2016,
        "funding_stage": "Series C",
        "total_funding": "$180M",
        "tech_stack": ["python", "azure", "mongodb", "kubernetes", "react"],
        "culture": "Remote-first, async, strong documentation culture, generous learning budget.",
        "mission": "Make cloud data infrastructure effortless for every team.",
        "glassdoor_rating": 4.4,
        "summary": (
            "Nimbus Data is a remote-first Series C cloud infrastructure company using Azure and "
            "MongoDB. Known for async culture, deep documentation, and staff-level growth paths."
        ),
    },
    {
        "company_id": "co-002",
        "name": "Helio Robotics",
        "industry": "Robotics",
        "size": "51-200",
        "hq": "Austin, TX",
        "founded": 2019,
        "funding_stage": "Series B",
        "total_funding": "$95M",
        "tech_stack": ["python", "c++", "ros", "mongodb", "gcp"],
        "culture": "In-office, fast-paced, hardware+software collaboration, high ownership.",
        "mission": "Bring autonomous robotics to warehouse logistics.",
        "glassdoor_rating": 3.9,
        "summary": (
            "Helio Robotics is an Austin-based Series B robotics company. In-office, fast-paced, "
            "with high ownership and a mix of hardware and software engineering."
        ),
    },
    {
        "company_id": "co-003",
        "name": "Marlin Health",
        "industry": "Healthtech",
        "size": "201-500",
        "hq": "Boston, MA",
        "founded": 2014,
        "funding_stage": "Series D",
        "total_funding": "$260M",
        "tech_stack": ["python", "postgres", "aws", "react"],
        "culture": "Hybrid, mission-driven, strong work-life balance, regulated environment.",
        "mission": "Improve patient outcomes with better health data.",
        "glassdoor_rating": 4.1,
        "summary": (
            "Marlin Health is a Boston healthtech firm (Series D) with a hybrid, mission-driven "
            "culture and strong work-life balance in a regulated data environment."
        ),
    },
]

INTERVIEW_PREP = [
    {
        "role": "Staff Engineer",
        "question": "Walk me through how you'd design a multi-tenant data platform on MongoDB.",
        "category": "system design",
        "sample_answer": "Discuss tenancy model, sharding/indexing, isolation, and cost trade-offs.",
        "difficulty": "hard",
    },
    {
        "role": "Staff Engineer",
        "question": "How do you drive technical influence without direct authority?",
        "category": "leadership",
        "sample_answer": "Use RFCs, prototypes, and stakeholder alignment; measure impact.",
        "difficulty": "medium",
    },
    {
        "role": "Senior Backend Engineer",
        "question": "How would you diagnose and fix a slow MongoDB aggregation?",
        "category": "performance",
        "sample_answer": "explain(), index usage, $match early, covered queries, profiler.",
        "difficulty": "medium",
    },
]

CAREER_PATHS = [
    {
        "from_role": "Senior Backend Engineer",
        "to_role": "Staff Engineer",
        "typical_years": 3,
        "key_skills": ["system design", "technical leadership", "architecture", "mentoring", "cross-team influence"],
        "milestones": [
            "Lead a cross-team technical initiative end-to-end",
            "Author and drive adoption of an architecture RFC",
            "Mentor 2+ engineers to promotion",
        ],
    },
    {
        "from_role": "Data Analyst",
        "to_role": "Data Scientist",
        "typical_years": 2,
        "key_skills": ["python", "statistics", "machine learning", "experimentation", "sql"],
        "milestones": ["Ship a predictive model to production", "Own an A/B testing framework"],
    },
]

SKILLS_TAXONOMY = [
    {"skill": "python", "category": "language", "related": ["fastapi", "pytest", "asyncio"], "typical_roles": ["backend engineer", "data scientist"]},
    {"skill": "mongodb", "category": "database", "related": ["atlas vector search", "aggregation", "data modeling"], "typical_roles": ["backend engineer", "data engineer"]},
    {"skill": "system design", "category": "architecture", "related": ["scalability", "distributed systems", "trade-off analysis"], "typical_roles": ["staff engineer", "architect"]},
    {"skill": "technical leadership", "category": "leadership", "related": ["mentoring", "rfc", "cross-team influence"], "typical_roles": ["staff engineer", "eng manager"]},
]

COMPENSATION_DATA = [
    {"company": "Nimbus Data", "role": "Staff Engineer", "level": "L6", "base_min": 195000, "base_max": 230000, "equity": "0.05%-0.1%", "location": "Remote (US)"},
    {"company": "Nimbus Data", "role": "Senior Engineer", "level": "L5", "base_min": 155000, "base_max": 190000, "equity": "0.02%-0.05%", "location": "Remote (US)"},
    {"company": "Helio Robotics", "role": "Staff Engineer", "level": "Staff", "base_min": 195000, "base_max": 240000, "equity": "0.1%-0.2%", "location": "Austin, TX"},
]


# (collection_name, documents, embed_text_builder or None)
def _job_text(d: dict) -> str:
    return f"{d['title']} at {d['company']}. {d['description']} Skills: {', '.join(d['skills'])}."


def _company_text(d: dict) -> str:
    return f"{d['name']} — {d['industry']}. {d['summary']} Culture: {d['culture']} Tech: {', '.join(d['tech_stack'])}."


def _candidate_text(d: dict) -> str:
    return f"{d['headline']}. {d['summary']} Skills: {', '.join(d['skills'])}."


def _interview_text(d: dict) -> str:
    return f"[{d['role']} / {d['category']}] {d['question']}"


DATASET = [
    ("candidates", CANDIDATES, _candidate_text),
    ("jobs", JOBS, _job_text),
    ("companies", COMPANIES, _company_text),
    ("interview_prep", INTERVIEW_PREP, _interview_text),
    ("career_paths", CAREER_PATHS, None),
    ("skills_taxonomy", SKILLS_TAXONOMY, None),
    ("compensation_data", COMPENSATION_DATA, None),
]
