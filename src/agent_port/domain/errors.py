class AgentPortError(Exception):
    """Base class for expected, user-facing failures."""


class DetectionError(AgentPortError):
    """Raised when a harness cannot be selected safely."""


class InspectionError(AgentPortError):
    """Raised when a source cannot be inspected safely."""


class BackupError(AgentPortError):
    """Raised when a backup cannot be completed safely."""


class ArchiveError(AgentPortError):
    """Raised when an archive is unsafe, malformed, or corrupt."""


class TransferError(AgentPortError):
    """Raised when an archive transfer cannot be completed safely."""


class RestoreError(AgentPortError):
    """Raised when restore planning, application, or rollback cannot proceed safely."""


class VerificationError(AgentPortError):
    """Raised when a destination fails post-restore validation."""
