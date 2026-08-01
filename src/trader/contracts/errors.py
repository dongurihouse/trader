"""Exceptions raised at trader contract boundaries."""


class LookaheadError(Exception):
    """A request would require data after its as-of time."""


class ContractViolation(Exception):
    """Schema or field misuse was detected at a boundary."""


class BrokerNotConfigured(Exception):
    """The API broker lacks required wiring or a live-order interlock."""


__all__ = ["BrokerNotConfigured", "ContractViolation", "LookaheadError"]
