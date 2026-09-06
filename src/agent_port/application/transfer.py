from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import os
import socket
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import asyncssh

from agent_port import __version__
from agent_port.application.backup import BackupService
from agent_port.domain.errors import TransferError
from agent_port.domain.models import (
    TransferAcknowledgement,
    TransferHeader,
    TransferOffer,
    TransferReceiveResult,
    TransferSendResult,
)
from agent_port.infrastructure.archive import AgentPackReader
from agent_port.infrastructure.transfer.discovery import (
    LanAdvertisement,
    LanDiscovery,
    local_ipv4_addresses,
)
from agent_port.infrastructure.transfer.protocol import (
    TRANSFER_COMMAND,
    build_discovery_record,
    decode_pairing_code,
    derive_authentication_password,
    derive_host_key,
    encode_frame,
    encode_pairing_code,
    generate_pairing_secret,
    is_allowed_peer,
    publish_no_clobber,
    read_frame,
    sha256_file,
    write_frame,
)

MAX_FAILED_AUTHENTICATIONS = 5
DEFAULT_DISCOVERY_TIMEOUT = 8.0
TRANSFER_USERNAME = "agent-port"


class TransferSendService:
    def __init__(
        self,
        backup_service: BackupService | None = None,
        discovery: LanDiscovery | None = None,
    ) -> None:
        self._backup_service = backup_service or BackupService()
        self._discovery = discovery or LanDiscovery()

    def execute(
        self,
        source: Path,
        requested_harness: str = "auto",
        include: frozenset[str] = frozenset({"sessions", "skills"}),
        user_home: Path | None = None,
        timeout: int = 600,
        on_ready: Callable[[TransferOffer], None] | None = None,
    ) -> TransferSendResult:
        if not 30 <= timeout <= 3600:
            raise TransferError("Transfer timeout must be between 30 and 3600 seconds.")
        return asyncio.run(
            self._execute_async(
                source,
                requested_harness,
                include,
                user_home,
                timeout,
                on_ready,
                "0.0.0.0",
            )
        )

    async def _execute_async(
        self,
        source: Path,
        requested_harness: str,
        include: frozenset[str],
        user_home: Path | None,
        timeout: int,
        on_ready: Callable[[TransferOffer], None] | None,
        bind_host: str = "0.0.0.0",
    ) -> TransferSendResult:
        with tempfile.TemporaryDirectory(prefix="agent-port-transfer-") as temporary:
            temporary_root = Path(temporary)
            archive = temporary_root / "transfer.agentpack"
            backup = self._backup_service.execute(
                source, archive, requested_harness, include, user_home
            )
            os.chmod(archive, 0o600)
            archive_sha256 = sha256_file(archive)
            secret = generate_pairing_secret()
            expires_at = int(time.time()) + timeout
            header = TransferHeader(
                cli_version=__version__,
                archive_size=archive.stat().st_size,
                archive_sha256=archive_sha256,
            )
            state = _TransferServerState(
                archive=archive,
                header=header,
                authentication_password=derive_authentication_password(secret),
                expires_at=expires_at,
            )
            listener: asyncssh.SSHAcceptor | None = None
            advertisement: LanAdvertisement | None = None
            try:
                listener = await asyncssh.listen(
                    bind_host,
                    0,
                    family=socket.AF_INET,
                    server_factory=lambda: _PairingSSHServer(state),
                    server_host_keys=[derive_host_key(secret)],
                    process_factory=state.handle_process,
                    encoding=None,
                    config=None,
                    allow_pty=False,
                    agent_forwarding=False,
                    x11_forwarding=False,
                    server_version=f"AgentPort_{__version__}",
                )
                port = listener.get_port()
                if not port:
                    raise TransferError("Could not select a single LAN transfer port.")
                discovery_available = True
                record = build_discovery_record(secret, expires_at)
                try:
                    advertisement = await self._discovery.advertise(record, port)
                    hosts = list(advertisement.addresses)
                except TransferError:
                    discovery_available = False
                    hosts = list(local_ipv4_addresses())
                if not hosts:
                    hosts = [socket.gethostname()]
                if on_ready is not None:
                    on_ready(
                        TransferOffer(
                            pairing_code=encode_pairing_code(secret),
                            hosts=hosts,
                            port=port,
                            expires_at=expires_at,
                            discovery_available=discovery_available,
                        )
                    )
                await _wait_for_transfer(state, timeout)
            except (OSError, asyncssh.Error) as error:
                raise TransferError(f"Could not serve the LAN transfer: {error}") from error
            finally:
                if advertisement is not None:
                    with contextlib.suppress(Exception):
                        await advertisement.close()
                if listener is not None:
                    listener.close()
                    await listener.wait_closed()
                await state.close_connections()
            return TransferSendResult(
                archived_bytes=header.archive_size,
                archive_sha256=archive_sha256,
                content=backup.manifest.counts,
            )


class TransferReceiveService:
    def __init__(
        self,
        reader: AgentPackReader | None = None,
        discovery: LanDiscovery | None = None,
    ) -> None:
        self._reader = reader or AgentPackReader()
        self._discovery = discovery or LanDiscovery()

    def execute(
        self,
        output: Path,
        pairing_code: str,
        host: str | None = None,
        port: int | None = None,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
    ) -> TransferReceiveResult:
        return asyncio.run(self._execute_async(output, pairing_code, host, port, discovery_timeout))

    async def _execute_async(
        self,
        output: Path,
        pairing_code: str,
        host: str | None,
        port: int | None,
        discovery_timeout: float,
    ) -> TransferReceiveResult:
        output = output.expanduser().resolve()
        if output.suffix != ".agentpack":
            raise TransferError("Transfer output must use the .agentpack extension.")
        if output.exists():
            raise TransferError(f"Refusing to overwrite existing output: {output}")
        if (host is None) != (port is None):
            raise TransferError("--host and --port must be provided together.")
        if port is not None and not 1 <= port <= 65535:
            raise TransferError("Transfer port must be between 1 and 65535.")
        if discovery_timeout <= 0 or discovery_timeout > 60:
            raise TransferError(
                "Discovery timeout must be greater than 0 and no more than 60 seconds."
            )
        secret = decode_pairing_code(pairing_code)
        expected_fingerprint = derive_host_key(secret).get_fingerprint("sha256")
        if host is None:
            endpoint = await self._discovery.discover(secret, discovery_timeout)
            hosts = endpoint.hosts
            selected_port = endpoint.port
        else:
            hosts = (host,)
            assert port is not None
            selected_port = port
        output.parent.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for candidate in hosts:
            try:
                return await self._receive_from_host(
                    output,
                    candidate,
                    selected_port,
                    secret,
                    expected_fingerprint,
                )
            except TransferError as error:
                errors.append(f"{candidate}: {error}")
        detail = "; ".join(errors)
        raise TransferError(
            f"Could not receive the archive from any advertised address ({detail})."
        )

    async def _receive_from_host(
        self,
        output: Path,
        host: str,
        port: int,
        secret: bytes,
        expected_fingerprint: str,
    ) -> TransferReceiveResult:
        partial = output.parent / f".{output.name}.{uuid.uuid4().hex}.partial"
        try:
            try:
                async with asyncssh.connect(
                    host,
                    port,
                    username=TRANSFER_USERNAME,
                    password=derive_authentication_password(secret),
                    client_factory=lambda: _PinnedSSHClient(expected_fingerprint),
                    known_hosts=[],
                    client_keys=[],
                    preferred_auth="password",
                    public_key_auth=False,
                    kbdint_auth=False,
                    host_based_auth=False,
                    agent_forwarding=False,
                    x11_forwarding=False,
                    config=None,
                    connect_timeout=10,
                    login_timeout=10,
                    client_version=f"AgentPort_{__version__}",
                ) as connection:
                    process = await connection.create_process(TRANSFER_COMMAND, encoding=None)
                    header = await read_frame(process.stdout, TransferHeader)
                    _require_compatible_cli(header.cli_version)
                    digest = hashlib.sha256()
                    remaining = header.archive_size
                    descriptor = os.open(
                        partial,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    with os.fdopen(descriptor, "wb") as destination:
                        while remaining:
                            chunk = await process.stdout.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise TransferError(
                                    "Transfer ended before the complete archive was received."
                                )
                            destination.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                        destination.flush()
                        os.fsync(destination.fileno())
                    if await process.stdout.read(1):
                        raise TransferError("Transfer sent unexpected bytes after the archive.")
                    actual_sha256 = f"sha256:{digest.hexdigest()}"
                    if not hmac.compare_digest(actual_sha256, header.archive_sha256):
                        raise TransferError("Received archive SHA-256 does not match its header.")
                    report = self._reader.inspect(partial)
                    publish_no_clobber(partial, output)
                    acknowledgement = TransferAcknowledgement(
                        cli_version=__version__,
                        archive_size=header.archive_size,
                        archive_sha256=header.archive_sha256,
                    )
                    try:
                        process.stdin.write(encode_frame(acknowledgement))
                        process.stdin.write_eof()
                        await asyncio.wait_for(process.wait_closed(), timeout=5)
                    except (OSError, asyncssh.Error, TimeoutError):
                        pass
                    return TransferReceiveResult(
                        output=str(output),
                        archived_bytes=header.archive_size,
                        archive_sha256=header.archive_sha256,
                        harness=report.manifest.harness.value,
                        content=report.manifest.counts,
                    )
            except TransferError:
                raise
            except asyncssh.HostKeyNotVerifiable as error:
                raise TransferError(
                    "Pairing code does not match this sender. Copy the complete current code "
                    "from the source computer and check the host and port. Keep the source "
                    "transfer running and retry; no account password or SSH setup is needed."
                ) from error
            except asyncssh.PermissionDenied as error:
                raise TransferError(
                    "Pairing authentication was rejected. Check that the source transfer is "
                    "still running and use its current pairing code. If it expired or stopped, "
                    "start transfer send again and use the new code; no account password is needed."
                ) from error
            except (OSError, asyncssh.Error) as error:
                raise TransferError(f"Secure LAN connection failed: {error}") from error
        finally:
            partial.unlink(missing_ok=True)


class _PinnedSSHClient(asyncssh.SSHClient):
    def __init__(self, expected_fingerprint: str) -> None:
        self._expected_fingerprint = expected_fingerprint

    def validate_host_public_key(
        self,
        _host: str,
        address: str,
        _port: int,
        key: asyncssh.SSHKey,
    ) -> bool:
        return is_allowed_peer(address) and hmac.compare_digest(
            key.get_fingerprint("sha256"), self._expected_fingerprint
        )


class _TransferServerState:
    def __init__(
        self,
        archive: Path,
        header: TransferHeader,
        authentication_password: str,
        expires_at: int,
    ) -> None:
        self.archive = archive
        self.header = header
        self.authentication_password = authentication_password
        self.expires_at = expires_at
        self.failed_authentications = 0
        self.completed = asyncio.Event()
        self.auth_limit_reached = asyncio.Event()
        self.connections: set[asyncssh.SSHServerConnection] = set()
        self._active = False
        self._active_guard = asyncio.Lock()

    async def handle_process(self, process: asyncssh.SSHServerProcess[bytes]) -> None:
        if (
            process.command != TRANSFER_COMMAND
            or process.subsystem
            or process.term_type
            or process.env
        ):
            process.exit(126)
            return
        async with self._active_guard:
            if self._active or self.completed.is_set():
                process.exit(75)
                return
            self._active = True
        try:
            write_frame(process.stdout, self.header)
            with self.archive.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    process.stdout.write(chunk)
                    await process.stdout.drain()
            process.stdout.write_eof()
            acknowledgement = await read_frame(process.stdin, TransferAcknowledgement)
            _require_compatible_cli(acknowledgement.cli_version)
            if acknowledgement.archive_size != self.header.archive_size or not hmac.compare_digest(
                acknowledgement.archive_sha256, self.header.archive_sha256
            ):
                raise TransferError("Receiver acknowledgement does not match the archive.")
            self.completed.set()
            process.exit(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            process.exit(1)
        finally:
            async with self._active_guard:
                self._active = False

    async def close_connections(self) -> None:
        connections = tuple(self.connections)
        for connection in connections:
            connection.close()
        if connections:
            await asyncio.gather(
                *(connection.wait_closed() for connection in connections),
                return_exceptions=True,
            )


class _PairingSSHServer(asyncssh.SSHServer):
    def __init__(self, state: _TransferServerState) -> None:
        self._state = state
        self._connection: asyncssh.SSHServerConnection | None = None

    def connection_made(self, connection: asyncssh.SSHServerConnection) -> None:
        self._connection = connection
        peer = connection.get_extra_info("peername")
        address = peer[0] if isinstance(peer, tuple) and peer else ""
        if not isinstance(address, str) or not is_allowed_peer(address):
            connection.close()
            return
        self._state.connections.add(connection)

    def connection_lost(self, _error: Exception | None) -> None:
        if self._connection is not None:
            self._state.connections.discard(self._connection)

    def begin_auth(self, _username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return self._state.failed_authentications < MAX_FAILED_AUTHENTICATIONS

    def validate_password(self, username: str, password: str) -> bool:
        valid = (
            int(time.time()) < self._state.expires_at
            and username == TRANSFER_USERNAME
            and hmac.compare_digest(password, self._state.authentication_password)
        )
        if valid:
            return True
        self._state.failed_authentications += 1
        if self._state.failed_authentications >= MAX_FAILED_AUTHENTICATIONS:
            self._state.auth_limit_reached.set()
        return False


async def _wait_for_transfer(state: _TransferServerState, timeout: int) -> None:
    completed = asyncio.create_task(state.completed.wait())
    auth_limit = asyncio.create_task(state.auth_limit_reached.wait())
    try:
        done, pending = await asyncio.wait(
            {completed, auth_limit}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if not done:
            raise TransferError("Pairing code expired before the transfer completed.")
        if auth_limit in done and auth_limit.result():
            raise TransferError("Transfer stopped after five failed authentication attempts.")
        if completed not in done or not completed.result():
            raise TransferError("Transfer ended without receiver verification.")
    finally:
        for task in (completed, auth_limit):
            if not task.done():
                task.cancel()
        await asyncio.gather(completed, auth_limit, return_exceptions=True)


def _require_compatible_cli(value: str) -> None:
    expected = __version__.split(".")[:2]
    actual = value.split(".")[:2]
    if actual != expected:
        raise TransferError(
            f"Transfer peer uses incompatible Agent Port {value}; expected {'.'.join(expected)}.x."
        )
