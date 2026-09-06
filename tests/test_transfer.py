from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from zeroconf import ServiceStateChange

import agent_port.application.transfer as transfer_module
import agent_port.infrastructure.transfer.discovery as discovery_module
from agent_port.application.backup import BackupService
from agent_port.application.transfer import (
    MAX_FAILED_AUTHENTICATIONS,
    TransferReceiveService,
    TransferSendService,
    _PairingSSHServer,
    _PinnedSSHClient,
    _TransferServerState,
    _wait_for_transfer,
)
from agent_port.domain.errors import ArchiveError, TransferError
from agent_port.domain.models import (
    DiscoveryRecord,
    TransferAcknowledgement,
    TransferHeader,
    TransferOffer,
)
from agent_port.infrastructure.archive import AgentPackReader
from agent_port.infrastructure.transfer.discovery import LanAdvertisement, LanDiscovery
from agent_port.infrastructure.transfer.protocol import (
    build_discovery_record,
    decode_pairing_code,
    derive_authentication_password,
    derive_host_key,
    encode_frame,
    encode_pairing_code,
    generate_pairing_secret,
    is_allowed_peer,
    parse_discovery_properties,
    publish_no_clobber,
    read_frame,
    verify_discovery_record,
)


class _UnavailableDiscovery(LanDiscovery):
    async def advertise(self, record: DiscoveryRecord, port: int) -> LanAdvertisement:
        raise TransferError("synthetic discovery failure")


class _BytesReader:
    def __init__(self, value: bytes) -> None:
        self._value = value

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = len(self._value)
        result, self._value = self._value[:n], self._value[n:]
        return result


class _BlockingReader(_BytesReader):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.blocked = asyncio.Event()

    async def read(self, n: int = -1) -> bytes:
        value = await super().read(n)
        if value:
            return value
        self.blocked.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _BytesWriter:
    def __init__(self) -> None:
        self.value = bytearray()
        self.eof = False

    def write(self, value: bytes) -> None:
        self.value.extend(value)

    async def drain(self) -> None:
        return None

    def write_eof(self) -> None:
        self.eof = True


class _FakeServerProcess:
    def __init__(
        self,
        acknowledgement: bytes,
        *,
        command: str = "agent-port-transfer-v1",
        subsystem: str | None = None,
        term_type: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.subsystem = subsystem
        self.term_type = term_type
        self.env = env or {}
        self.stdin = _BytesReader(acknowledgement)
        self.stdout = _BytesWriter()
        self.exit_status: int | None = None

    def exit(self, status: int) -> None:
        self.exit_status = status


class _FakeClientProcess:
    def __init__(self, payload: bytes, *, acknowledgement_error: bool = False) -> None:
        self.stdout = _BytesReader(payload)
        self.stdin = _BytesWriter()
        self._acknowledgement_error = acknowledgement_error

    async def wait_closed(self) -> None:
        if self._acknowledgement_error:
            raise OSError("synthetic acknowledgement failure")


class _FakeClientConnection:
    def __init__(self, process: _FakeClientProcess) -> None:
        self._process = process

    async def __aenter__(self) -> _FakeClientConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def create_process(self, _command: str, encoding: None) -> _FakeClientProcess:
        assert encoding is None
        return self._process


def _fake_connect(monkeypatch: pytest.MonkeyPatch, process: _FakeClientProcess) -> None:
    def connect(*_args: object, **_kwargs: object) -> _FakeClientConnection:
        return _FakeClientConnection(process)

    monkeypatch.setattr(transfer_module.asyncssh, "connect", connect)


def _client_payload(archive: bytes, *, digest: str | None = None, size: int | None = None) -> bytes:
    header = TransferHeader(
        cli_version="0.5.0",
        archive_size=len(archive) if size is None else size,
        archive_sha256=digest or f"sha256:{hashlib.sha256(archive).hexdigest()}",
    )
    return encode_frame(header) + archive


def test_pairing_code_round_trip_and_key_separation() -> None:
    secret = generate_pairing_secret()
    code = encode_pairing_code(secret)

    assert decode_pairing_code(code.lower().replace("-", " ")) == secret
    assert len(derive_authentication_password(secret)) >= 40
    assert derive_host_key(secret).get_fingerprint() == derive_host_key(secret).get_fingerprint()
    with pytest.raises(TransferError, match="Invalid pairing code"):
        decode_pairing_code("not-a-valid-code")


def test_discovery_record_is_authenticated_and_expires() -> None:
    secret = bytes(range(16))
    now = int(time.time())
    record = build_discovery_record(secret, now + 60)

    assert verify_discovery_record(secret, record, now)
    assert not verify_discovery_record(secret, record, now + 60)
    forged = record.model_copy(update={"authentication_tag": "0" * 64})
    assert not verify_discovery_record(secret, forged, now)
    parsed = parse_discovery_properties(
        {
            b"v": b"1",
            b"sid": record.session_id.encode(),
            b"exp": str(record.expires_at).encode(),
            b"fp": record.host_key_fingerprint.encode(),
            b"tag": record.authentication_tag.encode(),
        }
    )
    assert parsed == record
    with pytest.raises(TransferError, match="Unsupported"):
        parse_discovery_properties(
            {
                b"v": b"2",
                b"sid": record.session_id.encode(),
                b"exp": str(record.expires_at).encode(),
                b"fp": record.host_key_fingerprint.encode(),
                b"tag": record.authentication_tag.encode(),
            }
        )


def test_zeroconf_advertisement_and_discovery_are_mocked_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = bytes(range(16))
    record = build_discovery_record(secret, int(time.time()) + 60)

    class FakeAsyncZeroconf:
        info: object | None = None

        def __init__(self, **_kwargs: object) -> None:
            self.zeroconf = object()
            self.closed = False

        async def async_register_service(self, info: object) -> None:
            FakeAsyncZeroconf.info = info

        async def async_unregister_service(self, _info: object) -> None:
            return None

        async def async_close(self) -> None:
            self.closed = True

        async def async_get_service_info(
            self, _service_type: str, _name: str, timeout: int
        ) -> object | None:
            assert timeout == 1000
            return FakeAsyncZeroconf.info

    class FakeBrowser:
        def __init__(
            self,
            zeroconf: object,
            service_type: str,
            handlers: list[object],
        ) -> None:
            assert zeroconf is not None
            handler = handlers[0]
            assert callable(handler)
            info = FakeAsyncZeroconf.info
            assert info is not None
            handler(
                zeroconf=zeroconf,
                service_type=service_type,
                name=info.name,
                state_change=ServiceStateChange.Added,
            )

        async def async_cancel(self) -> None:
            return None

    monkeypatch.setattr(discovery_module, "AsyncZeroconf", FakeAsyncZeroconf)
    monkeypatch.setattr(discovery_module, "AsyncServiceBrowser", FakeBrowser)
    monkeypatch.setattr(discovery_module, "local_ipv4_addresses", lambda: ("192.168.1.20",))

    async def scenario() -> None:
        service = LanDiscovery()
        advertisement = await service.advertise(record, 43210)
        assert advertisement.addresses == ("192.168.1.20",)
        endpoint = await service.discover(secret, 0.01)
        assert endpoint.hosts == ("192.168.1.20",)
        assert endpoint.port == 43210
        assert endpoint.record == record
        await advertisement.close()

    asyncio.run(scenario())


def test_discovery_rejects_ambiguous_authenticated_advertisements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = bytes(range(16))
    record = build_discovery_record(secret, int(time.time()) + 60)
    properties = {
        b"v": b"1",
        b"sid": record.session_id.encode(),
        b"exp": str(record.expires_at).encode(),
        b"fp": record.host_key_fingerprint.encode(),
        b"tag": record.authentication_tag.encode(),
    }

    class FakeInfo:
        def __init__(self, name: str, address: str) -> None:
            self.name = name
            self.properties = properties
            self.port = 43210
            self._address = address

        def parsed_scoped_addresses(self) -> list[str]:
            return [self._address]

    infos = {
        "first._agent-port._tcp.local.": FakeInfo("first._agent-port._tcp.local.", "192.168.1.20"),
        "second._agent-port._tcp.local.": FakeInfo(
            "second._agent-port._tcp.local.", "192.168.1.21"
        ),
    }

    class FakeAsyncZeroconf:
        def __init__(self, **_kwargs: object) -> None:
            self.zeroconf = object()

        async def async_get_service_info(
            self, _service_type: str, name: str, timeout: int
        ) -> FakeInfo:
            assert timeout == 1000
            return infos[name]

        async def async_close(self) -> None:
            return None

    class FakeBrowser:
        def __init__(
            self,
            zeroconf: object,
            service_type: str,
            handlers: list[object],
        ) -> None:
            assert zeroconf is not None
            handler = handlers[0]
            assert callable(handler)
            for name in infos:
                handler(
                    zeroconf=zeroconf,
                    service_type=service_type,
                    name=name,
                    state_change=ServiceStateChange.Added,
                )

        async def async_cancel(self) -> None:
            return None

    monkeypatch.setattr(discovery_module, "AsyncZeroconf", FakeAsyncZeroconf)
    monkeypatch.setattr(discovery_module, "AsyncServiceBrowser", FakeBrowser)

    with pytest.raises(TransferError, match="Multiple transfers"):
        asyncio.run(LanDiscovery().discover(secret, 0.01))


def test_transfer_models_and_peer_scope_are_strict() -> None:
    assert is_allowed_peer("127.0.0.1")
    assert is_allowed_peer("192.168.1.20")
    assert is_allowed_peer("fe80::1%en0")
    assert not is_allowed_peer("8.8.8.8")
    assert not is_allowed_peer("100.64.0.1")
    assert not is_allowed_peer("192.0.2.1")
    assert not is_allowed_peer("old-machine.example")
    with pytest.raises(ValidationError):
        TransferHeader(
            protocol_version=2,
            cli_version="0.5.0",
            archive_size=1,
            archive_sha256=f"sha256:{'0' * 64}",
        )
    with pytest.raises(ValidationError):
        TransferHeader(
            cli_version="0.5",
            archive_size=1,
            archive_sha256=f"sha256:{'0' * 64}",
        )
    with pytest.raises(ValidationError):
        TransferHeader(
            cli_version="0.5.0",
            archive_size=1,
            archive_sha256=f"sha256:{'0' * 64}",
            unexpected=True,
        )


def test_client_pins_the_host_key_and_rejects_public_peers() -> None:
    secret = bytes(range(16))
    expected_key = derive_host_key(secret)
    client = _PinnedSSHClient(expected_key.get_fingerprint("sha256"))

    assert client.validate_host_public_key("source", "192.168.1.20", 43210, expected_key)
    assert not client.validate_host_public_key(
        "source", "192.168.1.20", 43210, derive_host_key(bytes(reversed(range(16))))
    )
    assert not client.validate_host_public_key("source", "8.8.8.8", 43210, expected_key)


def test_protocol_frames_reject_truncation() -> None:
    header = TransferHeader(
        cli_version="0.5.0",
        archive_size=3,
        archive_sha256=f"sha256:{'0' * 64}",
    )

    assert asyncio.run(read_frame(_BytesReader(encode_frame(header)), TransferHeader)) == header
    with pytest.raises(TransferError, match="before all expected bytes"):
        asyncio.run(read_frame(_BytesReader(encode_frame(header)[:-1]), TransferHeader))


def test_server_stops_password_authentication_after_five_failures(tmp_path: Path) -> None:
    archive = tmp_path / "source.agentpack"
    archive.write_bytes(b"synthetic")
    header = TransferHeader(
        cli_version="0.5.0",
        archive_size=archive.stat().st_size,
        archive_sha256=f"sha256:{'0' * 64}",
    )

    async def scenario() -> None:
        state = _TransferServerState(archive, header, "correct", int(time.time()) + 60)
        server = _PairingSSHServer(state)
        for _ in range(MAX_FAILED_AUTHENTICATIONS):
            assert not server.validate_password("agent-port", "wrong")
        assert state.auth_limit_reached.is_set()
        assert not server.password_auth_supported()
        with pytest.raises(TransferError, match="five failed"):
            await _wait_for_transfer(state, 1)

    asyncio.run(scenario())


def test_server_rejects_shell_subsystem_pty_environment_and_concurrency(tmp_path: Path) -> None:
    archive = tmp_path / "source.agentpack"
    archive.write_bytes(b"synthetic")
    header = TransferHeader(
        cli_version="0.5.0",
        archive_size=archive.stat().st_size,
        archive_sha256=f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}",
    )

    async def scenario() -> None:
        state = _TransferServerState(archive, header, "correct", int(time.time()) + 60)
        restricted = (
            _FakeServerProcess(b"", command=""),
            _FakeServerProcess(b"", subsystem="sftp"),
            _FakeServerProcess(b"", term_type="xterm"),
            _FakeServerProcess(b"", env={"LANG": "en_US.UTF-8"}),
        )
        for process in restricted:
            await state.handle_process(process)  # type: ignore[arg-type]
            assert process.exit_status == 126

        state._active = True
        concurrent = _FakeServerProcess(b"")
        await state.handle_process(concurrent)  # type: ignore[arg-type]
        assert concurrent.exit_status == 75
        state._active = False

    asyncio.run(scenario())


def test_failed_acknowledgement_allows_retry_and_success_is_single_use(tmp_path: Path) -> None:
    archive = tmp_path / "source.agentpack"
    archive.write_bytes(b"synthetic")
    digest = f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"
    header = TransferHeader(
        cli_version="0.5.0",
        archive_size=archive.stat().st_size,
        archive_sha256=digest,
    )
    valid = encode_frame(
        TransferAcknowledgement(
            cli_version="0.5.0",
            archive_size=archive.stat().st_size,
            archive_sha256=digest,
        )
    )
    invalid = encode_frame(
        TransferAcknowledgement(
            cli_version="0.5.0",
            archive_size=archive.stat().st_size,
            archive_sha256=f"sha256:{'0' * 64}",
        )
    )

    async def scenario() -> None:
        state = _TransferServerState(archive, header, "correct", int(time.time()) + 60)
        failed = _FakeServerProcess(invalid)
        await state.handle_process(failed)  # type: ignore[arg-type]
        assert failed.exit_status == 1
        assert not state.completed.is_set()

        retried = _FakeServerProcess(valid)
        await state.handle_process(retried)  # type: ignore[arg-type]
        assert retried.exit_status == 0
        assert state.completed.is_set()

        replay = _FakeServerProcess(valid)
        await state.handle_process(replay)  # type: ignore[arg-type]
        assert replay.exit_status == 75

    asyncio.run(scenario())


def test_wait_for_transfer_reports_timeout_and_unverified_completion(tmp_path: Path) -> None:
    archive = tmp_path / "source.agentpack"
    archive.write_bytes(b"synthetic")
    header = TransferHeader(
        cli_version="0.5.0",
        archive_size=archive.stat().st_size,
        archive_sha256=f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}",
    )

    async def scenario() -> None:
        state = _TransferServerState(archive, header, "correct", int(time.time()) + 60)
        with pytest.raises(TransferError, match="expired"):
            await _wait_for_transfer(state, 0)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("fixture_name", "expected_harness"),
    [("codex_home", "codex"), ("claude_home", "claude-code")],
)
def test_localhost_transfer_is_verified_and_does_not_restore(
    fixture_name: str,
    expected_harness: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    harness_home: Path = request.getfixturevalue(fixture_name)
    output = tmp_path / "received.agentpack"
    offers: list[TransferOffer] = []
    ready = asyncio.Event()
    sender = TransferSendService(discovery=_UnavailableDiscovery())
    receiver = TransferReceiveService(discovery=_UnavailableDiscovery())

    def on_ready(offer: TransferOffer) -> None:
        offers.append(offer)
        ready.set()

    async def scenario() -> None:
        send_task = asyncio.create_task(
            sender._execute_async(
                harness_home,
                "auto",
                frozenset({"sessions", "skills"}),
                None,
                30,
                on_ready,
                "127.0.0.1",
            )
        )
        ready_task = asyncio.create_task(ready.wait())
        done, _ = await asyncio.wait(
            {ready_task, send_task}, timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        if send_task in done:
            try:
                await send_task
            except TransferError as error:
                if "operation not permitted" in str(error).lower() and not os.environ.get("CI"):
                    pytest.skip("sandbox does not permit loopback listeners")
                raise
        if ready_task not in done:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
            pytest.fail("LAN transfer server did not become ready")
        offer = offers[0]
        with pytest.raises(TransferError, match="Pairing code does not match this sender"):
            await receiver._execute_async(
                output,
                encode_pairing_code(bytes(reversed(range(16)))),
                "127.0.0.1",
                offer.port,
                0.1,
            )
        receipt = await receiver._execute_async(
            output,
            offer.pairing_code,
            "127.0.0.1",
            offer.port,
            0.1,
        )
        sent = await asyncio.wait_for(send_task, timeout=10)
        assert receipt.archive_sha256 == sent.archive_sha256

    asyncio.run(scenario())

    report = AgentPackReader().inspect(output)
    assert report.manifest.harness.value == expected_harness
    assert not (harness_home.parent / ".agent-port-runs").exists()
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600


def test_receive_refuses_invalid_output_before_network(tmp_path: Path) -> None:
    code = encode_pairing_code(bytes(range(16)))
    service = TransferReceiveService(discovery=_UnavailableDiscovery())

    with pytest.raises(TransferError, match="extension"):
        service.execute(tmp_path / "archive.zip", code, "127.0.0.1", 12345)
    existing = tmp_path / "existing.agentpack"
    existing.write_bytes(b"owned")
    with pytest.raises(TransferError, match="overwrite"):
        service.execute(existing, code, "127.0.0.1", 12345)
    assert existing.read_bytes() == b"owned"


def test_receive_publishes_mode_0600_and_sends_acknowledgement(
    codex_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, source)
    process = _FakeClientProcess(_client_payload(source.read_bytes()))
    _fake_connect(monkeypatch, process)
    output = tmp_path / "received.agentpack"
    created_modes: list[int] = []
    real_fdopen = os.fdopen

    def tracked_fdopen(descriptor: int, mode: str) -> object:
        created_modes.append(os.fstat(descriptor).st_mode & 0o777)
        return real_fdopen(descriptor, mode)

    monkeypatch.setattr(transfer_module.os, "fdopen", tracked_fdopen)

    result = asyncio.run(
        TransferReceiveService()._receive_from_host(
            output,
            "127.0.0.1",
            43210,
            bytes(range(16)),
            derive_host_key(bytes(range(16))).get_fingerprint("sha256"),
        )
    )

    assert output.read_bytes() == source.read_bytes()
    assert len(created_modes) == 1
    if os.name != "nt":
        assert created_modes == [0o600]
        assert output.stat().st_mode & 0o777 == 0o600
    assert result.archive_sha256 == f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"
    acknowledgement = asyncio.run(
        read_frame(_BytesReader(bytes(process.stdin.value)), TransferAcknowledgement)
    )
    assert acknowledgement.archive_size == source.stat().st_size
    assert process.stdin.eof
    assert not list(tmp_path.glob(".*.partial"))


@pytest.mark.parametrize("failure", ["truncated", "altered-hash", "corrupt-archive"])
def test_receive_rejects_bad_payloads_and_removes_partial_files(
    failure: str,
    codex_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, source)
    archive = source.read_bytes()
    if failure == "truncated":
        payload = _client_payload(archive, size=len(archive) + 10)
        expected_error: type[Exception] = TransferError
    elif failure == "altered-hash":
        payload = _client_payload(archive, digest=f"sha256:{'0' * 64}")
        expected_error = TransferError
    else:
        archive = b"not-an-agentpack"
        payload = _client_payload(archive)
        expected_error = ArchiveError
    _fake_connect(monkeypatch, _FakeClientProcess(payload))
    output = tmp_path / "received.agentpack"

    with pytest.raises(expected_error):
        asyncio.run(
            TransferReceiveService()._receive_from_host(
                output,
                "127.0.0.1",
                43210,
                bytes(range(16)),
                derive_host_key(bytes(range(16))).get_fingerprint("sha256"),
            )
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".*.partial"))


def test_destination_collision_during_publication_preserves_existing_file(
    codex_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, source)
    _fake_connect(monkeypatch, _FakeClientProcess(_client_payload(source.read_bytes())))
    output = tmp_path / "received.agentpack"

    def collide(partial: Path, destination: Path) -> None:
        destination.write_bytes(b"owned")
        publish_no_clobber(partial, destination)

    monkeypatch.setattr(transfer_module, "publish_no_clobber", collide)
    with pytest.raises(TransferError, match="overwrite"):
        asyncio.run(
            TransferReceiveService()._receive_from_host(
                output,
                "127.0.0.1",
                43210,
                bytes(range(16)),
                derive_host_key(bytes(range(16))).get_fingerprint("sha256"),
            )
        )

    assert output.read_bytes() == b"owned"
    assert not list(tmp_path.glob(".*.partial"))


def test_acknowledgement_transport_failure_keeps_verified_output(
    codex_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, source)
    process = _FakeClientProcess(_client_payload(source.read_bytes()), acknowledgement_error=True)
    _fake_connect(monkeypatch, process)
    output = tmp_path / "received.agentpack"

    result = asyncio.run(
        TransferReceiveService()._receive_from_host(
            output,
            "127.0.0.1",
            43210,
            bytes(range(16)),
            derive_host_key(bytes(range(16))).get_fingerprint("sha256"),
        )
    )

    assert output.exists()
    assert result.archived_bytes == source.stat().st_size
    assert not list(tmp_path.glob(".*.partial"))


def test_cancelled_receive_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = TransferHeader(
        cli_version="0.5.0",
        archive_size=10,
        archive_sha256=f"sha256:{'0' * 64}",
    )
    reader = _BlockingReader(encode_frame(header))
    process = _FakeClientProcess(b"")
    process.stdout = reader
    _fake_connect(monkeypatch, process)
    output = tmp_path / "received.agentpack"

    async def scenario() -> None:
        task = asyncio.create_task(
            TransferReceiveService()._receive_from_host(
                output,
                "127.0.0.1",
                43210,
                bytes(range(16)),
                derive_host_key(bytes(range(16))).get_fingerprint("sha256"),
            )
        )
        await asyncio.wait_for(reader.blocked.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not output.exists()
    assert not list(tmp_path.glob(".*.partial"))
