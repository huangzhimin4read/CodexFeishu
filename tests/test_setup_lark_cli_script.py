from pathlib import Path


def test_lark_cli_setup_uses_official_package_and_guided_auth_without_secrets() -> None:
    script = (
        Path(__file__).parents[1] / "scripts" / "setup_lark_cli.ps1"
    ).read_text(encoding="utf-8")

    assert 'install -g "@larksuite/cli"' in script
    assert "config init --new" in script
    assert "auth login --recommend" in script
    assert "auth status --json --verify" in script
    assert "RequireUserIdentity" in script
    assert "app_secret" not in script.casefold()
    assert "access_token" not in script.casefold()


def test_broker_installer_runs_lark_cli_guide_by_default() -> None:
    script = (
        Path(__file__).parents[1] / "scripts" / "install_broker_autorestart.ps1"
    ).read_text(encoding="utf-8")

    assert "setup_lark_cli.ps1" in script
    assert "SkipLarkCliSetup" in script
    assert "RequireUserIdentity" in script
    assert "& $larkCliSetup -Profile $LarkCliProfile" in script


def test_broker_installer_uses_stable_windows_powershell_for_scheduled_task() -> None:
    script = (
        Path(__file__).parents[1] / "scripts" / "install_broker_autorestart.ps1"
    ).read_text(encoding="utf-8")

    assert "$env:WINDIR" in script
    assert "System32\\WindowsPowerShell\\v1.0\\powershell.exe" in script
    assert "Join-Path $PSHOME 'powershell.exe'" not in script
