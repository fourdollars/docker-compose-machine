"""Unit tests for DockerComposeCharm using ops.testing.Harness."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import ops
import pytest
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

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
