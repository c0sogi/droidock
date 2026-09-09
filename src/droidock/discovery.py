from __future__ import annotations

import threading
import time

from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf

from .models import ADB_SERVICE_KINDS, Service, normalize_endpoint

SERVICE_TYPES = {f"{service_type}.local.": kind for service_type, kind in ADB_SERVICE_KINDS.items()}


class MdnsDiscovery:
    """Independent multicast discovery, merged with ADB's results by ConnectionManager."""

    def discover(self, seconds: float) -> tuple[list[Service], list[str]]:
        if seconds <= 0:
            return [], []
        discovered: set[Service] = set()
        lock = threading.Lock()
        warnings: list[str] = []

        def changed(
            zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange
        ) -> None:
            if state_change is ServiceStateChange.Removed:
                return
            info = zeroconf.get_service_info(service_type, name, timeout=500)
            if not info or not info.port:
                return
            for address in info.parsed_scoped_addresses(IPVersion.All):
                host = f"[{address}]" if ":" in address else address
                item = Service(
                    name.removesuffix("." + service_type),
                    SERVICE_TYPES[service_type],
                    normalize_endpoint(f"{host}:{info.port}"),
                    "zeroconf",
                )
                with lock:
                    discovered.add(item)

        try:
            with Zeroconf(ip_version=IPVersion.All) as zeroconf:
                browser = ServiceBrowser(zeroconf, list(SERVICE_TYPES), handlers=[changed])
                try:
                    time.sleep(seconds)
                finally:
                    browser.cancel()
        except OSError as exc:
            warnings.append(f"Wireless discovery is unavailable: {exc}")
        return sorted(discovered, key=lambda item: (item.kind, item.instance, item.endpoint)), warnings
