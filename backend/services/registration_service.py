"""Admin-approved registration requests.

Visitors submit a request without creating an active account. Administrators
review the request and, on approval, create the account in the same database
transaction while transferring the already-hashed password verbatim.
"""

import re
import threading
from dataclasses import dataclass
from datetime import datetime

from config.settings import settings
from database import EventLog, RegistrationRequest, User, get_db_context
from services.auth_service import (
    AuthError,
    _require_admin_actor,
    create_user_from_hash,
    hash_password,
    validate_email,
    validate_full_name,
    validate_password,
    validate_username,
)
from sqlalchemy.exc import IntegrityError

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STATUSES = (PENDING, APPROVED, REJECTED)

_registration_lock = threading.RLock()


@dataclass(frozen=True)
class RegistrationRequestView:
    """Safe registration response: it never contains a password or password hash."""

    id: int
    full_name: str
    email: str
    submitted_at: datetime | None
    status: str
    reviewed_at: datetime | None
    reviewed_by_admin_id: int | None
    created_user_id: int | None


def _to_view(row: RegistrationRequest) -> RegistrationRequestView:
    return RegistrationRequestView(
        id=int(row.id),
        full_name=str(row.full_name),
        email=str(row.email),
        submitted_at=row.submitted_at,
        status=str(row.status),
        reviewed_at=row.reviewed_at,
        reviewed_by_admin_id=row.reviewed_by_admin_id,
        created_user_id=row.created_user_id,
    )


def _sanitize_username(email: str) -> str:
    base = email.split("@", 1)[0].lower()
    cleaned = re.sub(r"[^a-z0-9_.-]", "", base).strip("._-")
    if len(cleaned) < 3:
        cleaned = f"user{cleaned}"
    return cleaned[:30]


def _next_username(db, email: str) -> str:
    base = _sanitize_username(email)
    candidate = base
    for suffix in range(1, 1000):
        if not db.query(User).filter(User.username == candidate).first():
            return validate_username(candidate)
        marker = str(suffix)
        candidate = f"{base[: 30 - len(marker)]}{marker}"
    raise AuthError("Could not allocate a unique username for this registration.")


def submit_registration_request(
    *, full_name: str, email: str, password: str, confirm_password: str
) -> RegistrationRequestView:
    """Store a pending request without creating a login-capable user account."""
    if not settings.enable_registration:
        raise AuthError("Registration is disabled by the administrator.")
    normalized_email = validate_email(email)
    normalized_name = validate_full_name(full_name)
    validate_password(password)
    if password != confirm_password:
        raise AuthError("Passwords do not match.")

    with _registration_lock:
        try:
            with get_db_context() as db:
                if db.query(User).filter(User.email == normalized_email).first():
                    raise AuthError("An account with this email already exists.")
                if (
                    db.query(RegistrationRequest)
                    .filter(RegistrationRequest.email == normalized_email)
                    .first()
                ):
                    raise AuthError(
                        "A registration request with this email already exists. "
                        "Please wait for administrator review."
                    )

                row = RegistrationRequest(
                    full_name=normalized_name,
                    email=normalized_email,
                    password_hash=hash_password(password),
                    status=PENDING,
                    submitted_at=datetime.now(),
                )
                db.add(row)
                db.flush()
                db.add(
                    EventLog(
                        event_type="registration.request_submitted",
                        message=f"Registration request {row.id} submitted",
                    )
                )
                return _to_view(row)
        except IntegrityError as exc:
            raise AuthError(
                "A registration request with this email already exists. "
                "Please wait for administrator review."
            ) from exc


def list_registration_requests(
    *, actor_admin_id: int | None, status: str | None = None
) -> list[RegistrationRequestView]:
    """List requests for an authorized admin. Password hashes are never selected for display."""
    normalized_status: str | None = None
    if status:
        normalized_status = str(status).strip().lower()
        if normalized_status not in STATUSES:
            raise AuthError("Unsupported registration status.")

    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        query = db.query(RegistrationRequest)
        if normalized_status:
            query = query.filter(RegistrationRequest.status == normalized_status)
        rows = query.order_by(
            RegistrationRequest.submitted_at.desc(), RegistrationRequest.id.desc()
        ).all()
        return [_to_view(row) for row in rows]


def get_registration_request_counts(*, actor_admin_id: int | None) -> dict[str, int]:
    with get_db_context() as db:
        _require_admin_actor(db, actor_admin_id)
        return {
            name: int(
                db.query(RegistrationRequest).filter(RegistrationRequest.status == name).count()
            )
            for name in STATUSES
        }


def approve_registration_request(
    *, request_id: int, actor_admin_id: int | None
) -> tuple[RegistrationRequestView, str]:
    """Atomically create the user and mark a still-pending request approved."""
    with _registration_lock:
        try:
            with get_db_context() as db:
                admin = _require_admin_actor(db, actor_admin_id)
                row = (
                    db.query(RegistrationRequest)
                    .filter(RegistrationRequest.id == int(request_id))
                    .first()
                )
                if row is None:
                    raise AuthError("Registration request not found.")
                if str(row.status) != PENDING:
                    raise AuthError("Only pending registration requests can be approved.")
                if db.query(User).filter(User.email == row.email).first():
                    raise AuthError("An account with this email already exists.")

                username = _next_username(db, str(row.email))
                account = create_user_from_hash(
                    db,
                    actor_admin_id=actor_admin_id,
                    email=str(row.email),
                    password_hash=str(row.password_hash),
                    full_name=str(row.full_name),
                    username=username,
                    created_by_admin_id=admin.id,
                    account_status="active",
                    must_change_password=False,
                    event_type="registration.account_created",
                    event_message=f"Account created from registration request {row.id}",
                )
                now = datetime.now()
                row.status = APPROVED
                row.reviewed_at = now
                row.reviewed_by_admin_id = admin.id
                row.created_user_id = account.id
                db.add(
                    EventLog(
                        user_id=account.id,
                        event_type="registration.request_approved",
                        message=f"Registration request {row.id} approved",
                    )
                )
                return _to_view(row), str(account.email)
        except IntegrityError as exc:
            raise AuthError(
                "Could not create the account because its email or username is already in use."
            ) from exc


def reject_registration_request(
    *, request_id: int, actor_admin_id: int | None
) -> RegistrationRequestView:
    """Reject a pending request without creating an account, preserving the audit row."""
    with _registration_lock, get_db_context() as db:
        admin = _require_admin_actor(db, actor_admin_id)
        row = (
            db.query(RegistrationRequest).filter(RegistrationRequest.id == int(request_id)).first()
        )
        if row is None:
            raise AuthError("Registration request not found.")
        if str(row.status) != PENDING:
            raise AuthError("Only pending registration requests can be rejected.")

        row.status = REJECTED
        row.reviewed_at = datetime.now()
        row.reviewed_by_admin_id = admin.id
        db.add(
            EventLog(
                event_type="registration.request_rejected",
                message=f"Registration request {row.id} rejected",
            )
        )
        return _to_view(row)
