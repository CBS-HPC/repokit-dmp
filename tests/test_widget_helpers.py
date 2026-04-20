from __future__ import annotations


def test_dataset_heading_label_marks_empty_scans():
    from repokit_dmp.editor import sections_root_projects as srp

    ds = {
        "title": "taqtrade_dummy",
        "extension": [{"repokit_info": {"number_of_files": 0, "data_files": []}}],
    }

    assert srp._dataset_heading_label(ds, 3, "taqtrade_dummy") == (
        "Dataset #4: taqtrade_dummy (Empty)"
    )


def test_dataset_heading_label_ignores_unscanned_datasets():
    from repokit_dmp.editor import sections_root_projects as srp

    ds = {"title": "taqtrade_dummy", "extension": [{"repokit_info": {}}]}

    assert srp._dataset_heading_label(ds, 0, "taqtrade_dummy") == "Dataset #1: taqtrade_dummy"


def test_resolve_browse_start_path_falls_back_to_project_root(tmp_path, monkeypatch):
    from repokit_dmp.editor import widget_helpers as wh

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(wh, "PROJECT_ROOT", project_root)

    resolved = wh._resolve_browse_start_path("missing/subdir")

    assert resolved == project_root


def test_resolve_browse_start_path_uses_existing_path(tmp_path, monkeypatch):
    from repokit_dmp.editor import widget_helpers as wh

    project_root = tmp_path / "project"
    project_root.mkdir()
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    monkeypatch.setattr(wh, "PROJECT_ROOT", project_root)

    resolved = wh._resolve_browse_start_path(existing_dir)

    assert resolved == existing_dir.resolve()
