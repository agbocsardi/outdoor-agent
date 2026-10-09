"""Public, redacted failures shared by services and the CLI."""


class OutdoorError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = 2,
        *,
        provider: str | None = None,
        retryable: bool = False,
        suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.provider = provider
        self.retryable = retryable
        self.suggestions = suggestions or []
