"""Small, deliberately sanitized errors shared by the isolated finance API."""


class FinanceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 503,
                 retryable: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.uncertain = uncertain

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "uncertain": self.uncertain}
