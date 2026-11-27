from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(100), unique=True, nullable=True, index=True)
    full_name = Column(String(200), nullable=False)
    phone_number = Column(String(50), nullable=True)
    password_hash = Column(String(500), nullable=False)
    role = Column(String(20), nullable=False, default="user", index=True)
    account_status = Column(String(20), nullable=False, default="active", index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    is_verified = Column(Boolean, nullable=False, default=True)
    must_change_password = Column(Boolean, nullable=False, default=False)
    additional_profile_info = Column(Text, nullable=True)
    created_by_admin_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    workspace_token = Column(String(80), unique=True, nullable=True, index=True)
    created_at = Column(DateTime, default=func.now())
    last_login = Column(DateTime, nullable=True)

    sessions = relationship(
        "UserSession",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    preferences = relationship(
        "UserSettings",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    profile = relationship(
        "UserProfile",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    events = relationship(
        "EventLog",
        back_populates="user",
        passive_deletes=True,
    )


class RegistrationRequest(Base):
    __tablename__ = "registration_requests"
    __table_args__ = (UniqueConstraint("email", name="uq_registration_request_email"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    full_name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    password_hash = Column(String(500), nullable=False)
    submitted_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    theme = Column(String(20), default="dark")
    default_role = Column(String(100), default="")
    default_difficulty = Column(String(20), default="medium")
    default_interview_type = Column(String(50), default="Full Interview")
    llm_provider = Column(String(40), default="auto")
    llm_model = Column(String(120), default="")
    voice_enabled = Column(Boolean, default=True)
    privacy_share_analytics = Column(Boolean, default=False)
    preferred_interview_categories = Column(JSON, default=list)
    learning_preferences = Column(JSON, default=dict)
    notification_preferences = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="preferences")


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    headline = Column(String(200), default="")
    objective = Column(Text, default="")
    skills = Column(JSON, default=list)
    technical_interests = Column(JSON, default=list)
    education = Column(JSON, default=list)
    work_experience = Column(JSON, default=list)
    career_interests = Column(JSON, default=list)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="profile")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), unique=True, nullable=False, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    label = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    last_active = Column(DateTime, default=func.now())
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="sessions")
    documents = relationship(
        "KnowledgeDocument", back_populates="session", cascade="all, delete-orphan"
    )
    roadmaps = relationship(
        "LearningRoadmap", back_populates="session", cascade="all, delete-orphan"
    )
    assessments = relationship("Assessment", back_populates="session", cascade="all, delete-orphan")
    reports = relationship(
        "GeneratedReport", back_populates="session", cascade="all, delete-orphan"
    )
    resume_analyses = relationship(
        "ResumeAnalysis", back_populates="session", cascade="all, delete-orphan"
    )
    job_descriptions = relationship(
        "JobDescription", back_populates="session", cascade="all, delete-orphan"
    )
    interview_sessions = relationship(
        "InterviewSession", back_populates="session", cascade="all, delete-orphan"
    )
    question_bookmarks = relationship(
        "QuestionBookmark", back_populates="session", cascade="all, delete-orphan"
    )
    practice_attempts = relationship(
        "QuestionPracticeAttempt", back_populates="session", cascade="all, delete-orphan"
    )
    learning_notes = relationship(
        "LearningNote", back_populates="session", cascade="all, delete-orphan"
    )


class QuestionBookmark(Base):
    __tablename__ = "question_bookmarks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    question_text = Column(Text, nullable=False)
    category = Column(String(50), default="technical")
    difficulty = Column(String(20), default="medium")
    expected_key_points = Column(JSON, default=list)
    source = Column(String(30), default="generated")
    created_at = Column(DateTime, default=func.now())

    session = relationship("UserSession", back_populates="question_bookmarks")


class QuestionPracticeAttempt(Base):
    __tablename__ = "question_practice_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source = Column(String(30), default="generated")
    source_id = Column(Integer, nullable=True)
    question_text = Column(Text, nullable=False)
    category = Column(String(50), default="technical")
    difficulty = Column(String(20), default="medium")
    expected_key_points = Column(JSON, default=list)
    answer_text = Column(Text, nullable=False)
    status = Column(String(20), default="evaluated")
    overall_score = Column(Float)
    accuracy = Column(Float)
    relevance = Column(Float)
    clarity = Column(Float)
    completeness = Column(Float)
    strengths = Column(JSON, default=list)
    missing_points = Column(JSON, default=list)
    improvements = Column(JSON, default=list)
    improved_answer = Column(Text, default="")
    final_feedback = Column(Text, default="")
    rubric_version = Column(String(30), default="question-practice-v1")
    submitted_at = Column(DateTime, default=func.now())
    evaluated_at = Column(DateTime, nullable=True)

    session = relationship("UserSession", back_populates="practice_attempts")


class LearningNote(Base):
    __tablename__ = "learning_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, default="")
    topic = Column(String(120), default="")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    session = relationship("UserSession", back_populates="learning_notes")


class ResumeAnalysis(Base):
    __tablename__ = "resume_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500))
    file_hash = Column(String(64))

    raw_text = Column(Text)
    candidate_name = Column(String(200))
    email = Column(String(200))
    phone = Column(String(50))
    linkedin = Column(String(200))
    github = Column(String(200))
    portfolio = Column(String(200))

    education = Column(JSON)
    experience = Column(JSON)
    skills = Column(JSON)
    programming_languages = Column(JSON)
    frameworks = Column(JSON)
    databases = Column(JSON)
    tools = Column(JSON)
    projects = Column(JSON)
    certifications = Column(JSON)
    strengths = Column(JSON)
    weaknesses = Column(JSON)

    technical_skills_score = Column(Float)
    experience_score = Column(Float)
    projects_score = Column(Float)
    education_score = Column(Float)
    certifications_score = Column(Float)
    structure_score = Column(Float)
    overall_resume_score = Column(Float)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    session = relationship("UserSession", back_populates="resume_analyses")


class JobDescription(Base):
    __tablename__ = "job_descriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200))
    company = Column(String(200))
    raw_text = Column(Text)
    source = Column(String(50))

    required_skills = Column(JSON)
    preferred_skills = Column(JSON)
    programming_languages = Column(JSON)
    frameworks = Column(JSON)
    databases = Column(JSON)
    tools = Column(JSON)
    experience_requirements = Column(JSON)
    education_requirements = Column(JSON)
    responsibilities = Column(JSON)
    keywords = Column(JSON)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    session = relationship("UserSession", back_populates="job_descriptions")
    match_analyses = relationship(
        "ResumeJobMatch", back_populates="job_description", cascade="all, delete-orphan"
    )


class ResumeJobMatch(Base):
    __tablename__ = "resume_job_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_analysis_id = Column(
        Integer, ForeignKey("resume_analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_description_id = Column(
        Integer, ForeignKey("job_descriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    technical_skills_match = Column(Float)
    experience_match = Column(Float)
    projects_match = Column(Float)
    education_match = Column(Float)
    overall_match = Column(Float)

    matching_skills = Column(JSON)
    missing_skills = Column(JSON)
    skill_gaps = Column(JSON)
    experience_gaps = Column(JSON)
    recommendations = Column(JSON)

    created_at = Column(DateTime, default=func.now())

    resume_analysis = relationship("ResumeAnalysis")
    job_description = relationship("JobDescription", back_populates="match_analyses")


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    resume_analysis_id = Column(
        Integer, ForeignKey("resume_analyses.id", ondelete="SET NULL"), nullable=True
    )
    job_description_id = Column(
        Integer, ForeignKey("job_descriptions.id", ondelete="SET NULL"), nullable=True
    )

    interview_type = Column(String(50))
    difficulty = Column(String(20))
    total_questions = Column(Integer, default=0)
    current_question_index = Column(Integer, default=0)
    duration_seconds = Column(Integer, default=0)
    answer_mode = Column(String(20), default="text")
    job_post_id = Column(
        Integer, ForeignKey("job_posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    interview_mode = Column(String(20), default="text")
    duration_minutes = Column(Integer, default=0)
    consent_given = Column(Boolean, default=False)
    is_job_interview = Column(Boolean, default=False, index=True)
    blueprint_json = Column(JSON, nullable=True)
    report_json = Column(JSON, nullable=True)

    status = Column(String(30), default="created")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    overall_score = Column(Float)
    technical_score = Column(Float)
    communication_score = Column(Float)
    hr_score = Column(Float)
    problem_solving_score = Column(Float)

    strengths = Column(JSON)
    weaknesses = Column(JSON)
    missed_concepts = Column(JSON)
    recommended_topics = Column(JSON)
    recommended_projects = Column(JSON)
    recommended_questions = Column(JSON)
    improvement_plan_7_day = Column(JSON)
    improvement_plan_30_day = Column(JSON)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    session = relationship("UserSession", back_populates="interview_sessions")
    resume_analysis = relationship("ResumeAnalysis")
    job_description = relationship("JobDescription")
    job_post = relationship("JobPost")
    questions = relationship(
        "InterviewQuestion",
        back_populates="interview_session",
        cascade="all, delete-orphan",
    )
    answers = relationship(
        "InterviewAnswer",
        back_populates="interview_session",
        cascade="all, delete-orphan",
    )
    evaluations = relationship(
        "InterviewEvaluation",
        back_populates="interview_session",
        cascade="all, delete-orphan",
    )
    media = relationship(
        "InterviewMedia",
        back_populates="interview_session",
        cascade="all, delete-orphan",
    )
    live_recordings = relationship(
        "LiveInterviewRecording",
        back_populates="interview_session",
        cascade="all, delete-orphan",
    )


class InterviewQuestion(Base):
    __tablename__ = "interview_questions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    interview_session_id = Column(
        Integer,
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    question_number = Column(Integer, nullable=False)
    category = Column(String(50))
    difficulty = Column(String(20))
    question_text = Column(Text, nullable=False)
    expected_key_points = Column(JSON)

    answer_text = Column(Text)
    answer_audio_path = Column(String(500))
    answer_mode = Column(String(20))

    technical_accuracy = Column(Float)
    relevance = Column(Float)
    communication = Column(Float)
    confidence = Column(Float)
    completeness = Column(Float)
    problem_solving = Column(Float)
    structure = Column(Float)
    overall_score = Column(Float)
    rubric_version = Column(String(20), default="v2")
    is_follow_up = Column(Boolean, default=False)
    parent_question_id = Column(
        Integer, ForeignKey("interview_questions.id", ondelete="SET NULL"), nullable=True
    )
    focus_skill = Column(String(120), nullable=True)
    difficulty_adjustment = Column(String(20), nullable=True)

    strengths = Column(JSON)
    weaknesses = Column(JSON)
    missing_points = Column(JSON)
    incorrect_points = Column(JSON)
    suggested_answer = Column(Text)
    ideal_answer = Column(Text)
    communication_feedback = Column(Text)
    technical_feedback = Column(Text)

    asked_at = Column(DateTime, default=func.now())
    answered_at = Column(DateTime, nullable=True)
    evaluated_at = Column(DateTime, nullable=True)

    interview_session = relationship("InterviewSession", back_populates="questions")
    answer = relationship(
        "InterviewAnswer", back_populates="question", uselist=False, cascade="all, delete-orphan"
    )


class InterviewProgress(Base):
    __tablename__ = "interview_progress"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)

    total_interviews = Column(Integer, default=0)
    total_questions_answered = Column(Integer, default=0)
    average_overall_score = Column(Float)
    average_technical_score = Column(Float)
    average_communication_score = Column(Float)
    average_hr_score = Column(Float)

    skill_scores = Column(JSON)
    skill_gaps = Column(JSON)
    strong_areas = Column(JSON)
    weak_areas = Column(JSON)

    score_history = Column(JSON)

    last_updated = Column(DateTime, default=func.now(), onupdate=func.now())

    session = relationship("UserSession")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = Column(String(30))
    source_id = Column(Integer, nullable=True)

    chunk_text = Column(Text, nullable=False)
    chunk_index = Column(Integer, nullable=False)

    embedding = Column(JSON)

    created_at = Column(DateTime, default=func.now())


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_hash = Column(String(64), nullable=False, index=True)
    source_type = Column(String(30), default="document")
    status = Column(String(20), default="processing", index=True)
    chunk_count = Column(Integer, default=0)
    word_count = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())
    processed_at = Column(DateTime, nullable=True)

    session = relationship("UserSession", back_populates="documents")


class LearningRoadmap(Base):
    __tablename__ = "learning_roadmaps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    summary = Column(Text, default="")
    focus_skills = Column(JSON, default=list)
    source_interview_id = Column(
        Integer, ForeignKey("interview_sessions.id", ondelete="SET NULL"), nullable=True
    )
    career_target = Column(String(255), default="")
    existing_skills = Column(JSON, default=list)
    skill_level = Column(String(40), default="intermediate")
    hours_per_day = Column(Float, default=1.0)
    duration_weeks = Column(Integer, default=4)
    status = Column(String(20), default="active", index=True)
    phases = Column(JSON, default=list)
    skill_gap = Column(JSON, default=dict)
    recommendations = Column(JSON, default=list)
    topic_mastery = Column(JSON, default=dict)
    generation_fingerprint = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)

    session = relationship("UserSession", back_populates="roadmaps")
    tasks = relationship(
        "LearningTask",
        back_populates="roadmap",
        cascade="all, delete-orphan",
        order_by="LearningTask.order_index",
    )


class LearningTask(Base):
    __tablename__ = "learning_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    roadmap_id = Column(
        Integer, ForeignKey("learning_roadmaps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_index = Column(Integer, nullable=False, default=0)
    title = Column(String(255), nullable=False)
    description = Column(Text, default="")
    topic = Column(String(120), default="")
    task_type = Column(String(24), default="daily", index=True)
    week_number = Column(Integer, nullable=True, index=True)
    day_number = Column(Integer, nullable=True)
    objective = Column(Text, default="")
    estimated_minutes = Column(Integer, default=30)
    priority = Column(String(20), default="medium")
    resources = Column(JSON, default=list)
    practice_questions = Column(JSON, default=list)
    status = Column(String(20), default="pending", index=True)
    completed_at = Column(DateTime, nullable=True)

    roadmap = relationship("LearningRoadmap", back_populates="tasks")


class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    roadmap_id = Column(
        Integer, ForeignKey("learning_roadmaps.id", ondelete="CASCADE"), nullable=True, index=True
    )
    week_number = Column(Integer, nullable=True, index=True)
    question_count = Column(Integer, default=0)
    topic = Column(String(120), nullable=False)
    difficulty = Column(String(20), default="medium")
    questions = Column(JSON, default=list)
    answers = Column(JSON, default=list)
    feedback = Column(JSON, default=list)
    score = Column(Float, default=0.0)
    max_score = Column(Float, default=0.0)
    status = Column(String(20), default="in_progress")
    created_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)

    session = relationship("UserSession", back_populates="assessments")


class GeneratedReport(Base):
    __tablename__ = "generated_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    interview_session_id = Column(
        Integer, ForeignKey("interview_sessions.id", ondelete="SET NULL"), nullable=True
    )
    report_type = Column(String(40), default="interview")
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=func.now())

    session = relationship("UserSession", back_populates="reports")


class EventLog(Base):
    __tablename__ = "event_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type = Column(String(60), nullable=False, index=True)
    message = Column(Text, default="")
    level = Column(String(20), default="info")
    created_at = Column(DateTime, default=func.now(), index=True)

    user = relationship("User", back_populates="events")


class AIPerformanceEvent(Base):
    __tablename__ = "ai_performance_events"
    __table_args__ = (
        Index("ix_ai_performance_events_type_created", "event_type", "created_at"),
        Index("ix_ai_performance_events_provider_created", "provider", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(40), nullable=False, index=True)
    provider = Column(String(40), nullable=True, index=True)
    model = Column(String(120), nullable=True, index=True)
    feature = Column(String(80), nullable=True, index=True)
    status = Column(String(30), nullable=False, index=True)
    severity = Column(String(20), nullable=True, index=True)
    latency_ms = Column(Float, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    error_category = Column(String(60), nullable=True, index=True)
    error_message = Column(Text, nullable=True)
    request_id = Column(String(64), nullable=True, unique=True, index=True)
    attempts = Column(Integer, nullable=False, default=1)
    used_fallback = Column(Boolean, nullable=False, default=False)
    details_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=func.now(), index=True)


class AIAlert(Base):
    __tablename__ = "ai_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_key = Column(String(120), nullable=False, unique=True, index=True)
    alert_type = Column(String(60), nullable=False, index=True)
    severity = Column(String(20), nullable=False, index=True)
    provider = Column(String(40), nullable=True, index=True)
    message = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="active", index=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    acknowledged_at = Column(DateTime, nullable=True)


class LearningResource(Base):
    __tablename__ = "learning_resources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False, index=True)
    description = Column(Text, nullable=False, default="")
    category = Column(String(50), nullable=False, index=True)
    skill_tags = Column(JSON, default=list)
    difficulty = Column(String(20), nullable=False, default="beginner", index=True)
    estimated_minutes = Column(Integer, nullable=False, default=15)
    content = Column(Text, nullable=False, default="")
    url = Column(String(500), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=func.now(), index=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class LearningResourceProgress(Base):
    __tablename__ = "learning_resource_progress"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "resource_id", name="uq_learning_resource_progress_session_resource"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_id = Column(
        Integer, ForeignKey("learning_resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status = Column(String(20), nullable=False, default="not_started", index=True)
    progress_percent = Column(Integer, nullable=False, default=0)
    last_viewed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class LearningActivity(Base):
    __tablename__ = "learning_activities"
    __table_args__ = (
        UniqueConstraint("session_id", "activity_key", name="uq_learning_activity_session_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    activity_date = Column(Date, default=func.current_date(), nullable=False, index=True)
    activity_type = Column(String(60), nullable=False, index=True)
    activity_key = Column(String(160), nullable=False, index=True)
    description = Column(Text, nullable=False, default="")
    points = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=func.now(), index=True)


class UserAchievement(Base):
    __tablename__ = "user_achievements"
    __table_args__ = (
        UniqueConstraint("session_id", "badge_key", name="uq_user_achievement_session_badge"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    badge_key = Column(String(80), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    description = Column(Text, nullable=False, default="")
    points = Column(Integer, nullable=False, default=0)
    earned_at = Column(DateTime, default=func.now(), index=True)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("session_id", "event_key", name="uq_notification_session_event"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notification_type = Column(String(60), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False, default="")
    severity = Column(String(20), nullable=False, default="info", index=True)
    target_page = Column(String(80), nullable=True)
    event_key = Column(String(180), nullable=True, index=True)
    is_read = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    read_at = Column(DateTime, nullable=True)


class JobPost(Base):
    __tablename__ = "job_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False, index=True)
    company = Column(String(200), nullable=False, index=True)
    description = Column(Text, nullable=False)
    responsibilities = Column(JSON, default=list)
    required_skills = Column(JSON, default=list)
    preferred_skills = Column(JSON, default=list)
    experience_level = Column(String(120), default="")
    education = Column(String(200), default="")
    interview_difficulty = Column(String(20), default="medium", index=True)
    question_preferences = Column(JSON, default=list)
    is_published = Column(Boolean, default=False, index=True)
    application_deadline = Column(DateTime, nullable=True, index=True)
    blueprint_json = Column(JSON, nullable=True)
    created_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=func.now(), index=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    skills = relationship(
        "JobSkill", back_populates="job_post", cascade="all, delete-orphan"
    )


class JobSkill(Base):
    __tablename__ = "job_skills"
    __table_args__ = (
        UniqueConstraint("job_post_id", "name", "skill_type", name="uq_job_skill_post_name_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_post_id = Column(
        Integer, ForeignKey("job_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String(160), nullable=False)
    skill_type = Column(String(20), nullable=False, default="required", index=True)
    category = Column(String(20), default="technical", index=True)
    source = Column(String(20), default="admin")
    created_at = Column(DateTime, default=func.now())

    job_post = relationship("JobPost", back_populates="skills")


class InterviewAnswer(Base):
    __tablename__ = "interview_answers"
    __table_args__ = (
        UniqueConstraint("question_id", name="uq_interview_answer_question"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    interview_session_id = Column(
        Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id = Column(
        Integer, ForeignKey("interview_questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    answer_text = Column(Text, nullable=False)
    transcription_source = Column(String(20), default="text")
    created_at = Column(DateTime, default=func.now())

    interview_session = relationship("InterviewSession", back_populates="answers")
    question = relationship("InterviewQuestion", back_populates="answer")
    media = relationship(
        "InterviewMedia", back_populates="answer", cascade="all, delete-orphan", uselist=False
    )
    evaluation = relationship(
        "InterviewEvaluation", back_populates="answer", uselist=False, cascade="all, delete-orphan"
    )


class InterviewEvaluation(Base):
    __tablename__ = "interview_evaluations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    interview_session_id = Column(
        Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    answer_id = Column(
        Integer, ForeignKey("interview_answers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id = Column(
        Integer, ForeignKey("interview_questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scores = Column(JSON, default=dict)
    knowledge_gaps = Column(JSON, default=list)
    demonstrated_strengths = Column(JSON, default=list)
    job_requirement_relevance = Column(Float, default=0.0)
    next_difficulty = Column(String(20), default="medium")
    follow_up_needed = Column(Boolean, default=False)
    follow_up_focus = Column(String(200), default="")
    suggested_answer = Column(Text, default="")
    feedback = Column(JSON, default=dict)
    rubric_version = Column(String(20), default="v2")
    created_at = Column(DateTime, default=func.now())

    interview_session = relationship("InterviewSession", back_populates="evaluations")
    answer = relationship("InterviewAnswer", back_populates="evaluation")
    question = relationship("InterviewQuestion")


class InterviewMedia(Base):
    __tablename__ = "interview_media"

    id = Column(Integer, primary_key=True, autoincrement=True)
    interview_session_id = Column(
        Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    answer_id = Column(
        Integer, ForeignKey("interview_answers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_type = Column(String(20), nullable=False, index=True)
    storage_path = Column(String(500), nullable=False)
    original_name = Column(String(255), nullable=True)
    mime_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, default=0)
    sha256 = Column(String(64), nullable=True, index=True)
    consent_given = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now(), index=True)

    interview_session = relationship("InterviewSession", back_populates="media")
    answer = relationship("InterviewAnswer", back_populates="media")


class LiveInterviewRecording(Base):
    __tablename__ = "live_interview_recordings"
    __table_args__ = (
        UniqueConstraint("interview_session_id", name="uq_live_recording_interview"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    interview_session_id = Column(
        Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consent_given = Column(Boolean, nullable=False, default=False)
    consent_at = Column(DateTime, nullable=True)
    consent_policy_version = Column(String(40), nullable=False, default="live-recording-v1")
    recording_status = Column(String(20), nullable=False, default="recording", index=True)
    started_at = Column(DateTime, nullable=True)
    finalized_at = Column(DateTime, nullable=True)
    deletion_status = Column(String(20), nullable=False, default="active", index=True)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    interview_session = relationship("InterviewSession", back_populates="live_recordings")
    media = relationship(
        "LiveInterviewMedia", back_populates="recording", cascade="all, delete-orphan"
    )


class LiveInterviewMedia(Base):
    __tablename__ = "live_interview_media"
    __table_args__ = (
        UniqueConstraint("recording_id", "media_type", name="uq_live_media_recording_type"),
        UniqueConstraint("storage_key", name="uq_live_media_storage_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    recording_id = Column(
        Integer, ForeignKey("live_interview_recordings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    interview_session_id = Column(
        Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_type = Column(String(20), nullable=False, index=True)
    storage_key = Column(String(500), nullable=False)
    filename = Column(String(255), nullable=False)
    mime_type = Column(String(120), nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    duration_seconds = Column(Float, nullable=False, default=0.0)
    checksum = Column(String(64), nullable=False, index=True)
    media_status = Column(String(20), nullable=False, default="ready", index=True)
    metadata_json = Column(JSON, nullable=False, default=dict)
    finalized_at = Column(DateTime, nullable=True)
    deletion_status = Column(String(20), nullable=False, default="active", index=True)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)

    recording = relationship("LiveInterviewRecording", back_populates="media")


class InterviewCallRoom(Base):
    __tablename__ = "interview_call_rooms"
    __table_args__ = (
        UniqueConstraint("room_id", name="uq_interview_call_room_id"),
        UniqueConstraint("invite_token_hash", name="uq_interview_call_invite_hash"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(String(64), nullable=False, index=True)
    admin_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String(200), nullable=True)
    status = Column(String(30), nullable=False, default="invited", index=True)
    invite_token_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    accepted_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class InterviewCallParticipant(Base):
    __tablename__ = "interview_call_participants"
    __table_args__ = (
        UniqueConstraint(
            "room_id", "user_id", name="uq_interview_call_participant_room_user"
        ),
        UniqueConstraint("room_id", "role", name="uq_interview_call_participant_room_role"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(
        Integer,
        ForeignKey("interview_call_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String(20), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="invited", index=True)
    joined_at = Column(DateTime, nullable=True)
    left_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class InterviewCallSignal(Base):
    __tablename__ = "interview_call_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(
        Integer,
        ForeignKey("interview_call_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    signal_type = Column(String(40), nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    consumed_at = Column(DateTime, nullable=True, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=func.now(), index=True)


class AdminReportHistory(Base):
    __tablename__ = "admin_report_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_name = Column(String(160), nullable=False)
    category = Column(String(60), nullable=False, index=True)
    generated_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    generated_by_session_id = Column(
        Integer, ForeignKey("user_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    generated_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    date_start = Column(Date, nullable=True)
    date_end = Column(Date, nullable=True)
    export_format = Column(String(10), nullable=False)
    status = Column(String(20), nullable=False, default="generated", index=True)
    filter_json = Column(Text, nullable=False, default="{}")
    file_name = Column(String(220), nullable=False)


class DataBackup(Base):
    __tablename__ = "data_backups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    backup_id = Column(String(80), nullable=False, unique=True, index=True)
    filename = Column(String(180), nullable=False)
    backup_type = Column(String(20), nullable=False, default="manual", index=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    integrity_status = Column(String(20), nullable=False, default="unknown", index=True)
    size_bytes = Column(Integer, nullable=False, default=0)
    checksum = Column(String(64), nullable=True, index=True)
    db_engine = Column(String(20), nullable=False, default="sqlite")
    table_count = Column(Integer, nullable=False, default=0)
    page_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True)
    actor_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    error_message = Column(Text, nullable=False, default="")
    metadata_json = Column(JSON, nullable=False, default=dict)


class DataRetentionSetting(Base):
    __tablename__ = "data_retention_settings"

    key = Column(String(80), primary_key=True)
    value_json = Column(JSON, nullable=False, default=dict)
    description = Column(Text, nullable=False, default="")
    updated_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_tickets_user_updated", "user_id", "updated_at"),
        Index("ix_support_tickets_status_priority", "status", "priority"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String(80), nullable=False, unique=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject = Column(String(200), nullable=False)
    category = Column(String(60), nullable=False, index=True)
    description = Column(Text, nullable=False, default="")
    priority = Column(String(20), nullable=False, default="Medium", index=True)
    status = Column(String(30), nullable=False, default="Open", index=True)
    resolved_at = Column(DateTime, nullable=True)
    last_activity_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False, index=True
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"
    __table_args__ = (Index("ix_support_messages_ticket_created", "ticket_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(
        Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_role = Column(String(20), nullable=False, default="user", index=True)
    message_type = Column(String(30), nullable=False, default="reply", index=True)
    message = Column(Text, nullable=False, default="")
    metadata_json = Column(JSON, nullable=False, default=dict)
    is_read_by_user = Column(Boolean, nullable=False, default=False, index=True)
    is_read_by_admin = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)


class SupportInternalNote(Base):
    __tablename__ = "support_internal_notes"
    __table_args__ = (Index("ix_support_internal_notes_ticket_created", "ticket_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(
        Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admin_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)


class SupportFeedbackNote(Base):
    __tablename__ = "support_feedback_notes"
    __table_args__ = (
        Index("ix_support_feedback_notes_feedback_created", "feedback_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    feedback_id = Column(
        Integer, ForeignKey("user_feedback.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admin_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)


class SupportAttachment(Base):
    __tablename__ = "support_attachments"
    __table_args__ = (Index("ix_support_attachments_record", "record_type", "record_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_type = Column(String(30), nullable=False, index=True)
    record_id = Column(Integer, nullable=False, index=True)
    original_filename = Column(String(255), nullable=False)
    safe_filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    mime_type = Column(String(120), nullable=False)
    extension = Column(String(20), nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    checksum = Column(String(64), nullable=False, index=True)
    uploader_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    uploader_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (Index("ix_user_feedback_user_created", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    feedback_id = Column(String(80), nullable=False, unique=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category = Column(String(60), nullable=False, index=True)
    rating = Column(Integer, nullable=False, default=5, index=True)
    details = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, default="New", index=True)
    admin_response = Column(Text, nullable=False, default="")
    responded_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    responded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False, index=True
    )


class BugReport(Base):
    __tablename__ = "bug_reports"
    __table_args__ = (Index("ix_bug_reports_user_created", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(String(80), nullable=False, unique=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String(200), nullable=False)
    feature = Column(String(120), nullable=False, index=True)
    description = Column(Text, nullable=False)
    steps_to_reproduce = Column(Text, nullable=False, default="")
    expected_result = Column(Text, nullable=False, default="")
    actual_result = Column(Text, nullable=False, default="")
    severity = Column(String(20), nullable=False, default="Medium", index=True)
    status = Column(String(30), nullable=False, default="New", index=True)
    admin_notes = Column(Text, nullable=False, default="")
    resolution_details = Column(Text, nullable=False, default="")
    resolved_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False, index=True
    )


class FeatureRequest(Base):
    __tablename__ = "feature_requests"
    __table_args__ = (Index("ix_feature_requests_user_created", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(80), nullable=False, unique=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    category = Column(String(60), nullable=False, index=True)
    status = Column(String(30), nullable=False, default="Submitted", index=True)
    admin_notes = Column(Text, nullable=False, default="")
    published = Column(Boolean, nullable=False, default=False, index=True)
    announced = Column(Boolean, nullable=False, default=False, index=True)
    released_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False, index=True
    )


class FeatureRequestVote(Base):
    __tablename__ = "feature_request_votes"
    __table_args__ = (
        UniqueConstraint("request_id", "user_id", name="uq_feature_request_vote_request_user"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(
        Integer, ForeignKey("feature_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)


class ContactMessage(Base):
    __tablename__ = "contact_messages"
    __table_args__ = (Index("ix_contact_messages_user_created", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    reference_id = Column(String(80), nullable=False, unique=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject = Column(String(200), nullable=False)
    category = Column(String(60), nullable=False, index=True)
    message = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, default="New", index=True)
    is_read_by_admin = Column(Boolean, nullable=False, default=False, index=True)
    admin_reply = Column(Text, nullable=False, default="")
    replied_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    replied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False, index=True
    )


class SupportFaq(Base):
    __tablename__ = "support_faqs"
    __table_args__ = (Index("ix_support_faqs_published_category", "is_published", "category"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    faq_id = Column(String(80), nullable=False, unique=True, index=True)
    category = Column(String(60), nullable=False, index=True)
    question = Column(String(300), nullable=False)
    answer = Column(Text, nullable=False)
    display_order = Column(Integer, nullable=False, default=0, index=True)
    is_published = Column(Boolean, nullable=False, default=False, index=True)
    created_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    updated_by_admin_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False, index=True
    )
