"""bot_jobs 상태·소스 상수."""

from __future__ import annotations


class JobStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobSource:
    SLACK = "slack"
    GRAPH = "graph"
    CONFLUENCE = "confluence"
    INTERNAL = "internal"


PERMANENT_SLACK_ERRORS: frozenset[str] = frozenset({
    "channel_not_found",
    "user_not_found",
    "is_archived",
    "account_inactive",
    "invalid_auth",
    "token_revoked",
    "not_in_channel",
    "team_access_not_granted",
    "is_inactive",
    "cannot_dm_bot",
    "user_deactivated",
})


class PermanentJobError(Exception):
    """재시도 없이 failed로 종료할 작업 오류."""

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code or "permanent"
