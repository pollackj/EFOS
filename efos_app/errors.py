"""Application-level exceptions. Messages are safe to show in the UI."""
from __future__ import annotations

class EFOSError(RuntimeError): """Base error for user-visible EFOS failures."""

class ValidationError(EFOSError):
    """The requested mutation violates schema, FSM, or dependency invariants."""

class ConflictError(ValidationError):
    """A mutation would create a logical contradiction or cycle."""

class NotFoundError(EFOSError): """The referenced record no longer exists."""

class ConfigurationProblem(EFOSError): """Configuration or credentials are unusable."""

class ExternalServiceError(EFOSError): """An external API call failed."""

class ReadOnlyQueryError(ValidationError):
    """Generated SQL failed the read-only safety envelope."""

class DatabaseConnectionError(EFOSError):
    """PostgreSQL is unreachable or refused the connection."""

class ProvisioningRequiredError(EFOSError):
    """The configured role cannot create the target database."""

class SchemaValidationError(EFOSError):
    """The target database exists but its EFOS schema is partial or mismatched."""

    def __init__(self, message: str, discrepancies: list[str] | None = None):
        super().__init__(message)
        self.discrepancies = list(discrepancies or [])

    def report(self) -> str:
        if not self.discrepancies: return str(self)
        return str(self) + "\n\n" + "\n".join(f"• {item}" for item in self.discrepancies)

class AuthenticationRequiredError(EFOSError): """Google Calendar OAuth authorization is missing or expired."""
