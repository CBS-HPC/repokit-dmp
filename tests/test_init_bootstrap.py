from __future__ import annotations

import json
from pathlib import Path

from repokit_common import read_toml


def test_init_project_creates_pyproject_and_dmp(tmp_path, monkeypatch):
    from repokit_dmp import init as dmp_init

    monkeypatch.chdir(tmp_path)

    calls: list[Path] = []

    def fake_create_or_update_dmp_from_schema(dmp_path: Path = Path("dmp.json")) -> Path:
        calls.append(dmp_path)
        dmp_path.write_text(json.dumps({"dmp": {"created": True}}), encoding="utf-8")
        return dmp_path

    monkeypatch.setattr(
        dmp_init,
        "create_or_update_dmp_from_schema",
        fake_create_or_update_dmp_from_schema,
    )

    result = dmp_init.init_project(force=False)

    assert result["created_dmp"] is True
    assert result["pyproject_path"] == tmp_path / "pyproject.toml"
    assert result["dmp_path"] == tmp_path / "dmp.json"
    assert calls == [tmp_path / "dmp.json"]

    datasets_cfg = read_toml(
        folder=str(tmp_path),
        json_filename=None,
        tool_name="datasets",
        toml_path="pyproject.toml",
    )
    data_policy_cfg = read_toml(
        folder=str(tmp_path),
        json_filename=None,
        tool_name="data_policy",
        toml_path="pyproject.toml",
    )

    assert datasets_cfg and datasets_cfg["patterns"] == "data/*"
    assert data_policy_cfg and "tool-description" in data_policy_cfg
    assert data_policy_cfg["patterns"] == []
    assert json.loads((tmp_path / "dmp.json").read_text(encoding="utf-8")) == {
        "dmp": {"created": True}
    }


def test_init_project_preserves_existing_dmp_without_force(tmp_path, monkeypatch):
    from repokit_dmp import init as dmp_init

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.datasets]\npatterns = [\"existing/data/*\"]\n",
        encoding="utf-8",
    )
    dmp_path = tmp_path / "dmp.json"
    dmp_path.write_text(json.dumps({"dmp": {"existing": True}}), encoding="utf-8")

    def fail_if_called(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("create_or_update_dmp_from_schema should not be called")

    monkeypatch.setattr(dmp_init, "create_or_update_dmp_from_schema", fail_if_called)

    result = dmp_init.init_project(force=False)

    assert result["created_dmp"] is False
    assert json.loads(dmp_path.read_text(encoding="utf-8")) == {"dmp": {"existing": True}}

    datasets_cfg = read_toml(
        folder=str(tmp_path),
        json_filename=None,
        tool_name="datasets",
        toml_path="pyproject.toml",
    )
    assert datasets_cfg and datasets_cfg["patterns"] == ["existing/data/*"]


def test_init_project_force_rebuilds_dmp(tmp_path, monkeypatch):
    from repokit_dmp import init as dmp_init

    monkeypatch.chdir(tmp_path)
    dmp_path = tmp_path / "dmp.json"
    dmp_path.write_text(json.dumps({"dmp": {"existing": True}}), encoding="utf-8")

    def fake_create_or_update_dmp_from_schema(dmp_path: Path = Path("dmp.json")) -> Path:
        dmp_path.write_text(json.dumps({"dmp": {"forced": True}}), encoding="utf-8")
        return dmp_path

    monkeypatch.setattr(
        dmp_init,
        "create_or_update_dmp_from_schema",
        fake_create_or_update_dmp_from_schema,
    )

    result = dmp_init.init_project(force=True)

    assert result["created_dmp"] is True
    assert json.loads(dmp_path.read_text(encoding="utf-8")) == {"dmp": {"forced": True}}
