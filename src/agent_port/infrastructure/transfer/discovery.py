from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import ifaddr
from zeroconf import IPVersion, ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from agent_port.domain.errors import TransferError
from agent_port.domain.models import DiscoveryRecord
from agent_port.infrastructure.transfer.protocol import (
    discovery_properties,
    is_allowed_peer,
    parse_discovery_properties,
    verify_discovery_record,
)

SERVICE_TYPE = "_agent-port._tcp.local."


@dataclass(frozen=True)
class DiscoveredEndpoint:
    hosts: tuple[str, ...]
    port: int
    record: DiscoveryRecord


class LanAdvertisement:
    def __init__(
        self,
        zeroconf: AsyncZeroconf,
        info: ServiceInfo,
        addresses: tuple[str, ...],
    ) -> None:
        self._zeroconf = zeroconf
        self._info = info
        self.addresses = addresses

    async def close(self) -> None:
        try:
            await self._zeroconf.async_unregister_service(self._info)
        finally:
            await self._zeroconf.async_close()


class LanDiscovery:
    async def advertise(self, record: DiscoveryRecord, port: int) -> LanAdvertisement:
        addresses = local_ipv4_addresses()
        if not addresses:
            raise TransferError("No private or link-local IPv4 address is available for discovery.")
        zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        service_name = f"{record.session_id}.{SERVICE_TYPE}"
        info = ServiceInfo(
            SERVICE_TYPE,
            service_name,
            port=port,
            properties=discovery_properties(record),
            parsed_addresses=list(addresses),
            server=f"{record.session_id}.local.",
        )
        try:
            await zeroconf.async_register_service(info)
        except Exception as error:
            await zeroconf.async_close()
            raise TransferError(f"Could not advertise the LAN transfer: {error}") from error
        return LanAdvertisement(zeroconf, info, addresses)

    async def discover(self, secret: bytes, timeout: float) -> DiscoveredEndpoint:
        zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        tasks: set[asyncio.Task[None]] = set()
        matches: dict[str, DiscoveredEndpoint] = {}
        match_found = asyncio.Event()

        async def resolve(service_type: str, name: str) -> None:
            info = await zeroconf.async_get_service_info(service_type, name, timeout=1000)
            if info is None:
                return
            try:
                record = parse_discovery_properties(info.properties)
            except TransferError:
                return
            if not verify_discovery_record(secret, record, int(time.time())):
                return
            addresses = tuple(
                address for address in info.parsed_scoped_addresses() if is_allowed_peer(address)
            )
            if addresses:
                if info.port is None:
                    return
                matches[name] = DiscoveredEndpoint(addresses, info.port, record)
                match_found.set()

        def on_change(
            zeroconf: object,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ) -> None:
            # zeroconf calls handlers with keyword arguments. Preserve its
            # parameter names even though this callback does not need the
            # zeroconf instance itself.
            del zeroconf
            if state_change not in {ServiceStateChange.Added, ServiceStateChange.Updated}:
                return
            task = asyncio.create_task(resolve(service_type, name))
            tasks.add(task)

        browser = AsyncServiceBrowser(zeroconf.zeroconf, SERVICE_TYPE, handlers=[on_change])
        try:
            try:
                await asyncio.wait_for(match_found.wait(), timeout=timeout)
                await asyncio.sleep(0.1)
            except TimeoutError:
                pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await browser.async_cancel()
            await zeroconf.async_close()
        if not matches:
            raise TransferError(
                "No matching Agent Port transfer was discovered. Use --host and --port "
                "with the fallback endpoint shown on the source machine."
            )
        if len(matches) > 1:
            raise TransferError("Multiple transfers matched this pairing code; refusing ambiguity.")
        return next(iter(matches.values()))


def local_ipv4_addresses() -> tuple[str, ...]:
    candidates = [
        ip.ip for adapter in ifaddr.get_adapters() for ip in adapter.ips if isinstance(ip.ip, str)
    ]
    return tuple(
        sorted(
            {
                address
                for address in candidates
                if is_allowed_peer(address) and not address.startswith("127.")
            }
        )
    )
