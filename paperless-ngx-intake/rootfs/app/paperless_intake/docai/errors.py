"""Exceptions raised by the OCR, validation and rendering steps."""


class ParserError(RuntimeError):
    pass


class ConfigurationError(ParserError):
    pass


class CompatibilityError(ParserError):
    pass


class IntegrityError(ParserError):
    pass


class ResultValidationError(ParserError):
    pass
