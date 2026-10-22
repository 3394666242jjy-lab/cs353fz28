"""Safe business errors for B's worker; never expose provider bodies or private inputs."""


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.trace_id: str | None = None

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "code": self.code,
            "message": self.safe_message,
            "retryable": self.retryable,
            "trace_id": self.trace_id,
        }
