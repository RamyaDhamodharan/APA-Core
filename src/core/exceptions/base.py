class APException(Exception):
    """Base exception for AP Core service."""

    def __init__(self, message: str, code: str = "INTERNAL_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code


class EntityNotFoundException(APException):
    def __init__(self, entity: str, identifier: str):
        super().__init__(f"{entity} with id '{identifier}' not found.", code="NOT_FOUND")


class ValidationException(APException):
    def __init__(self, message: str):
        super().__init__(message, code="VALIDATION_ERROR")


class MatchingException(APException):
    def __init__(self, message: str):
        super().__init__(message, code="MATCHING_ERROR")
