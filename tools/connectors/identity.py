"""Host-bound personal connector identity, inherited by delegated tool threads.

A bound resolver is authoritative, including a missing credential. Multi-user
hosts must never fall back to a bot's or the installation owner's account.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class _PersonalIdentity:
    resolver: Callable[[], str | None]
    authorize_connection: Callable[[Sequence[str]], None] | None = None


_identity: ContextVar[_PersonalIdentity | None] = ContextVar("connector_identity", default=None)


def bind_connector_identity(
    resolver: Callable[[], str | None], *,
    authorize_connection: Callable[[Sequence[str]], None] | None = None,
):
    """Bind server-owned credentials and policy with one reset token.

    The optional policy callback raises to refuse a connection mutation. Legacy
    single-user callers can keep binding only their credential resolver.
    """
    return _identity.set(_PersonalIdentity(resolver, authorize_connection))


def reset_connector_identity(token) -> None:
    _identity.reset(token)


def personal_connector_bound() -> bool:
    return _identity.get() is not None


def personal_connector_token() -> tuple[bool, str | None]:
    identity = _identity.get()
    if identity is None:
        return False, None
    value = identity.resolver()
    return True, value.strip() if isinstance(value, str) and value.strip() else None


def authorize_personal_connection(connectors: Sequence[str]) -> None:
    identity = _identity.get()
    if identity is not None and identity.authorize_connection is not None:
        identity.authorize_connection(tuple(connectors))
