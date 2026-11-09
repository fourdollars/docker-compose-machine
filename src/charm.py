#!/usr/bin/env python3
"""Minimal Juju machine charm for running Docker Compose workloads (PoC only)."""

from __future__ import annotations

import logging
from typing import Any

import ops
from ops.charm import CharmBase
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    ModelError,
    SecretNotFoundError,
)

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
        framework.observe(self.on.secret_changed, self._on_secret_changed)
        framework.observe(self.on.tailscale_login_action, self._on_tailscale_login_action)
        framework.observe(self.on.tailscale_status_action, self._on_tailscale_status_action)
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

    def _on_secret_changed(self, _event: ops.SecretChangedEvent) -> None:
        self._reconcile()

    def _on_stop(self, _event: ops.StopEvent) -> None:
        self._shutdown()

    def _on_remove(self, _event: ops.RemoveEvent) -> None:
        self._shutdown()

    def _get_tailscale_config(self) -> dict[str, Any] | None:
        """Retrieve and parse Tailscale settings from the configured Juju Secret."""
        secret_uri = str(self.config.get("tailscale-secret") or "").strip()
        if not secret_uri:
            return None

        try:
            secret = self.model.get_secret(id=secret_uri)
            content = secret.get_content(refresh=True)
        except (SecretNotFoundError, ModelError) as exc:
            logger.warning("Failed to retrieve Tailscale secret %s: %s", secret_uri, exc)
            return None

        auth_key = content.get("authkey") or content.get("auth-key") or ""
        hostname = content.get("hostname") or ""
        domain = content.get("domain") or ""

        web_port_raw = content.get("web-port") or content.get("web_port") or ""
        web_port: int | None = None
        if web_port_raw and str(web_port_raw).strip().isdigit():
            web_port = int(str(web_port_raw).strip())

        tcp_ports: list[int] = []
        if "ports" in content:
            for p in str(content["ports"]).split(","):
                p_clean = p.strip()
                if p_clean.isdigit():
                    tcp_ports.append(int(p_clean))

        return {
            "auth_key": auth_key,
            "hostname": hostname,
            "domain": domain,
            "web_port": web_port,
            "tcp_ports": tcp_ports,
        }

    def _on_tailscale_login_action(self, event: ops.ActionEvent) -> None:
        """Handle tailscale-login action to output interactive browser auth URL."""
        if not self.compose.is_tailscale_installed():
            event.fail("Tailscale is not installed. Please configure 'tailscale-secret' first.")
            return

        status = self.compose.get_tailscale_status()
        if status.get("BackendState") == "Running":
            event.set_results(
                {
                    "status": "already_logged_in",
                    "tailscale-ips": status.get("TailscaleIPs", []),
                }
            )
            return

        hostname = event.params.get("hostname") or None
        if not hostname:
            ts_cfg = self._get_tailscale_config()
            if ts_cfg and ts_cfg.get("hostname"):
                hostname = ts_cfg["hostname"]

        url = self.compose.get_tailscale_login_url(hostname=hostname)
        if url:
            event.set_results({"status": "needs_login", "login-url": url})
        else:
            event.fail("Failed to retrieve Tailscale login URL.")

    def _on_tailscale_status_action(self, event: ops.ActionEvent) -> None:
        """Handle tailscale-status action to report current state."""
        if not self.compose.is_tailscale_installed():
            event.fail("Tailscale is not installed.")
            return

        status = self.compose.get_tailscale_status()
        event.set_results(
            {
                "backend-state": status.get("BackendState", "Unknown"),
                "tailscale-ips": status.get("TailscaleIPs", []),
                "auth-url": status.get("AuthURL", ""),
            }
        )

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

        ts_config = self._get_tailscale_config()
        if ts_config is not None:
            if not self.compose.is_tailscale_installed():
                self.unit.status = MaintenanceStatus("Installing Tailscale")
                try:
                    self.compose.install_tailscale()
                except ComposeError as exc:
                    logger.error("Failed to install Tailscale: %s", exc)
                    self.unit.status = BlockedStatus(f"Tailscale install failed: {exc}")
                    return

            if ts_config.get("auth_key"):
                try:
                    self.compose.tailscale_up(
                        ts_config["auth_key"],
                        hostname=ts_config.get("hostname") or None,
                    )
                except ComposeError as exc:
                    logger.warning("Tailscale up failed: %s", exc)

            extra_env: list[str] = []
            if ts_config.get("domain"):
                extra_env.append(f"DOMAIN={ts_config['domain']}")
            if ts_config.get("hostname"):
                extra_env.append(f"TAILSCALE_HOSTNAME={ts_config['hostname']}")
            if ts_config.get("web_port"):
                extra_env.append(f"WEB_PORT={ts_config['web_port']}")
            if ts_config.get("tcp_ports"):
                extra_env.append(f"PORTS={','.join(map(str, ts_config['tcp_ports']))}")

            if extra_env:
                if env_content and not env_content.endswith("\n"):
                    env_content += "\n"
                env_content += "\n".join(extra_env) + "\n"

        self.unit.status = MaintenanceStatus("Applying Docker Compose configuration")
        try:
            if has_url:
                self.compose.setup_from_url(url, env_content)
            else:
                self.compose.write_files(raw, env_content)
            ports = self.compose.validate_and_get_ports()
            self.compose.up()
            self.unit.set_ports(*(ops.Port(proto, port) for proto, port in ports))

            tailscale_logged_in = False
            if ts_config is not None:
                ts_status = self.compose.get_tailscale_status()
                tailscale_logged_in = ts_status.get("BackendState") == "Running"
                if tailscale_logged_in:
                    web_port = ts_config.get("web_port")
                    tcp_ports = list(ts_config.get("tcp_ports") or [])
                    if not tcp_ports:
                        tcp_ports = [p for proto, p in ports if proto == "tcp" and p != web_port]
                    self.compose.tailscale_serve(web_port=web_port, tcp_ports=tcp_ports)

            if ts_config is not None and not tailscale_logged_in:
                self.unit.status = ActiveStatus("Running (Tailscale login required)")
            else:
                self.unit.status = ActiveStatus("Running")
        except ComposeError as exc:
            logger.error("Docker Compose reconcile failed: %s", exc)
            self.unit.status = BlockedStatus(str(exc)[:120])

    def _shutdown(self) -> None:
        if self.compose.is_tailscale_installed():
            self.compose.tailscale_serve_reset()
        try:
            self.compose.down()
        except ComposeError as exc:
            logger.warning("Failed to run docker compose down: %s", exc)
        self.unit.set_ports()


if __name__ == "__main__":  # pragma: nocover
    main(DockerComposeCharm)
