#!/usr/bin/env python3
"""Minimal Juju machine charm for running Docker Compose workloads (PoC only)."""

from __future__ import annotations

import logging

import ops
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

from compose import ComposeError, ComposeManager

logger = logging.getLogger(__name__)


class DockerComposeCharm(CharmBase):
    """Charm the service using Docker Compose."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)
        self.compose = ComposeManager()

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.upgrade_charm, self._on_config_changed)
        framework.observe(self.on.stop, self._on_stop)
        framework.observe(self.on.remove, self._on_remove)

    def _on_install(self, _event: ops.InstallEvent) -> None:
        self.unit.status = MaintenanceStatus("Installing Docker and Docker Compose")
        try:
            self.compose.install_runtime()
        except ComposeError as exc:
            logger.error("Failed to install Docker runtime: %s", exc)
            self.unit.status = BlockedStatus(f"Install failed: {exc}")

    def _on_start(self, _event: ops.StartEvent) -> None:
        self._reconcile()

    def _on_config_changed(self, _event: ops.HookEvent) -> None:
        self._reconcile()

    def _on_stop(self, _event: ops.StopEvent) -> None:
        self._shutdown()

    def _on_remove(self, _event: ops.RemoveEvent) -> None:
        self._shutdown()

    def _reconcile(self) -> None:
        raw = str(self.config.get("raw") or "")
        url = str(self.config.get("url") or "").strip()
        env_content = str(self.config.get("env") or "")

        has_raw = bool(raw.strip())
        has_url = bool(url)

        if not has_raw and not has_url:
            self.unit.status = BlockedStatus("Waiting for 'raw' or 'url' config")
            return

        if has_raw and has_url:
            self.unit.status = BlockedStatus("Cannot set both 'raw' and 'url'; choose one")
            return

        self.unit.status = MaintenanceStatus("Applying Docker Compose configuration")
        try:
            if has_url:
                self.compose.setup_from_url(url, env_content)
            else:
                self.compose.write_files(raw, env_content)
            ports = self.compose.validate_and_get_ports()
            self.compose.up()
            self.unit.set_ports(*(ops.Port(proto, port) for proto, port in ports))
            self.unit.status = ActiveStatus("Running")
        except ComposeError as exc:
            logger.error("Docker Compose reconcile failed: %s", exc)
            self.unit.status = BlockedStatus(str(exc)[:120])

    def _shutdown(self) -> None:
        try:
            self.compose.down()
        except ComposeError as exc:
            logger.warning("Failed to run docker compose down: %s", exc)
        self.unit.set_ports()


if __name__ == "__main__":  # pragma: nocover
    main(DockerComposeCharm)
