class DomainError(Exception):
    """A domain invariant was violated."""


class InvalidOrder(DomainError):
    pass


class InvalidCoordinates(DomainError):
    pass


class InvalidStateTransition(DomainError):
    pass
