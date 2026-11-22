from datetime import UTC, date, datetime, timedelta

import database.db as db_module
import pytest
from config.settings import settings
from database import InterviewQuestion, InterviewSession, User, UserSession, get_db_context
from services.analytics import get_analytics
from services.auth_service import AuthError


@pytest.fixture
def analytics_db(tmp_path):
    original_database = settings.database_path
    original_vectors = settings.vector_store_path
    settings.database_path = str(tmp_path / "analytics_regression.db")
    settings.vector_store_path = str(tmp_path / "vectors")
    db_module.engine = None
    db_module.SessionLocal = None
    db_module.init_database()

    with db_module.get_db_context() as db:
        one = User(
            email="analytics-one@example.com",
            username="analytics-one",
            full_name="Analytics One",
            password_hash="isolated-test-hash",
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        two = User(
            email="analytics-two@example.com",
            username="analytics-two",
            full_name="Analytics Two",
            password_hash="isolated-test-hash",
            role="user",
            account_status="active",
            is_active=True,
            is_verified=True,
            must_change_password=False,
        )
        db.add_all([one, two])
        db.flush()
        session_one = UserSession(session_id="analytics-user-1", user_id=one.id, label="one")
        session_two = UserSession(session_id="analytics-user-2", user_id=two.id, label="two")
        db.add_all([session_one, session_two])
        db.flush()
        ids = {
            "user1": one.id,
            "user2": two.id,
            "session1": session_one.id,
            "session2": session_two.id,
            "database_path": settings.database_path,
        }

    yield ids

    db_module.close_database()
    db_module.SessionLocal = None
    settings.database_path = original_database
    settings.vector_store_path = original_vectors


def _dt(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _seed_interview(
    session_id: int,
    *,
    created: datetime,
    difficulty: str = "medium",
    interview_type: str = "Technical",
    status: str = "completed",
    score: float | None = None,
    technical: float | None = None,
    communication: float | None = None,
    problem_solving: float | None = None,
    duration_seconds: int = 0,
    recommended_topics: list[str] | None = None,
) -> int:
    with get_db_context() as db:
        row = InterviewSession(
            session_id=session_id,
            interview_type=interview_type,
            difficulty=difficulty,
            status=status,
            overall_score=score,
            technical_score=technical,
            communication_score=communication,
            problem_solving_score=problem_solving,
            duration_seconds=duration_seconds,
            recommended_topics=recommended_topics or [],
            created_at=created,
            completed_at=created if status == "completed" else None,
        )
        db.add(row)
        db.flush()
        return int(row.id)


def _seed_question(
    interview_id: int,
    *,
    number: int = 1,
    category: str = "technical",
    score: float | None = None,
    technical_accuracy: float | None = None,
    communication: float | None = None,
    confidence: float | None = None,
    problem_solving: float | None = None,
    structure: float | None = None,
) -> None:
    with get_db_context() as db:
        db.add(
            InterviewQuestion(
                interview_session_id=interview_id,
                question_number=number,
                category=category,
                question_text="Isolated regression question",
                overall_score=score,
                technical_accuracy=technical_accuracy,
                communication=communication,
                confidence=confidence,
                problem_solving=problem_solving,
                structure=structure,
            )
        )


class TestAnalyticsBasicCounts:
    def test_basic_analytics_counts(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(analytics_db["session1"], created=base, score=7.0)
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            score=9.0,
            status="in_progress",
        )
        _seed_interview(analytics_db["session1"], created=base + timedelta(hours=2), score=8.0)

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.total_interviews == 3
        assert snapshot.completed_interviews == 2
        assert snapshot.completion_rate == 66.7


class TestAnalyticsScores:
    def test_average_best_and_lowest_scores(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(
            analytics_db["session1"],
            created=base,
            score=7.0,
            technical=8.0,
            communication=6.0,
            problem_solving=7.0,
        )
        _seed_interview(analytics_db["session1"], created=base + timedelta(hours=1))
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=2),
            score=9.0,
            technical=9.0,
            communication=8.0,
            problem_solving=9.0,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.average_score == 8.0
        assert snapshot.best_score == 9.0
        assert snapshot.lowest_score == 7.0
        assert snapshot.scored_interviews == 2
        assert snapshot.technical_score == 8.5
        assert snapshot.communication_score == 7.0
        assert snapshot.problem_solving_score == 8.0
        expected_readiness = round(8.0 * 0.4 + 8.5 * 0.25 + 7.0 * 0.2 + 8.0 * 0.15, 2)
        assert snapshot.readiness_score == expected_readiness

    def test_null_and_missing_scores(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        scored_id = _seed_interview(
            analytics_db["session1"],
            created=base,
            score=7.0,
            recommended_topics=["Caching"],
        )
        unscored_id = _seed_interview(
            analytics_db["session1"], created=base + timedelta(hours=1)
        )
        _seed_question(
            scored_id,
            number=1,
            category="technical",
            score=8.0,
            technical_accuracy=9.0,
            communication=7.0,
            problem_solving=8.0,
        )
        _seed_question(
            scored_id,
            number=2,
            category="behavioral",
            score=6.0,
            communication=6.0,
            confidence=5.0,
            structure=6.0,
        )
        _seed_question(unscored_id, number=1, score=None)

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.total_answered == 2
        assert snapshot.confidence_score == 6.9
        assert snapshot.strong_areas == ["technical", "problem_solving"]
        assert snapshot.weak_areas == ["communication", "structure", "confidence"]
        assert snapshot.skill_breakdown is not None
        assert set(snapshot.skill_breakdown["Skill"]) == {
            "technical",
            "communication",
            "confidence",
            "problem_solving",
            "structure",
        }
        assert set(snapshot.category_scores["Category"]) == {"technical", "behavioral"}
        assert snapshot.recommendations == ["Caching"]


class TestAnalyticsPracticeHours:
    def test_practice_hours_calculation(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(
            analytics_db["session1"],
            created=base,
            score=7.0,
            duration_seconds=1800,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            score=8.0,
            duration_seconds=3600,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=2),
            score=9.0,
            duration_seconds=0,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.practice_hours == 1.5


class TestAnalyticsImprovement:
    def test_improvement_percentage(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(analytics_db["session1"], created=base, score=5.0)
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            score=10.0,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.performance_improvement_percent == 100.0
        assert snapshot.latest_score == 10.0
        assert snapshot.previous_score == 5.0
        assert snapshot.progress_percent == 100.0

    def test_zero_first_score_handling(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(analytics_db["session1"], created=base, score=0.0)
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            score=8.0,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.performance_improvement_percent == 0.0
        assert snapshot.average_score == 4.0
        assert snapshot.best_score == 8.0
        assert snapshot.lowest_score == 0.0


class TestAnalyticsFilters:
    def test_difficulty_filtering_easy_medium_hard(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(
            analytics_db["session1"],
            created=base,
            difficulty="easy",
            score=6.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            difficulty="medium",
            score=7.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=2),
            difficulty="hard",
            score=8.0,
        )

        easy = get_analytics(
            analytics_db["session1"], analytics_db["user1"], difficulty="easy"
        )
        medium = get_analytics(
            analytics_db["session1"], analytics_db["user1"], difficulty="medium"
        )
        hard = get_analytics(
            analytics_db["session1"], analytics_db["user1"], difficulty="hard"
        )

        assert easy.total_interviews == 1
        assert easy.average_score == 6.0
        assert medium.total_interviews == 1
        assert medium.average_score == 7.0
        assert hard.total_interviews == 1
        assert hard.average_score == 8.0

    def test_interview_type_filtering(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(
            analytics_db["session1"],
            created=base,
            interview_type="Technical",
            score=7.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            interview_type="Behavioral",
            score=8.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=2),
            interview_type="HR",
            score=9.0,
        )

        technical = get_analytics(
            analytics_db["session1"], analytics_db["user1"], interview_type="Technical"
        )
        behavioral = get_analytics(
            analytics_db["session1"], analytics_db["user1"], interview_type="Behavioral"
        )

        assert technical.total_interviews == 1
        assert technical.average_score == 7.0
        assert behavioral.total_interviews == 1
        assert behavioral.average_score == 8.0

    def test_date_range_filtering(self, analytics_db):
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 1, 1, 9, 0, 0),
            score=4.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 1, 15, 9, 0, 0),
            score=7.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 2, 1, 9, 0, 0),
            score=9.0,
        )

        bounded = get_analytics(
            analytics_db["session1"],
            analytics_db["user1"],
            date_from=date(2026, 1, 10),
            date_to=date(2026, 1, 20),
        )
        from_only = get_analytics(
            analytics_db["session1"],
            analytics_db["user1"],
            date_from=date(2026, 1, 10),
        )
        to_only = get_analytics(
            analytics_db["session1"],
            analytics_db["user1"],
            date_to=date(2026, 1, 20),
        )

        assert bounded.total_interviews == 1
        assert bounded.average_score == 7.0
        assert from_only.total_interviews == 2
        assert from_only.best_score == 9.0
        assert to_only.total_interviews == 2
        assert to_only.best_score == 7.0

    def test_score_range_filtering(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(analytics_db["session1"], created=base, score=4.0)
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            score=7.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=2),
            score=9.0,
        )

        bounded = get_analytics(
            analytics_db["session1"],
            analytics_db["user1"],
            minimum_score=6.0,
            maximum_score=8.0,
        )
        minimum_only = get_analytics(
            analytics_db["session1"],
            analytics_db["user1"],
            minimum_score=8.0,
        )
        maximum_only = get_analytics(
            analytics_db["session1"],
            analytics_db["user1"],
            maximum_score=5.0,
        )

        assert bounded.total_interviews == 3
        assert bounded.completed_interviews == 1
        assert bounded.scored_interviews == 1
        assert bounded.average_score == 7.0
        assert minimum_only.average_score == 9.0
        assert maximum_only.average_score == 4.0

    def test_unsupported_filter_values_return_no_matches(self, analytics_db):
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 1, 1, 9, 0, 0),
            difficulty="medium",
            interview_type="Technical",
            score=7.0,
        )

        technical_difficulty = get_analytics(
            analytics_db["session1"], analytics_db["user1"], difficulty="technical"
        )
        behavioral_difficulty = get_analytics(
            analytics_db["session1"], analytics_db["user1"], difficulty="behavioral"
        )
        case_type = get_analytics(
            analytics_db["session1"], analytics_db["user1"], interview_type="Case"
        )
        reversed_range = get_analytics(
            analytics_db["session1"],
            analytics_db["user1"],
            minimum_score=9.0,
            maximum_score=2.0,
        )

        assert technical_difficulty.total_interviews == 0
        assert behavioral_difficulty.total_interviews == 0
        assert case_type.total_interviews == 0
        assert reversed_range.total_interviews == 1
        assert reversed_range.completed_interviews == 0
        assert reversed_range.average_score == 0.0


class TestAnalyticsStates:
    def test_empty_analytics_state(self, analytics_db):
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 1, 1, 9, 0, 0),
            status="in_progress",
            score=None,
            duration_seconds=600,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.total_interviews == 1
        assert snapshot.completed_interviews == 0
        assert snapshot.scored_interviews == 0
        assert snapshot.average_score == 0.0
        assert snapshot.best_score == 0.0
        assert snapshot.lowest_score == 0.0
        assert snapshot.practice_hours == 0.0
        assert snapshot.completion_rate == 0.0
        assert snapshot.trend is not None
        assert snapshot.trend.empty
        assert len(snapshot.recent_sessions) == 1
        assert snapshot.recent_sessions[0]["Score"] is None

    def test_single_interview_analytics(self, analytics_db):
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 1, 1, 9, 0, 0),
            score=8.0,
            technical=8.0,
            communication=7.0,
            problem_solving=8.0,
            duration_seconds=1800,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.total_interviews == 1
        assert snapshot.completed_interviews == 1
        assert snapshot.completion_rate == 100.0
        assert snapshot.average_score == 8.0
        assert snapshot.best_score == 8.0
        assert snapshot.lowest_score == 8.0
        assert snapshot.latest_score == 8.0
        assert snapshot.previous_score == 0.0
        assert snapshot.progress_percent == 0.0
        assert snapshot.performance_improvement_percent == 0.0
        assert snapshot.practice_hours == 0.5

    def test_user_with_no_interview_records(self, analytics_db):
        snapshot = get_analytics(analytics_db["session2"], analytics_db["user2"])

        assert snapshot.total_interviews == 0
        assert snapshot.completed_interviews == 0
        assert snapshot.scored_interviews == 0
        assert snapshot.average_score == 0.0
        assert snapshot.completion_rate == 0.0
        assert snapshot.recent_sessions == []
        assert snapshot.trend is not None
        assert snapshot.trend.empty

    def test_unscored_interviews(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(
            analytics_db["session1"],
            created=base,
            technical=8.0,
            communication=6.0,
            problem_solving=7.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            technical=8.0,
            communication=6.0,
            problem_solving=7.0,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.completed_interviews == 2
        assert snapshot.scored_interviews == 0
        assert snapshot.average_score == 0.0
        assert snapshot.best_score == 0.0
        assert snapshot.lowest_score == 0.0
        assert snapshot.technical_score == 8.0
        assert snapshot.communication_score == 6.0
        assert snapshot.problem_solving_score == 7.0
        assert snapshot.performance_improvement_percent == 0.0
        assert list(snapshot.trend["Overall"]) == [0.0, 0.0]
        assert list(snapshot.trend["Technical"]) == [8.0, 8.0]


class TestAnalyticsIsolation:
    def test_cross_user_data_isolation(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(analytics_db["session1"], created=base, score=6.0)
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            score=8.0,
        )
        _seed_interview(analytics_db["session2"], created=base, score=10.0)

        first = get_analytics(analytics_db["session1"], analytics_db["user1"])
        second = get_analytics(analytics_db["session2"], analytics_db["user2"])

        assert first.total_interviews == 2
        assert first.best_score == 8.0
        assert second.total_interviews == 1
        assert second.best_score == 10.0

        with pytest.raises(AuthError):
            get_analytics(analytics_db["session2"], analytics_db["user1"])
        with pytest.raises(AuthError):
            get_analytics(analytics_db["session1"], None)


class TestAnalyticsTrends:
    def test_recent_sessions_dates_support_weekly_activity(self, analytics_db):
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 3, 1, 9, 0, 0),
            score=5.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 3, 2, 9, 0, 0),
            score=6.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=_dt(2026, 3, 2, 15, 0, 0),
            score=7.0,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])
        dates = [session["Date"] for session in snapshot.recent_sessions]

        assert dates == ["2026-03-02", "2026-03-02", "2026-03-01"]
        assert all(date.fromisoformat(value).isoformat() == value for value in dates)

        anchor = date(2026, 3, 2)
        previous_day = date(2026, 3, 1)
        weekly_counts = {
            (anchor - timedelta(days=offset)).strftime("%a"): sum(
                1
                for session in snapshot.recent_sessions
                if session["Date"] == (anchor - timedelta(days=offset)).isoformat()
            )
            for offset in range(6, -1, -1)
        }
        assert weekly_counts[anchor.strftime("%a")] == 2
        assert weekly_counts[previous_day.strftime("%a")] == 1

    def test_score_progression_calculations(self, analytics_db):
        base = _dt(2026, 1, 1, 9, 0, 0)
        _seed_interview(
            analytics_db["session1"],
            created=base,
            score=5.0,
            technical=5.0,
            communication=5.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=1),
            score=8.0,
            technical=8.0,
            communication=8.0,
        )
        _seed_interview(
            analytics_db["session1"],
            created=base + timedelta(hours=2),
            score=6.0,
            technical=6.0,
            communication=6.0,
        )

        snapshot = get_analytics(analytics_db["session1"], analytics_db["user1"])

        assert snapshot.trend is not None
        assert list(snapshot.trend["Session"]) == ["#1", "#2", "#3"]
        assert list(snapshot.trend["Overall"]) == [5.0, 8.0, 6.0]
        assert list(snapshot.trend["Technical"]) == [5.0, 8.0, 6.0]
        assert list(snapshot.trend["Communication"]) == [5.0, 8.0, 6.0]
        assert snapshot.latest_score == 6.0
        assert snapshot.previous_score == 8.0
        assert snapshot.progress_percent == -25.0
        assert snapshot.performance_improvement_percent == 20.0
