"""Unit tests for compose.py helper module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from compose import (
    ComposeError,
    ComposeManager,
    extract_ports_from_config,
    fetch_url,
    parse_git_url,
)


class TestFetchUrl:
    """Tests for fetching remote docker-compose.yml via HTTP/HTTPS."""

    def test_rejects_non_http_url(self) -> None:
        with pytest.raises(ComposeError, match="http:// or https://"):
            fetch_url("file:///etc/passwd")

    def test_fetches_valid_https_url(self) -> None:
        fake_yaml = "services:\n  web:\n    image: nginx\n"
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_yaml.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("compose.urlopen", return_value=mock_resp) as mock_urlopen:
            content = fetch_url("https://concourse-ci.org/docker-compose.yml")

        assert content == fake_yaml
        mock_urlopen.assert_called_once()

    def test_raises_compose_error_on_network_failure(self) -> None:
        with (
            patch("compose.urlopen", side_effect=URLError("connection refused")),
            pytest.raises(ComposeError, match="Failed to download"),
        ):
            fetch_url("https://concourse-ci.org/docker-compose.yml")


class TestExtractPortsFromConfig:
    """Tests for parsing `docker compose config --format json` output."""

    def test_extracts_ports_from_normalized_json(self) -> None:
        config_json = json.dumps(
            {
                "name": "concourse-ci",
                "services": {
                    "concourse-db": {"image": "postgres"},
                    "concourse": {
                        "image": "concourse/concourse",
                        "ports": [
                            {
                                "mode": "ingress",
                                "target": 8080,
                                "published": "8080",
                                "protocol": "tcp",
                            }
                        ],
                    },
                },
            }
        )
        assert extract_ports_from_config(config_json) == [("tcp", 8080)]

    def test_skips_loopback_bound_ports_and_deduplicates(self) -> None:
        config_json = json.dumps(
            {
                "services": {
                    "svc1": {
                        "ports": [
                            {
                                "host_ip": "127.0.0.1",
                                "target": 5432,
                                "published": "5432",
                                "protocol": "tcp",
                            },
                            {
                                "host_ip": "0.0.0.0",
                                "target": 7233,
                                "published": "7233",
                                "protocol": "tcp",
                            },
                            {
                                "target": 8233,
                                "published": 8233,
                                "protocol": "tcp",
                            },
                        ]
                    },
                    "svc2": {
                        "ports": [
                            {
                                "target": 7233,
                                "published": "7233",
                                "protocol": "tcp",
                            },
                            {
                                "target": 9000,
                                "published": "9000-9001",
                                "protocol": "udp",
                            },
                        ]
                    },
                }
            }
        )
        assert extract_ports_from_config(config_json) == [
            ("tcp", 7233),
            ("tcp", 8233),
            ("udp", 9000),
            ("udp", 9001),
        ]

    def test_raises_on_invalid_json_or_missing_services(self) -> None:
        with pytest.raises(ComposeError, match="Invalid compose config"):
            extract_ports_from_config("not-json")

        with pytest.raises(ComposeError, match="services"):
            extract_ports_from_config(json.dumps({"version": "3"}))


class TestComposeManager:
    """Tests for file writing and docker compose CLI invocations."""

    def test_write_files_creates_compose_and_env_with_0600(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path / "compose-work")
        mgr.write_files(
            raw_yaml="services:\n  db:\n    image: postgres:${POSTGRESQL_VERSION}\n",
            env_content="POSTGRESQL_VERSION=16",
        )

        assert mgr.compose_file.read_text() == (
            "services:\n  db:\n    image: postgres:${POSTGRESQL_VERSION}\n"
        )
        assert mgr.env_file.read_text() == "POSTGRESQL_VERSION=16\n"
        assert (mgr.env_file.stat().st_mode & 0o777) == 0o600

    def test_write_files_removes_env_file_when_env_empty(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        mgr.write_files("services:\n  db:\n    image: postgres\n", "FOO=bar")
        assert mgr.env_file.exists()

        mgr.write_files("services:\n  db:\n    image: postgres\n", "")
        assert not mgr.env_file.exists()

    def test_validate_and_get_ports_invokes_docker_compose_config(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        mgr.write_files("services:\n  web:\n    image: nginx\n", "")

        fake_stdout = json.dumps(
            {
                "services": {
                    "web": {"ports": [{"target": 80, "published": "8080", "protocol": "tcp"}]}
                }
            }
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fake_stdout, stderr=""
        )
        with patch("compose.subprocess.run", return_value=completed) as mock_run:
            ports = mgr.validate_and_get_ports()

        assert ports == [("tcp", 8080)]
        mock_run.assert_called_once_with(
            [
                "docker",
                "compose",
                "--project-directory",
                str(tmp_path),
                "-f",
                str(mgr.compose_file),
                "config",
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_up_and_down_commands(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        mgr.write_files("services:\n  web:\n    image: nginx\n", "")

        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("compose.subprocess.run", return_value=ok) as mock_run:
            mgr.up()
            mgr.down()

        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0].args[0] == [
            "docker",
            "compose",
            "--project-directory",
            str(tmp_path),
            "-f",
            str(mgr.compose_file),
            "up",
            "-d",
            "--remove-orphans",
        ]
        assert mock_run.call_args_list[1].args[0] == [
            "docker",
            "compose",
            "--project-directory",
            str(tmp_path),
            "-f",
            str(mgr.compose_file),
            "down",
            "--remove-orphans",
        ]

    def test_compose_file_resolution(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        assert mgr.compose_file == tmp_path / "compose.yaml"

        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        assert mgr.compose_file == tmp_path / "docker-compose.yml"

        (tmp_path / "compose.yaml").write_text("services: {}\n")
        assert mgr.compose_file == tmp_path / "compose.yaml"

    def test_setup_from_url_with_direct_file(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        fake_yaml = "services:\n  app:\n    image: my-app\n"
        with patch("compose.fetch_url", return_value=fake_yaml) as mock_fetch:
            mgr.setup_from_url("https://example.com/docker-compose.yml", "FOO=bar")

        mock_fetch.assert_called_once_with("https://example.com/docker-compose.yml")
        assert (tmp_path / "compose.yaml").read_text() == fake_yaml
        assert (tmp_path / ".env").read_text() == "FOO=bar\n"

    def test_setup_from_url_with_git_repo(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        with patch.object(mgr, "_clone_git_repo") as mock_clone:
            (tmp_path / "docker-compose.yml").write_text("services: {}\n")
            mgr.setup_from_url(
                "https://github.com/temporalio/temporal-server-operations.git#compose",
                "ENV=1",
            )

        mock_clone.assert_called_once_with(
            "https://github.com/temporalio/temporal-server-operations.git",
            branch=None,
            subpath="compose",
        )
        assert (tmp_path / ".env").read_text() == "ENV=1\n"

    def test_setup_from_url_raises_when_no_compose_file_found(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        with (
            patch.object(mgr, "_clone_git_repo"),
            pytest.raises(ComposeError, match="No compose file"),
        ):
            mgr.setup_from_url("https://github.com/foo/bar.git")

    def test_clone_git_repo_copies_subpath(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path / "work")

        def fake_clone(cmd: list[str]) -> str:
            clone_target = Path(cmd[-1])
            sub = clone_target / "compose"
            sub.mkdir(parents=True)
            (sub / "docker-compose.yml").write_text("services: {}\n")
            return ""

        with patch.object(mgr, "_run_cmd", side_effect=fake_clone) as mock_run:
            mgr._clone_git_repo(
                "https://github.com/temporalio/temporal-server-operations.git",
                branch="main",
                subpath="compose",
            )

        assert mock_run.call_count == 1
        assert "git" in mock_run.call_args[0][0]
        assert "--branch" in mock_run.call_args[0][0]
        assert (mgr.work_dir / "docker-compose.yml").exists()

    def test_clone_git_repo_raises_on_missing_subpath(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path / "work")

        def fake_clone(cmd: list[str]) -> str:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return ""

        with (
            patch.object(mgr, "_run_cmd", side_effect=fake_clone),
            pytest.raises(ComposeError, match="Subdirectory 'nonexistent' not found"),
        ):
            mgr._clone_git_repo("https://github.com/foo/bar.git", subpath="nonexistent")


class TestParseGitUrl:
    """Tests for parse_git_url helper."""

    def test_raw_github_with_refs_heads(self) -> None:
        url = "https://raw.githubusercontent.com/temporalio/temporal-server-operations/refs/heads/main/compose/docker-compose.yml"
        assert parse_git_url(url) == (
            "https://github.com/temporalio/temporal-server-operations.git",
            "main",
            "compose",
        )

    def test_raw_github_direct_branch(self) -> None:
        url = "https://raw.githubusercontent.com/temporalio/temporal-server-operations/main/compose/docker-compose.yml"
        assert parse_git_url(url) == (
            "https://github.com/temporalio/temporal-server-operations.git",
            "main",
            "compose",
        )

    def test_github_blob_url(self) -> None:
        url = "https://github.com/temporalio/temporal-server-operations/blob/main/compose/docker-compose.yml"
        assert parse_git_url(url) == (
            "https://github.com/temporalio/temporal-server-operations.git",
            "main",
            "compose",
        )

    def test_github_tree_url(self) -> None:
        url = "https://github.com/temporalio/temporal-server-operations/tree/main/compose"
        assert parse_git_url(url) == (
            "https://github.com/temporalio/temporal-server-operations.git",
            "main",
            "compose",
        )

    def test_git_repo_with_fragment(self) -> None:
        url = "https://github.com/temporalio/temporal-server-operations.git#compose"
        assert parse_git_url(url) == (
            "https://github.com/temporalio/temporal-server-operations.git",
            None,
            "compose",
        )

    def test_plain_github_repo_with_fragment(self) -> None:
        url = "https://github.com/temporalio/temporal-server-operations#compose"
        assert parse_git_url(url) == (
            "https://github.com/temporalio/temporal-server-operations.git",
            None,
            "compose",
        )

    def test_non_git_url_returns_none(self) -> None:
        url = "https://concourse-ci.org/docker-compose.yml"
        assert parse_git_url(url) is None


class TestTailscaleMethods:
    """Tests for Tailscale installation, login, status and serve helpers in ComposeManager."""

    def test_is_tailscale_installed(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        with patch("compose.shutil.which", return_value=None):
            assert mgr.is_tailscale_installed() is False
        with patch("compose.shutil.which", return_value="/usr/bin/tailscale"):
            assert mgr.is_tailscale_installed() is True

    def test_install_tailscale(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        with patch.object(mgr, "_run_cmd") as mock_cmd:
            mgr.install_tailscale()

        assert mock_cmd.call_count == 4
        calls = [call.args[0] for call in mock_cmd.call_args_list]
        assert calls[0] == ["apt-get", "update", "-y"]
        assert calls[1] == ["apt-get", "install", "-y", "curl"]
        assert calls[2] == ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"]
        assert calls[3] == ["systemctl", "enable", "--now", "tailscaled.service"]

    def test_get_tailscale_status(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        sample_status = json.dumps({"BackendState": "Running", "TailscaleIPs": ["100.64.0.1"]})
        with patch.object(mgr, "_run_cmd", return_value=sample_status):
            res = mgr.get_tailscale_status()
            assert res["BackendState"] == "Running"
            assert res["TailscaleIPs"] == ["100.64.0.1"]

        with patch.object(mgr, "_run_cmd", side_effect=ComposeError("tailscale error")):
            assert mgr.get_tailscale_status() == {}

    def test_get_tailscale_login_url_from_status(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        with patch.object(
            mgr,
            "get_tailscale_status",
            return_value={"AuthURL": "https://login.tailscale.com/a/status123"},
        ):
            assert mgr.get_tailscale_login_url() == "https://login.tailscale.com/a/status123"

    def test_get_tailscale_login_url_from_command_error(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        error_msg = "Command failed: To authenticate, visit: https://login.tailscale.com/a/err12345"
        with (
            patch.object(mgr, "get_tailscale_status", return_value={}),
            patch.object(mgr, "_run_cmd", side_effect=ComposeError(error_msg)),
        ):
            url = mgr.get_tailscale_login_url(hostname="my-node")
            assert url == "https://login.tailscale.com/a/err12345"

    def test_tailscale_up(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        with patch.object(mgr, "_run_cmd") as mock_cmd:
            mgr.tailscale_up("tskey-auth-xyz", hostname="hindsight")

        mock_cmd.assert_called_once_with(
            ["tailscale", "up", "--auth-key=tskey-auth-xyz", "--reset", "--hostname", "hindsight"]
        )

    def test_tailscale_serve(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        with patch.object(mgr, "_run_cmd") as mock_cmd:
            mgr.tailscale_serve(web_port=8080, tcp_ports=[5432, 7233, 8080])

        assert mock_cmd.call_count == 4
        calls = [call.args[0] for call in mock_cmd.call_args_list]
        assert calls[0] == ["tailscale", "serve", "reset"]
        assert calls[1] == [
            "tailscale",
            "serve",
            "--bg",
            "--yes",
            "--https",
            "443",
            "http://localhost:8080",
        ]
        assert calls[2] == [
            "tailscale",
            "serve",
            "--bg",
            "--yes",
            "--tcp",
            "5432",
            "tcp://localhost:5432",
        ]
        assert calls[3] == [
            "tailscale",
            "serve",
            "--bg",
            "--yes",
            "--tcp",
            "7233",
            "tcp://localhost:7233",
        ]

    def test_tailscale_serve_reset_ignores_compose_error(self, tmp_path: Path) -> None:
        mgr = ComposeManager(work_dir=tmp_path)
        with patch.object(mgr, "_run_cmd", side_effect=ComposeError("not running")):
            mgr.tailscale_serve_reset()
