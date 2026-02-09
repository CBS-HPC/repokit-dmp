def test_import():
    import repokit_dmp  # noqa: F401


def test_load_json_empty(tmp_path):
    from repokit_dmp.dmp import load_json

    empty = tmp_path / "dmp.json"
    empty.write_text("", encoding="utf-8")

    assert load_json(empty) == {}