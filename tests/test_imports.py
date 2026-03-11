import os
import subprocess
import sys
from pathlib import Path


def test_import():
    import repokit_dmp  # noqa: F401


def test_load_json_empty(tmp_path):
    from repokit_dmp.dmp import load_json

    empty = tmp_path / "dmp.json"
    empty.write_text("", encoding="utf-8")

    assert load_json(empty) == {}


def test_public_api_exports():
    from repokit_dmp.dataset import (
        dataset_path_update,
        dataset_to_readme,
        generate_dataset_table,
        get_data_files,
        main as dataset_main,
    )
    from repokit_dmp.dmp import (
        create_or_update_dmp_from_schema,
        ensure_dmp_shape,
        main as dmp_main,
    )

    assert callable(dataset_path_update)
    assert callable(dataset_to_readme)
    assert callable(generate_dataset_table)
    assert callable(get_data_files)
    assert callable(dataset_main)
    assert callable(create_or_update_dmp_from_schema)
    assert callable(ensure_dmp_shape)
    assert callable(dmp_main)


def test_import_dmp_does_not_mutate_pyproject(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname='tmp'\nversion='0.0.0'\n", encoding="utf-8")
    before = pyproject.read_text(encoding="utf-8")

    src_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-c", "import repokit_dmp.dmp"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert pyproject.read_text(encoding="utf-8") == before
