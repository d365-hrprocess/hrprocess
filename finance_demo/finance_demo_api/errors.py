"""Small, deliberately sanitized errors shared by the isolated finance API."""


class FinanceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 503,
                 retryable: bool = False, uncertain: bool = False, *,
                 cause_code: str | None = None, upstream_status_code: int | None = None,
                 stage: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.uncertain = uncertain
        self.cause_code = cause_code
        self.upstream_status_code = upstream_status_code
        self.stage = stage

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "uncertain": self.uncertain,
                **{k: v for k, v in {"cause_code": self.cause_code,
                   "upstream_status_code": self.upstream_status_code, "stage": self.stage}.items() if v is not None}}
