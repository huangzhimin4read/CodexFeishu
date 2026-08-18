"""Render and explicitly install a least-privilege hidden Scheduled Task."""

from __future__ import annotations

import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


def render_task_xml(*, python_executable: Path, config_path: Path, account: str) -> str:
    command = escape(str(python_executable.resolve()))
    arguments = escape(f'-m codex_feishu_bridge run --config "{config_path.resolve()}"')
    user = escape(account)
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>S4U</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Hidden>true</Hidden><StartWhenAvailable>true</StartWhenAvailable></Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>'''


def install_task(*, task_name: str, xml_path: Path) -> None:
    if not task_name.startswith("CodexFeishu-"):
        raise ValueError("task name must use the private CodexFeishu- prefix")
    subprocess.run(
        ["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(xml_path.resolve()), "/F"],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
