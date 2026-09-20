import json
from pathlib import Path

import pytest
import tomlkit

from cc_chat.config import Settings
from cc_chat.integration import migrate, restore


def test_migration_and_rollback_preserve_original_bytes(tmp_path, monkeypatch):
    import cc_chat.integration as integration

    monkeypatch.setattr(integration, "ROOT", tmp_path)
    config = tmp_path / "cc.toml"
    doc = {
        "projects": [
            {
                "name": "default",
                "agent": {"type": "claudecode", "options": {"work_dir": r"D:\ObsidianData\Note"}},
                "platforms": [
                    {"type": "weixin", "options": {"token": "not-a-real-secret", "allow_from": "*"}}
                ],
            }
        ]
    }
    original = ("# retain my configuration\n" + tomlkit.dumps(doc)).encode()
    config.write_bytes(original)
    (tmp_path / "config.local.json").write_text(
        json.dumps({"transport": "mock"}), encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime", cc_data_dir=Path(r"D:\short")
    )
    manifest = migrate(settings, "weixin:dm:owner", config)
    installed = tomlkit.parse(config.read_text("utf-8"))
    p = installed["projects"][0]
    assert p["agent"]["options"]["work_dir"] == str(settings.runtime_dir)
    assert "cc_chat.adapter" in p["agent"]["options"]["cli_path"]
    assert p["platforms"][0]["options"]["token"] == "not-a-real-secret"
    assert p["platforms"][0]["options"]["allow_from"] == "owner"
    assert Path(manifest["backup"], "config.toml").read_bytes() == original
    restore(Path(manifest["backup"]))
    assert config.read_bytes() == original


def test_custom_settings_save_back_to_same_file(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings()
    s.save(p)
    loaded = Settings.load(p)
    loaded.compress_days = 20
    loaded.save()
    assert json.loads(p.read_text())["compress_days"] == 20


def test_long_socket_path_rejected_before_mutation(tmp_path):
    config = tmp_path / "cc.toml"
    config.write_text("untouched")
    with pytest.raises(ValueError, match="过长"):
        migrate(Settings(cc_data_dir=Path("D:/" + "long" * 40)), "weixin:dm:owner", config)
    assert config.read_text() == "untouched"
