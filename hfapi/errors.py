"""方舟（Seedance）错误信封。客户端只按 error.code 分支，type 仅用于日志聚合。"""
from __future__ import annotations

from typing import Any

# 方舟官方码（本项目只用到其中一小部分，其余按需补）
CODE_INVALID = "InvalidParameter"
CODE_MISSING = "MissingParameter"
CODE_UNSUPPORTED = "InvalidParameter.UnsupportedParameter"
CODE_TASK_TYPE = "InvalidParameter.TaskTypeConstraint"
CODE_AUTH = "AuthenticationError"
CODE_BILLING_OFF = "Forbidden.BillingDisabled"
CODE_RATE_LIMIT = "RateLimitExceeded.EndpointRPMExceeded"
CODE_INTERNAL = "InternalServiceError"
CODE_UNAVAILABLE = "NoAvailableService"
CODE_SENSITIVE = "OutputVideoSensitiveContentDetected"
CODE_ARTIFACT_MISSING = "ArtifactMissing"


class ApiError(Exception):
    """带 HTTP 状态与方舟码的异常。upstream 只做取证，不是稳定契约。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int,
        *,
        param: str | None = None,
        err_type: str | None = None,
        upstream: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.param = param or ""
        self.err_type = err_type or _type_of(http_status)
        self.upstream = upstream

    def to_body(self) -> dict[str, Any]:
        err: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "type": self.err_type,
        }
        if self.param:
            err["param"] = self.param
        if self.upstream:
            err["upstream"] = self.upstream
        return {"error": err}


def _type_of(status: int) -> str:
    return {
        400: "BadRequest",
        401: "Unauthorized",
        403: "Forbidden",
        404: "NotFound",
        409: "Conflict",
        429: "TooManyRequests",
        500: "InternalServerError",
        501: "NotImplemented",
        502: "BadGateway",
        503: "ServiceUnavailable",
        504: "GatewayTimeout",
    }.get(status, "InternalServerError")
