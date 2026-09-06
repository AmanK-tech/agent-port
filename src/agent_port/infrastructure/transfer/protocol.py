from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, TypeVar

import asyncssh
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ValidationError

from agent_port.domain.errors import TransferError
from agent_port.domain.models import DiscoveryRecord
from agent_port.domain.models.common import StrictModel

TRANSFER_PROTOCOL_VERSION = 1
TRANSFER_COMMAND = "agent-port-transfer-v1"
MAX_FRAME_SIZE = 64 * 1024
PAIRING_SECRET_BYTES = 16
PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class AsyncByteReader(Protocol):
    async def read(self, n: int = -1) -> bytes: ...


class ByteWriter(Protocol):
    def write(self, data: bytes) -> object: ...


ModelT = TypeVar("ModelT", bound=StrictModel)


def generate_pairing_secret() -> bytes:
    return secrets.token_bytes(PAIRING_SECRET_BYTES)


def encode_pairing_code(secret: bytes) -> str:
    if len(secret) != PAIRING_SECRET_BYTES:
        raise TransferError("Pairing secrets must contain exactly 128 bits.")
    encoded = base64.b32encode(secret).decode("ascii").rstrip("=")
    return "-".join((encoded[:5], encoded[5:10], encoded[10:15], encoded[15:20], encoded[20:]))


def decode_pairing_code(value: str) -> bytes:
    normalized = "".join(character for character in value.upper() if character not in "- \t\r\n")
    if len(normalized) != 26 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for character in normalized
    ):
        raise TransferError("Invalid pairing code. Enter the complete code shown by transfer send.")
    padded = normalized + "=" * ((8 - len(normalized) % 8) % 8)
    try:
        secret = base64.b32decode(padded, casefold=False)
    except ValueError as error:
        raise TransferError("Invalid pairing code encoding.") from error
    if len(secret) != PAIRING_SECRET_BYTES:
        raise TransferError("Invalid pairing code length.")
    return secret


def _session_id(secret: bytes) -> str:
    return hashlib.sha256(b"agent-port/session/v1\0" + secret).hexdigest()[:16]


def _derive(secret: bytes, purpose: bytes, length: int) -> bytes:
    session = bytes.fromhex(_session_id(secret))
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=session, info=purpose).derive(secret)


def derive_authentication_password(secret: bytes) -> str:
    value = _derive(secret, b"agent-port/ssh-auth/v1", 32)
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def derive_host_key(secret: bytes) -> asyncssh.SSHKey:
    seed = _derive(secret, b"agent-port/ssh-host-key/v1", 32)
    key = Ed25519PrivateKey.from_private_bytes(seed)
    private_data = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return asyncssh.import_private_key(private_data)


def build_discovery_record(secret: bytes, expires_at: int) -> DiscoveryRecord:
    fingerprint = derive_host_key(secret).get_fingerprint("sha256")
    session_id = _session_id(secret)
    unsigned = {
        "protocol_version": TRANSFER_PROTOCOL_VERSION,
        "session_id": session_id,
        "expires_at": expires_at,
        "host_key_fingerprint": fingerprint,
    }
    authentication_tag = hmac.new(
        _derive(secret, b"agent-port/discovery-hmac/v1", 32),
        _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return DiscoveryRecord(
        protocol_version=1,
        session_id=session_id,
        expires_at=expires_at,
        host_key_fingerprint=fingerprint,
        authentication_tag=authentication_tag,
    )


def verify_discovery_record(secret: bytes, record: DiscoveryRecord, now: int) -> bool:
    if record.expires_at <= now or record.session_id != _session_id(secret):
        return False
    expected_fingerprint = derive_host_key(secret).get_fingerprint("sha256")
    if not hmac.compare_digest(record.host_key_fingerprint, expected_fingerprint):
        return False
    expected = build_discovery_record(secret, record.expires_at)
    return hmac.compare_digest(record.authentication_tag, expected.authentication_tag)


def discovery_properties(record: DiscoveryRecord) -> dict[str, str]:
    return {
        "v": str(record.protocol_version),
        "sid": record.session_id,
        "exp": str(record.expires_at),
        "fp": record.host_key_fingerprint,
        "tag": record.authentication_tag,
    }


def parse_discovery_properties(properties: Mapping[bytes, bytes | None]) -> DiscoveryRecord:
    try:
        decoded = {
            key.decode("ascii"): value.decode("ascii")
            for key, value in properties.items()
            if value is not None
        }
        protocol_version = int(decoded["v"])
        if protocol_version != TRANSFER_PROTOCOL_VERSION:
            raise TransferError("Unsupported Agent Port discovery protocol version.")
        return DiscoveryRecord(
            protocol_version=1,
            session_id=decoded["sid"],
            expires_at=int(decoded["exp"]),
            host_key_fingerprint=decoded["fp"],
            authentication_tag=decoded["tag"],
        )
    except (KeyError, UnicodeDecodeError, ValueError, ValidationError) as error:
        raise TransferError("Invalid Agent Port discovery record.") from error


def is_allowed_peer(value: str) -> bool:
    host = value.split("%", maxsplit=1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_link_local
        or address.is_loopback
        or any(
            address.version == network.version and address in network
            for network in PRIVATE_NETWORKS
        )
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def encode_frame(model: StrictModel) -> bytes:
    payload = _canonical_json(model.model_dump(mode="json"))
    if len(payload) > MAX_FRAME_SIZE:
        raise TransferError("Transfer protocol frame is too large.")
    return struct.pack(">I", len(payload)) + payload


async def read_frame(reader: AsyncByteReader, model: type[ModelT]) -> ModelT:
    size_data = await read_exactly(reader, 4)
    size = struct.unpack(">I", size_data)[0]
    if size == 0 or size > MAX_FRAME_SIZE:
        raise TransferError("Invalid transfer protocol frame size.")
    payload = await read_exactly(reader, size)
    try:
        return model.model_validate_json(payload)
    except ValidationError as error:
        raise TransferError(f"Invalid {model.__name__} frame: {error}") from error


async def read_exactly(reader: AsyncByteReader, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = await reader.read(remaining)
        if not chunk:
            raise TransferError("Transfer ended before all expected bytes were received.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_frame(writer: ByteWriter, model: StrictModel) -> None:
    writer.write(encode_frame(model))


def publish_no_clobber(partial: Path, output: Path) -> None:
    try:
        os.link(partial, output)
    except FileExistsError as error:
        raise TransferError(f"Refusing to overwrite existing output: {output}") from error
    except OSError as error:
        raise TransferError(f"Could not publish received archive atomically: {error}") from error
    partial.unlink()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
