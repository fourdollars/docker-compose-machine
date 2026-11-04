"""Helper module for managing Docker Compose files, URL downloads, and CLI operations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_WORK_DIR = Path("/srv/docker-compose")


class ComposeError(Exception):
    """Raised when a Docker Compose configuration or CLI operation fails."""


def fetch_url(url: str, timeout: int = 30) -> str:
    """Download a remote docker-compose.yml file via HTTP or HTTPS."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ComposeError(f"URL must start with http:// or https://: {url!r}")

    req = Request(url.strip(), headers={"User-Agent": "docker-compose-machine-charm/0.1"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8")
    except (URLError, OSError, UnicodeDecodeError) as exc:
        raise ComposeError(f"Failed to download {url}: {exc}") from exc


def parse_git_url(url: str) -> tuple[str, str | None, str | None] | None:
    """Parse URL into (repo_url, branch, subpath) if it represents a Git source."""
    trimmed = url.strip()
    parsed = urlparse(trimmed)
    fragment = parsed.fragment
    netloc = parsed.netloc.lower()
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]

    # Handle raw.githubusercontent.com/<owner>/<repo>/<branch_or_ref>/<subpath>/file
    if netloc == "raw.githubusercontent.com" and len(parts) >= 3:
        owner, repo = parts[0], parts[1]
        rest = parts[2:]
        if len(rest) >= 2 and rest[0] == "refs" and rest[1] == "heads":
            branch = rest[2] if len(rest) > 2 else None
            subpath_parts = rest[3:-1] if len(rest) > 3 else []
        elif len(rest) >= 2:
            branch = rest[0]
            subpath_parts = rest[1:-1]
        else:
            branch = rest[0]
            subpath_parts = []
        subpath = "/".join(subpath_parts) if subpath_parts else None
        return f"https://github.com/{owner}/{repo}.git", branch, subpath

    # Handle github.com/<owner>/<repo>/blob/<ref>/<subpath>/file or tree/<ref>/<subpath>
    if netloc == "github.com" and len(parts) >= 4 and parts[2] in ("blob", "tree"):
        owner, repo = parts[0], parts[1]
        action = parts[2]
        branch = parts[3]
        if action == "blob":
            subpath_parts = parts[4:-1]
        else:
            subpath_parts = parts[4:]
        subpath = "/".join(subpath_parts) if subpath_parts else None
        return f"https://github.com/{owner}/{repo}.git", branch, subpath

    base_url = trimmed.split("#")[0].strip()
    subpath = fragment if fragment else None

    # Handle git://, git+http://, git+https://, git@, or ending in .git
    if (
        base_url.endswith(".git")
        or parsed.scheme in ("git", "git+http", "git+https")
        or base_url.startswith("git@")
    ):
        repo_url = base_url[4:] if base_url.startswith("git+") else base_url
        return repo_url, None, subpath

    # Handle github.com/<owner>/<repo>[#<subpath>]
    if netloc == "github.com" and len(parts) == 2:
        return f"https://github.com/{parts[0]}/{parts[1]}.git", None, subpath

    return None


def extract_ports_from_config(config_json: str) -> list[tuple[str, int]]:
    """Extract non-loopback published ports from `docker compose config --format json` output."""
    try:
        data = json.loads(config_json)
    except json.JSONDecodeError as exc:
        raise ComposeError(f"Invalid compose config JSON: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise ComposeError("Invalid compose config: missing 'services' mapping")

    ports_set: set[tuple[str, int]] = set()
    for svc in data["services"].values():
        if not isinstance(svc, dict):
            continue
        for port_entry in svc.get("ports") or []:
            if not isinstance(port_entry, dict):
                continue
            host_ip = str(port_entry.get("host_ip") or "")
            if host_ip in ("127.0.0.1", "::1"):
                continue
            published = port_entry.get("published")
            if published is None or published == "":
                continue
            protocol = str(port_entry.get("protocol") or "tcp").lower()
            if protocol not in ("tcp", "udp"):
                continue

            pub_str = str(published).strip()
            if "-" in pub_str:
                start_s, end_s = pub_str.split("-", 1)
                if start_s.isdigit() and end_s.isdigit():
                    start_p, end_p = int(start_s), int(end_s)
                    if 1 <= start_p <= end_p <= 65535:
                        for port_num in range(start_p, end_p + 1):
                            ports_set.add((protocol, port_num))
            elif pub_str.isdigit():
                port_num = int(pub_str)
                if 1 <= port_num <= 65535:
                    ports_set.add((protocol, port_num))

    return sorted(ports_set)


class ComposeManager:
    """Manages `/srv/docker-compose/compose.yaml`, `.env`, and `docker compose` CLI calls."""

    COMPOSE_FILENAMES = (
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    )

    def __init__(self, work_dir: Path = DEFAULT_WORK_DIR) -> None:
        self.work_dir = Path(work_dir)

    @property
    def compose_file(self) -> Path:
        """Path to compose file."""
        for name in self.COMPOSE_FILENAMES:
            candidate = self.work_dir / name
            if candidate.is_file():
                return candidate
        return self.work_dir / "compose.yaml"

    @property
    def env_file(self) -> Path:
        """Path to .env file."""
        return self.work_dir / ".env"

    def install_runtime(self) -> None:
        """Install docker.io, docker-compose-v2, and git via apt and enable docker.service."""
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        self._run_cmd(["apt-get", "update", "-y"], env=env)
        self._run_cmd(
            ["apt-get", "install", "-y", "docker.io", "docker-compose-v2", "git"],
            env=env,
        )
        self._run_cmd(["systemctl", "enable", "--now", "docker.service"])
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def write_env(self, env_content: str = "") -> None:
        """Write optional .env file with 0600 permissions."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        cleaned_env = env_content.strip()
        if cleaned_env:
            fd = os.open(
                self.env_file,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(cleaned_env + "\n")
            os.chmod(self.env_file, 0o600)
        elif self.env_file.exists():
            self.env_file.unlink()

    def write_files(self, raw_yaml: str, env_content: str = "") -> None:
        """Write compose.yaml and optional .env (with 0600 permissions) to work_dir."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "compose.yaml").write_text(raw_yaml, encoding="utf-8")
        self.write_env(env_content)

    def _clone_git_repo(
        self, repo_url: str, branch: str | None = None, subpath: str | None = None
    ) -> None:
        temp_dir = tempfile.mkdtemp(prefix="compose-git-")
        try:
            cmd = ["git", "clone", "--depth", "1"]
            if branch:
                cmd.extend(["--branch", branch])
            cmd.extend([repo_url, temp_dir])
            self._run_cmd(cmd)

            source_dir = Path(temp_dir)
            if subpath:
                source_dir = source_dir / subpath
                if not source_dir.is_dir():
                    raise ComposeError(
                        f"Subdirectory {subpath!r} not found in repository {repo_url}"
                    )

            self.work_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_dir, self.work_dir, dirs_exist_ok=True)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def setup_from_url(self, url: str, env_content: str = "") -> None:
        """Fetch remote compose configuration (Git repo or direct URL) and write .env."""
        git_info = parse_git_url(url)
        if git_info:
            repo_url, branch, subpath = git_info
            self._clone_git_repo(repo_url, branch=branch, subpath=subpath)
        else:
            raw_yaml = fetch_url(url)
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / "compose.yaml").write_text(raw_yaml, encoding="utf-8")

        self.write_env(env_content)
        if not self.compose_file.is_file():
            raise ComposeError(
                f"No compose file ({', '.join(self.COMPOSE_FILENAMES)}) found in {self.work_dir}"
            )

    def validate_and_get_ports(self) -> list[tuple[str, int]]:
        """Run `docker compose config --format json` and return published ports."""
        output = self._run_compose(["config", "--format", "json"])
        return extract_ports_from_config(output)

    def up(self) -> None:
        """Run `docker compose up -d --remove-orphans`."""
        self._run_compose(["up", "-d", "--remove-orphans"])

    def down(self) -> None:
        """Run `docker compose down --remove-orphans` if compose.yaml exists."""
        if self.compose_file.exists():
            self._run_compose(["down", "--remove-orphans"])

    def _run_compose(self, args: list[str]) -> str:
        cmd = [
            "docker",
            "compose",
            "--project-directory",
            str(self.work_dir),
            "-f",
            str(self.compose_file),
            *args,
        ]
        return self._run_cmd(cmd)

    @staticmethod
    def _run_cmd(cmd: list[str], env: dict[str, str] | None = None) -> str:
        kwargs: dict[str, object] = {
            "check": False,
            "capture_output": True,
            "text": True,
        }
        if env is not None:
            kwargs["env"] = env
        res = subprocess.run(cmd, **kwargs)  # type: ignore[call-overload]
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise ComposeError(f"Command {' '.join(cmd)} failed: {detail}")
        return res.stdout
