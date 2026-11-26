"""Tests of ``setup.sh``: the real script runs in a subprocess, in this checkout.

The cases with ``uv`` or ``cargo`` missing use a reduced ``PATH`` (a folder of
links to a few system tools, without the one that is to be missing). Nothing
here installs uv or Rust: the installers are only ever printed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "setup.sh"
UV = shutil.which("uv")
CARGO = shutil.which("cargo")

UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"
RUST_INSTALL = "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
START = "uv run brain-mapper"
SYSTEM_TOOLS = [
    "bash",
    "sh",
    "dirname",
    "uname",
    "id",
    "tr",
    "grep",
    "stat",
    "curl",
    "env",
    "cat",
]
OWN_LINES = ("PASS uv:", "PASS rust:", "PASS environment:", "PASS check:")


def base_env(path: str, home: str | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")
    }
    env["PATH"] = path
    if home is not None:
        env["HOME"] = home
    return env


def run(
    args: list[str], env: dict[str, str], stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=900,
    )


def reduced_path(tmp_path: Path, keep: list[str | None]) -> str:
    """A PATH of one folder holding links to the system tools and to `keep`."""
    folder = tmp_path / "bin"
    folder.mkdir()
    for name in SYSTEM_TOOLS:
        found = shutil.which(name)
        assert found, f"{name} is needed by the tests"
        (folder / name).symlink_to(found)
    for found in keep:
        assert found, "the real uv or cargo is needed for this test"
        (folder / Path(found).name).symlink_to(found)
    return str(folder)


def git_status() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def freeze() -> str:
    out = subprocess.run(
        ["uv", "pip", "freeze"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        env=base_env(os.environ["PATH"]),
    )
    return out.stdout


def lines_starting(out: str, word: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(word)]


def test_full_run_passes_every_step_and_prints_the_start_command() -> None:
    proc = run([], base_env(os.environ["PATH"]))
    out = proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for step in ("uv", "rust", "environment", "check"):
        assert len(lines_starting(out, f"PASS {step}:")) == 1, out
    assert not lines_starting(out, "FAIL"), out
    # `brain-mapper --check` prints its own lines, each starting with PASS or
    # FAIL, before the script's `PASS check:` line.
    before = out.split("PASS check:")[0].splitlines()
    own = [x for x in before if x.startswith("PASS ") and not x.startswith(OWN_LINES)]
    assert own, "brain-mapper --check printed no PASS line of its own:\n" + out
    assert out.rstrip().splitlines()[-1].endswith(START), out


def test_second_run_changes_nothing() -> None:
    env = base_env(os.environ["PATH"])
    first = run([], env)
    assert first.returncode == 0, first.stdout + first.stderr
    lock = (REPO / "uv.lock").read_bytes()
    packages = freeze()
    status = git_status()
    second = run([], env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert (REPO / "uv.lock").read_bytes() == lock
    assert freeze() == packages
    assert git_status() == status
    assert lines_starting(second.stdout, "PASS environment:")


def test_without_uv_and_answer_no_stops_and_installs_nothing(tmp_path: Path) -> None:
    path = reduced_path(tmp_path, keep=[CARGO])
    assert shutil.which("uv", path=path) is None
    before = git_status()
    proc = run([], base_env(path), stdin="n\n")
    assert proc.returncode != 0
    assert "FAIL uv:" in proc.stdout
    assert UV_INSTALL in proc.stdout
    assert not lines_starting(proc.stdout, "PASS rust")
    assert START not in proc.stdout
    assert shutil.which("uv", path=path) is None
    assert git_status() == before


def test_without_uv_and_no_answer_is_a_no(tmp_path: Path) -> None:
    path = reduced_path(tmp_path, keep=[CARGO])
    proc = run([], base_env(path), stdin="")
    assert proc.returncode != 0
    assert UV_INSTALL in proc.stdout


def test_without_cargo_stops_at_step_two_with_the_install_command(
    tmp_path: Path,
) -> None:
    path = reduced_path(tmp_path, keep=[UV])
    assert shutil.which("cargo", path=path) is None
    before = git_status()
    proc = run([], base_env(path))
    assert proc.returncode != 0
    assert "PASS uv:" in proc.stdout
    assert "FAIL rust:" in proc.stdout
    assert RUST_INSTALL in proc.stdout
    assert "environment" not in proc.stdout
    assert START not in proc.stdout
    assert git_status() == before


def test_unknown_option_is_refused() -> None:
    proc = run(["--install-everything"], base_env(os.environ["PATH"]))
    assert proc.returncode == 2
    assert "unknown option" in proc.stderr


def test_nothing_is_written_outside_the_repository_and_uv_folders(
    tmp_path: Path,
) -> None:
    """A run with an empty home folder writes no file there (no profile edits, no dot files)."""
    home = tmp_path / "home"
    home.mkdir()
    env = base_env(os.environ["PATH"], home=str(home))
    # uv, cargo and rustup keep their own folders where they are.
    real_home = Path.home()
    env["UV_CACHE_DIR"] = subprocess.run(
        ["uv", "cache", "dir"], capture_output=True, text=True, check=True
    ).stdout.strip()
    env["UV_PYTHON_INSTALL_DIR"] = subprocess.run(
        ["uv", "python", "dir"], capture_output=True, text=True, check=True
    ).stdout.strip()
    env["UV_PYTHON_DOWNLOADS"] = "never"
    env["CARGO_HOME"] = os.environ.get("CARGO_HOME", str(real_home / ".cargo"))
    env["RUSTUP_HOME"] = os.environ.get("RUSTUP_HOME", str(real_home / ".rustup"))
    proc = run([], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The only thing allowed is the (empty) data folder that `brain-mapper
    # --check` makes; no file at all, so no profile, dot file or config.
    files = [x for x in home.rglob("*") if not x.is_dir()]
    assert files == []
    assert {x.name for x in home.iterdir()} <= {"brain-mapper"}


def test_script_has_no_sudo_and_names_no_shell_profile() -> None:
    text = SCRIPT.read_text()
    code = "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#"))
    assert "sudo " not in code.replace("Run: sudo usermod", "")
    for profile in (".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile"):
        assert profile not in code
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
