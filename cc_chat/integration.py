import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import tomlkit

from .config import ROOT, Settings


def migrate(settings: Settings, session: str, config: Path | None = None):
    config = config or settings.cc_data_dir / "config.toml"
    if not session.startswith("weixin:"):
        raise ValueError("必须明确指定个人微信会话标识")
    if len(str(settings.cc_data_dir / "run/api.sock").encode("utf-8")) >= 104:
        raise ValueError("cc-connect 数据目录过长，无法建立 Windows 本地通信套接字")
    original = config.read_bytes()
    doc = tomlkit.parse(original.decode("utf-8-sig"))
    project = next((p for p in doc.get("projects", []) if p.get("name") == settings.project), None)
    if project is None or project.get("agent", {}).get("type") != "claudecode":
        raise ValueError("未找到指定 Claude Code 项目")
    if not any(p.get("type") == "weixin" for p in project.get("platforms", [])):
        raise ValueError("目标项目没有微信平台")
    backup = ROOT / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True)
    (backup / "config.toml").write_bytes(original)
    local = ROOT / "config.local.json"
    if local.exists():
        shutil.copy2(local, backup / "config.local.json")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    options = project["agent"].setdefault("options", tomlkit.table())
    options["work_dir"] = str(settings.runtime_dir)
    python = str(Path(sys.executable).resolve())
    if " " in python:
        raise ValueError("本版本 cc-connect 的 cli_path 不支持空格路径，请使用无空格的 Python 路径")
    options["cli_path"] = python + " -m cc_chat.adapter"
    env = options.setdefault("env", tomlkit.table())
    env["CC_CHAT_CONFIG"] = str(local)
    # Disable default assistant/coding-session decoration only for this project.
    display = project.setdefault("display", tomlkit.table())
    # Our cc-connect patch also suppresses busy/startup queue receipts in quiet mode.
    display["mode"] = "quiet"
    for key in ["thinking_messages", "tool_messages", "show_context_indicator", "reply_footer"]:
        display[key] = False
    project["reset_on_idle_mins"] = 0
    for platform in project["platforms"]:
        if platform.get("type") == "weixin":
            peer = session.removeprefix("weixin:dm:")
            platform.setdefault("options", tomlkit.table())["allow_from"] = peer
    project["agent"]["type"] = "claudecode"
    settings.transport, settings.peer_session = "weixin", session
    settings.save(local)
    updated = tomlkit.dumps(doc).encode("utf-8")
    temp = config.with_suffix(".cc-chat.tmp")
    temp.write_bytes(updated)
    temp.replace(config)
    manifest = {
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "installed_sha256": hashlib.sha256(updated).hexdigest(),
        "config": str(config),
        "backup": str(backup),
        "project": settings.project,
    }
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
    return manifest


def restore(backup: Path, force=False):
    manifest = json.loads((backup / "manifest.json").read_text("utf-8"))
    target = Path(manifest["config"])
    if not force and hashlib.sha256(target.read_bytes()).hexdigest() != manifest["installed_sha256"]:
        raise ValueError("安装后 cc-connect 配置发生过修改；请人工合并或明确使用 --force")
    if hashlib.sha256((backup / "config.toml").read_bytes()).hexdigest() != manifest["original_sha256"]:
        raise ValueError("备份校验失败")
    shutil.copy2(backup / "config.toml", target)
    local = ROOT / "config.local.json"
    if (backup / "config.local.json").exists():
        shutil.copy2(backup / "config.local.json", local)
    else:
        settings = Settings.load()
        settings.transport = "mock"
        settings.peer_session = ""
        settings.save()
    return {"restored": str(target), "restart_required": True}


def install_startup():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        raise ValueError("Windows pythonw.exe 不存在")
    script = ROOT / "scripts" / "start-all.ps1"
    command = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script}"'
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", "AICHAT-cc-chat", "/SC", "ONLOGON", "/TR", command, "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return {"task": "AICHAT-cc-chat", "method": "schtasks"}
    # Locked-down Windows editions can deny schtasks even for the current user.
    # The per-user Startup folder has the same scope without requiring elevation.
    startup = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / \
        "Microsoft/Windows/Start Menu/Programs/Startup"
    startup.mkdir(parents=True, exist_ok=True)
    launcher = startup / "AICHAT-cc-chat.cmd"
    launcher.write_text(f'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script}"\r\n', encoding="utf-8")
    return {"task": "AICHAT-cc-chat", "method": "startup-folder", "launcher": str(launcher),
            "schtasks_error": (result.stderr or result.stdout).strip()[:300]}
