class IntegrationError(Exception):
    """Safe integration failure; never include provider payloads or credentials."""

    def __init__(self, message="Integration unavailable", status_code=502, retryable=True, *, diagnostics=None, log_diagnostics=None):
        super().__init__(message)
        self.detail = message
        self.status_code = status_code
        self.retryable = retryable
        self.diagnostics = diagnostics
        self.log_diagnostics = log_diagnostics


class ConfigurationError(IntegrationError):
    def __init__(self, message="Required integration is not configured", status_code=503, retryable=False):
        super().__init__(message, status_code, retryable)


class ResumeValidationError(ValueError):
    pass
