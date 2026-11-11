class OperationError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def require(condition, code, message, status=400, details=None):
    if not condition:
        raise OperationError(code, message, status, details)
