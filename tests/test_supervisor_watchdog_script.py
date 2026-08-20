import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_supervisor_default_startup_grace_covers_cold_windows_imports() -> None:
    script = (
        Path(__file__).parents[1] / "scripts" / "run_supervised_remote_service.ps1"
    ).read_text(encoding="utf-8")

    assert "[int]$StartupGraceSeconds = 300" in script


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is unavailable")
def test_supervisor_terminates_a_live_worker_when_health_never_appears(tmp_path: Path) -> None:
    application = tmp_path / "app"
    package = application / "codex_feishu_bridge"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = tmp_path / "live.toml"
    config.write_text("# watchdog fixture\n", encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "run_supervised_remote_service.ps1"

    completed = subprocess.run(
        [
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-PythonPath",
            sys.executable,
            "-ApplicationRoot",
            str(application),
            "-ConfigPath",
            str(config),
            "-RuntimeRoot",
            str(runtime),
            "-TaskName",
            "CodexFeishu-Watchdog-Test",
            "-RestartDelaySeconds",
            "5",
            "-MaxRestartAttempts",
            "1",
            "-StartupGraceSeconds",
            "1",
            "-HealthStaleSeconds",
            "1",
            "-HealthPollSeconds",
            "1",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    watchdog = json.loads((runtime / "broker-supervisor-last-watchdog.json").read_text(encoding="utf-8"))
    assert watchdog["reason"] == "health_missing"
    assert watchdog["process_id"] == int((runtime / "topic-group-service.pid").read_text().strip())
    launch = json.loads((runtime / "remote-service-launch.json").read_text(encoding="utf-8"))
    assert launch["watchdog_reason"] == "health_missing"
