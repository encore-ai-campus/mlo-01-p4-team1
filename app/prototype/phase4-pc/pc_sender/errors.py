"""Public error codes deliberately omit payloads, tokens, URLs and raw exceptions."""


class SenderError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ValidationError(SenderError):
    pass


class RetryableTaskError(SenderError):
    """A temporary problem with one job, rather than a rejected input."""


class Conflict(SenderError):
    pass


class TransportError(SenderError):
    pass
