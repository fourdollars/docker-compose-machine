import os
import subprocess
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("failures", "failing_file", "succeeds"),
    [
        (0, "amd64", True),
        (1, "amd64", True),
        (2, "arm64", True),
        (3, "amd64", False),
        (3, "arm64", False),
    ],
)
def test_charmhub_upload_retries(tmp_path, failures, failing_file, succeeds):
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/publish.yaml").read_text()
    upload_step = workflow.split("      - name: Upload charms to CharmHub\n", 1)[1]
    script = textwrap.dedent(
        upload_step.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
    )
    mocks = """
    charmcraft() {
      echo "$2" >> attempts
      if [[ "$2" == *"$FAILING_FILE"* ]] && (( FAILURES > 0 )); then
        FAILURES=$((FAILURES - 1))
        echo '{"revision":999}'
        return 1
      fi
      if [[ "$2" == *amd64* ]]; then
        echo '{"revision":101}'
      else
        echo '{"revision":102}'
      fi
    }
    sleep() { echo "$1" >> sleeps; }
    """
    for arch in ("amd64", "arm64"):
        (tmp_path / f"docker-compose-machine_{arch}.charm").touch()
    github_env = tmp_path / "github-env"
    result = subprocess.run(
        ["bash", "-e", "-c", textwrap.dedent(mocks) + script],
        cwd=tmp_path,
        env={
            **os.environ,
            "FAILURES": str(failures),
            "FAILING_FILE": failing_file,
            "GITHUB_ENV": str(github_env),
        },
        capture_output=True,
        text=True,
    )
    attempts = (tmp_path / "attempts").read_text().splitlines()
    expected = []
    for arch in ("amd64", "arm64"):
        count = min(failures + 1, 3) if arch == failing_file else 1
        expected.extend([f"docker-compose-machine_{arch}.charm"] * count)
        if arch == failing_file and not succeeds:
            break
    assert attempts == expected
    sleeps = tmp_path / "sleeps"
    assert (sleeps.read_text().splitlines() if sleeps.exists() else []) == ["10"] * min(failures, 2)
    if succeeds:
        assert result.returncode == 0, result.stderr
        assert github_env.read_text() == "revisions= 101 102\n"
    else:
        assert result.returncode == 1
        assert "after 3 attempts" in result.stdout
        assert not github_env.exists()
