"""Unit tests for DockerComposeCharm using ops.testing.Harness."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import ops
import pytest
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import ActionFailed, Harness

from charm import DockerComposeCharm
from compose import ComposeError

CONCOURSE_RAW_YAML = """\
services:
  concourse-db:
    image: postgres
  concourse:
    image: concourse/concourse
    command: quickstart
    ports: ["8080:8080"]
"""


@pytest.fixture()
def harness(tmp_path: Path) -> Harness[DockerComposeCharm]:
    h = Harness(DockerComposeCharm)
    yield h
    h.cleanup()


class TestDockerComposeCharm:
    """Lifecycle and configuration tests for DockerComposeCharm."""

    def test_install_calls_install_runtime(self, harness: Harness[DockerComposeCharm]) -> None:
        harness.begin()
        with patch.object(harness.charm.compose, "install_runtime") as mock_install:
            harness.charm.on.install.emit()
        mock_install.assert_called_once()

    def test_blocks_when_neither_raw_nor_url_is_configured(
        self, harness: Harness[DockerComposeCharm]
    ) -> None:
        harness.begin()
        harness.update_config({"raw": "", "url": ""})
        assert harness.model.unit.status == BlockedStatus("Waiting for 'raw' or 'url' config")

    def test_blocks_when_both_raw_and_url_are_configured(
        self, harness: Harness[DockerComposeCharm]
    ) -> None:
        harness.begin()
        harness.update_config(
            {
                "raw": CONCOURSE_RAW_YAML,
                "url": "https://concourse-ci.org/docker-compose.yml",
            }
        )
        assert harness.model.unit.status == BlockedStatus(
            "Cannot set both 'raw' and 'url'; choose one"
        )

    def test_deploys_with_raw_config_and_opens_ports(
        self, harness: Harness[DockerComposeCharm], tmp_path: Path
    ) -> None:
        harness.begin()
        harness.charm.compose.work_dir = tmp_path

        with (
            patch.object(
                harness.charm.compose,
                "validate_and_get_ports",
                return_value=[("tcp", 8080)],
            ) as mock_validate,
            patch.object(harness.charm.compose, "up") as mock_up,
        ):
            harness.update_config({"raw": CONCOURSE_RAW_YAML})

        mock_validate.assert_called_once()
        mock_up.assert_called_once()
        assert (tmp_path / "compose.yaml").read_text() == CONCOURSE_RAW_YAML
        assert harness.model.unit.opened_ports() == {ops.Port("tcp", 8080)}
        assert harness.model.unit.status == ActiveStatus("Running")

    def test_deploys_with_url_and_env_config(
        self, harness: Harness[DockerComposeCharm], tmp_path: Path
    ) -> None:
        harness.begin()
        harness.charm.compose.work_dir = tmp_path
        temporal_url = (
            "https://raw.githubusercontent.com/temporalio/"
            "temporal-server-operations/refs/heads/main/compose/docker-compose.yml"
        )

        with (
            patch.object(harness.charm.compose, "setup_from_url") as mock_setup,
            patch.object(
                harness.charm.compose,
                "validate_and_get_ports",
                return_value=[("tcp", 5432), ("tcp", 7233), ("tcp", 8233)],
            ),
            patch.object(harness.charm.compose, "up") as mock_up,
        ):
            harness.update_config(
                {
                    "url": temporal_url,
                    "env": "POSTGRESQL_VERSION=16",
                }
            )

        mock_setup.assert_called_once_with(temporal_url, "POSTGRESQL_VERSION=16")
        mock_up.assert_called_once()
        assert harness.model.unit.opened_ports() == {
            ops.Port("tcp", 5432),
            ops.Port("tcp", 7233),
            ops.Port("tcp", 8233),
        }
        assert harness.model.unit.status == ActiveStatus("Running")

    def test_blocks_on_compose_error(
        self, harness: Harness[DockerComposeCharm], tmp_path: Path
    ) -> None:
        harness.begin()
        harness.charm.compose.work_dir = tmp_path

        with patch.object(
            harness.charm.compose,
            "validate_and_get_ports",
            side_effect=ComposeError("invalid compose syntax"),
        ):
            harness.update_config({"raw": "invalid: yaml"})

        assert isinstance(harness.model.unit.status, BlockedStatus)
        assert "invalid compose syntax" in harness.model.unit.status.message

    def test_stop_and_remove_bring_compose_down_and_close_ports(
        self, harness: Harness[DockerComposeCharm], tmp_path: Path
    ) -> None:
        harness.begin()
        harness.charm.compose.work_dir = tmp_path

        with (
            patch.object(
                harness.charm.compose,
                "validate_and_get_ports",
                return_value=[("tcp", 8080)],
            ),
            patch.object(harness.charm.compose, "up"),
        ):
            harness.update_config({"raw": CONCOURSE_RAW_YAML})

        assert harness.model.unit.opened_ports() == {ops.Port("tcp", 8080)}

        mock_down = MagicMock()
        harness.charm.compose.down = mock_down
        harness.charm.on.stop.emit()

        mock_down.assert_called_once()
        assert harness.model.unit.opened_ports() == set()

    def test_tailscale_skipped_when_secret_empty(
        self, harness: Harness[DockerComposeCharm], tmp_path: Path
    ) -> None:
        harness.begin()
        harness.charm.compose.work_dir = tmp_path

        with (
            patch.object(harness.charm.compose, "install_tailscale") as mock_ts_install,
            patch.object(harness.charm.compose, "tailscale_serve") as mock_ts_serve,
            patch.object(
                harness.charm.compose,
                "validate_and_get_ports",
                return_value=[("tcp", 8080)],
            ),
            patch.object(harness.charm.compose, "up"),
        ):
            harness.update_config({"raw": CONCOURSE_RAW_YAML, "tailscale-secret": ""})

        mock_ts_install.assert_not_called()
        mock_ts_serve.assert_not_called()
        assert harness.model.unit.status == ActiveStatus("Running")

    def test_tailscale_configured_with_authkey_and_ports(
        self, harness: Harness[DockerComposeCharm], tmp_path: Path
    ) -> None:
        harness.begin()
        harness.charm.compose.work_dir = tmp_path

        secret_id = harness.add_user_secret(
            {
                "authkey": "tskey-auth-12345",
                "hostname": "temporal",
                "web-port": "8080",
                "ports": "5432, 7233, 9200",
                "domain": "temporal.example.com",
            }
        )
        harness.grant_secret(secret_id, harness.charm.app.name)

        with (
            patch.object(harness.charm.compose, "is_tailscale_installed", return_value=False),
            patch.object(harness.charm.compose, "install_tailscale") as mock_ts_install,
            patch.object(harness.charm.compose, "tailscale_up") as mock_ts_up,
            patch.object(
                harness.charm.compose,
                "get_tailscale_status",
                return_value={"BackendState": "Running"},
            ),
            patch.object(harness.charm.compose, "tailscale_serve") as mock_ts_serve,
            patch.object(
                harness.charm.compose,
                "validate_and_get_ports",
                return_value=[("tcp", 8080), ("tcp", 5432)],
            ),
            patch.object(harness.charm.compose, "up"),
        ):
            harness.update_config({"raw": CONCOURSE_RAW_YAML, "tailscale-secret": secret_id})

        mock_ts_install.assert_called_once()
        mock_ts_up.assert_called_once_with("tskey-auth-12345", hostname="temporal")
        mock_ts_serve.assert_called_once_with(web_port=8080, tcp_ports=[5432, 7233, 9200])
        assert harness.model.unit.status == ActiveStatus("Running")
        env_text = (tmp_path / ".env").read_text()
        assert "DOMAIN=temporal.example.com" in env_text
        assert "TAILSCALE_HOSTNAME=temporal" in env_text
        assert "WEB_PORT=8080" in env_text
        assert "PORTS=5432,7233,9200" in env_text

    def test_tailscale_auto_detects_ports_when_omitted_in_secret(
        self, harness: Harness[DockerComposeCharm], tmp_path: Path
    ) -> None:
        harness.begin()
        harness.charm.compose.work_dir = tmp_path

        secret_id = harness.add_user_secret(
            {
                "auth-key": "tskey-auth-67890",
                "hostname": "hindsight",
            }
        )
        harness.grant_secret(secret_id, harness.charm.app.name)

        with (
            patch.object(harness.charm.compose, "is_tailscale_installed", return_value=True),
            patch.object(harness.charm.compose, "tailscale_up"),
            patch.object(
                harness.charm.compose,
                "get_tailscale_status",
                return_value={"BackendState": "Running"},
            ),
            patch.object(harness.charm.compose, "tailscale_serve") as mock_ts_serve,
            patch.object(
                harness.charm.compose,
                "validate_and_get_ports",
                return_value=[("tcp", 8888), ("tcp", 9999)],
            ),
            patch.object(harness.charm.compose, "up"),
        ):
            harness.update_config({"raw": CONCOURSE_RAW_YAML, "tailscale-secret": secret_id})

        mock_ts_serve.assert_called_once_with(web_port=None, tcp_ports=[8888, 9999])
        assert harness.model.unit.status == ActiveStatus("Running")

    def test_tailscale_interactive_login_required(
        self, harness: Harness[DockerComposeCharm], tmp_path: Path
    ) -> None:
        harness.begin()
        harness.charm.compose.work_dir = tmp_path

        secret_id = harness.add_user_secret({"hostname": "my-host"})
        harness.grant_secret(secret_id, harness.charm.app.name)

        with (
            patch.object(harness.charm.compose, "is_tailscale_installed", return_value=True),
            patch.object(harness.charm.compose, "tailscale_up") as mock_ts_up,
            patch.object(
                harness.charm.compose,
                "get_tailscale_status",
                return_value={"BackendState": "NeedsLogin"},
            ),
            patch.object(harness.charm.compose, "tailscale_serve") as mock_ts_serve,
            patch.object(
                harness.charm.compose,
                "validate_and_get_ports",
                return_value=[("tcp", 8080)],
            ),
            patch.object(harness.charm.compose, "up"),
        ):
            harness.update_config({"raw": CONCOURSE_RAW_YAML, "tailscale-secret": secret_id})

        mock_ts_up.assert_not_called()
        mock_ts_serve.assert_not_called()
        assert harness.model.unit.status == ActiveStatus("Running (Tailscale login required)")

    def test_tailscale_login_action(self, harness: Harness[DockerComposeCharm]) -> None:
        harness.begin()

        # When Tailscale is not installed
        with (
            patch.object(harness.charm.compose, "is_tailscale_installed", return_value=False),
            pytest.raises(ActionFailed, match="Tailscale is not installed"),
        ):
            harness.run_action("tailscale-login")

        # When already logged in
        with (
            patch.object(harness.charm.compose, "is_tailscale_installed", return_value=True),
            patch.object(
                harness.charm.compose,
                "get_tailscale_status",
                return_value={"BackendState": "Running", "TailscaleIPs": ["100.64.1.2"]},
            ),
        ):
            out = harness.run_action("tailscale-login")
            assert out.results["status"] == "already_logged_in"
            assert out.results["tailscale-ips"] == ["100.64.1.2"]

        # When needs login
        with (
            patch.object(harness.charm.compose, "is_tailscale_installed", return_value=True),
            patch.object(
                harness.charm.compose,
                "get_tailscale_status",
                return_value={"BackendState": "NeedsLogin"},
            ),
            patch.object(
                harness.charm.compose,
                "get_tailscale_login_url",
                return_value="https://login.tailscale.com/a/action123",
            ),
        ):
            out = harness.run_action("tailscale-login", params={"hostname": "custom-host"})
            assert out.results["status"] == "needs_login"
            assert out.results["login-url"] == "https://login.tailscale.com/a/action123"

    def test_tailscale_status_action(self, harness: Harness[DockerComposeCharm]) -> None:
        harness.begin()

        with (
            patch.object(harness.charm.compose, "is_tailscale_installed", return_value=True),
            patch.object(
                harness.charm.compose,
                "get_tailscale_status",
                return_value={
                    "BackendState": "Running",
                    "TailscaleIPs": ["100.100.1.1"],
                    "AuthURL": "",
                },
            ),
        ):
            out = harness.run_action("tailscale-status")
            assert out.results["backend-state"] == "Running"
            assert out.results["tailscale-ips"] == ["100.100.1.1"]
