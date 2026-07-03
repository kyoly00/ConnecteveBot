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
