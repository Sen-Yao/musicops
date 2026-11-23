from __future__ import annotations

from enum import StrEnum


class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


TERMINAL_OPERATION_STATUSES = {
    OperationStatus.SUCCEEDED,
    OperationStatus.FAILED,
    OperationStatus.UNCERTAIN,
    OperationStatus.CANCELLED,
}
