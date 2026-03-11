from . import dataset_discovery_and_policy as _dataset_discovery_and_policy

globals().update(
    {
        _name: getattr(_dataset_discovery_and_policy, _name)
        for _name in dir(_dataset_discovery_and_policy)
        if not _name.startswith("_")
    }
)
del _dataset_discovery_and_policy
def generate_dataset_table(
    json_path: str,
    file_descriptions: dict[str, str] | None = None,
    include_hash: bool = False,
) -> tuple[str | None, str | None]:
    if not os.path.exists(json_path):
        return None, None

    with open(json_path, encoding="utf-8") as fh:
        json_data = json.load(fh)

    dmp = ensure_dmp_shape(json_data)["dmp"]
    datasets = dmp.get("dataset", [])

    hidden_fields = set()
    if not include_hash:
        hidden_fields.add("hash")

    def is_nonempty(val):
        return val not in (None, "", [], {}, "N/A", "Not provided")

    def safe_str(val):
        return "N/A" if val in (None, "", [], {}, "Not provided") else str(val)

    rows = []
    dynamic_id_fields: set[str] = set()  # <-- collect dynamic keys from dataset_id.type

    for ds in datasets:
        x = get_repokit_info_payload(ds) or {}
        dist = (ds.get("distribution") or [{}])[0]
        destination = dist.get("download_url") or dist.get("access_url") or x.get("destination")
        is_restricted = _is_restricted_dataset_path(destination or "")

        raw_access = (dist.get("data_access") or "").strip().lower()
        data_access = "closed" if is_restricted else (raw_access or "closed")

        licenses = dist.get("license") or []
        if isinstance(licenses, dict):
            licenses = [licenses]
        if licenses and isinstance(licenses[0], dict):
            license_ref = (licenses[0].get("license_ref") or "").strip()
        else:
            license_ref = ""
        if is_restricted:
            license_ref = ""

        # Build the row (unchanged fields kept as-is)
        # Normalize file formats to a list for safe rendering
        fmts = dist.get("format")
        if isinstance(fmts, str):
            fmts_list = [fmts]
        elif isinstance(fmts, (list, set, tuple)):
            fmts_list = list(fmts)
        else:
            fmts_list = []
        fmts_list = sorted(list(set(fmts_list)))

        row = {
            "data_name": ds.get("title"),
            "destination": destination,
            "data_access": data_access,
            "license_ref": license_ref,
            "created": ds.get("issued"),
            "lastest_change": ds.get("modified"),
            "hash": x.get("hash"),
            "provided": "Provided" if x.get("data_files") else "Can be re-created",
            "number_of_files": x.get("number_of_files"),
            "total_size_mb": x.get("total_size_mb")
            if x.get("total_size_mb") is not None
            else int(round((dist.get("byte_size") or 0) / (1024 * 1024))),
            "file_formats": fmts_list,
            "zip_file": None,
            "description": ds.get("description"),
            "_dtype": x.get("data_type") or data_type_from_path(x.get("destination") or ""),
            "_files": x.get("data_files") or [],
            "_sizes": x.get("data_size_mb") or [],
        }

        # --- Dynamic identifier field ---
        ds_id = ds.get("dataset_id") or {}
        if isinstance(ds_id, list) and ds_id:
            ds_id = ds_id[0]

        # Coerce empty/whitespace strings to None
        raw_identifier = ds_id.get("identifier")
        if isinstance(raw_identifier, str):
            ds_id_identifier = raw_identifier.strip() or None
        else:
            ds_id_identifier = raw_identifier

        raw_type = ds_id.get("type")
        ds_id_type = (raw_type.strip() if isinstance(raw_type, str) else raw_type) or None

        # Add a dynamic field named by the identifier type, with value = identifier (possibly None)
        if ds_id_type:
            row[str(ds_id_type)] = ds_id_identifier
            dynamic_id_fields.add(str(ds_id_type))

        rows.append(row)

    grouped = defaultdict(list)
    for r in rows:
        grouped[r["_dtype"]].append(r)

    # Standard (fixed) columns
    standard_fields = {
        "data_name": "Name",
        "destination": "Location",
        "data_access": "Access",
        "license_ref": "License Ref",
        "created": "Created",
        "lastest_change": "Lastest Change",
        "hash": "Hash",
        "provided": "Provided",
        "number_of_files": "Number of Files",
        "total_size_mb": "Total Size (MB)",
        "file_formats": "File Formats",
        "zip_file": "Zip File",
        "description": "Description",
    }
    if not include_hash:
        standard_fields.pop("hash", None)

    # Add dynamic identifier columns (header = key itself)
    for dyn in sorted(dynamic_id_fields):
        standard_fields[dyn] = dyn

    # Pick the active columns to show (non-empty in at least one row)
    active_fields = [
        k
        for k in standard_fields
        if k not in hidden_fields and any(is_nonempty(r.get(k)) for r in rows)
    ]

    summary_header = "| " + " | ".join([standard_fields[k] for k in active_fields]) + " |\n"
    summary_divider = (
        "| " + " | ".join(["-" * len(standard_fields[k]) for k in active_fields]) + " |\n"
    )

    base_detail = [
        "data_name",
        "data_files",
        "destination",
        "data_access",
        "license_ref",
        "created",
        "lastest_change",
        "provided",
        "data_size",
    ]
    if (
        include_hash
        and "hash" not in hidden_fields
        and any(is_nonempty(r.get("hash")) for r in rows)
    ):
        base_detail.insert(5, "hash")

    # Include dynamic identifier fields in the detail table as well
    base_detail = base_detail + sorted(dynamic_id_fields)

    detail_header = "| " + " | ".join([f.replace("_", " ").title() for f in base_detail]) + " |\n"
    detail_divider = "| " + " | ".join(["-" * len(f) for f in base_detail]) + " |\n"

    summary_blocks: list[str] = []
    detail_blocks: list[str] = []

    for dtype, entries in sorted(grouped.items()):
        desc = (
            f" <- {file_descriptions.get(dtype, '')}"
            if (file_descriptions and dtype in file_descriptions)
            else None
        )
        header = f"### {dtype} {desc}\n" if desc else f"### {dtype}\n"
        summary_blocks.append(header + summary_header + summary_divider)

        need_detail = any(len(r["_files"]) > 1 for r in entries)
        if need_detail:
            detail_blocks.append(header + detail_header + detail_divider)

        for r in entries:
            # Summary row
            vals = []
            for k in active_fields:
                if k == "file_formats":
                    fmts = r.get(k) or []
                    if isinstance(fmts, (list, tuple, set)):
                        val = "; ".join("." + f for f in fmts) or "N/A"
                    else:
                        val = "." + str(fmts) if fmts else "N/A"
                else:
                    val = r.get(k, "N/A")
                vals.append(safe_str(val))
            summary_blocks.append("| " + " | ".join(vals) + " |\n")

            # Detail rows
            if need_detail:
                files = r["_files"]
                sizes = r["_sizes"]
                if len(sizes) < len(files):
                    sizes += ["?"] * (len(files) - len(sizes))
                for f, sz in zip(files, sizes, strict=False):
                    detail_vals = []
                    for k in base_detail:
                        if k == "data_files":
                            detail_vals.append(safe_str(f))
                        elif k == "data_size":
                            detail_vals.append(safe_str(sz))
                        else:
                            detail_vals.append(safe_str(r.get(k)))
                    detail_blocks.append("| " + " | ".join(detail_vals) + " |\n")

        summary_blocks.append("\n")
        if need_detail:
            detail_blocks.append("\n")

    return "".join(summary_blocks), "".join(detail_blocks)


def dataset_to_readme(markdown_table: str, readme_file: str = "./README.md", do_print: bool = True):
    section_title = "**The following datasets are included in the project:**"
    readme_path = PROJECT_ROOT / pathlib.Path(readme_file)
    new_section = f"{section_title}\n\n{markdown_table.strip()}\n</details>"
    try:
        content = readme_path.read_text(encoding="utf-8")
        if section_title in content:
            start = content.find(section_title)
            closing_tag = "</details>"
            close_idx = content.find(closing_tag, start)
            if close_idx != -1:
                end = close_idx + len(closing_tag)
            else:
                end = content.find("\n## ", start + len(section_title))
                end = end if end != -1 else len(content)
            updated = content[:start] + new_section + content[end:]
        else:
            updated = content.rstrip() + "\n\n" + new_section
    except FileNotFoundError:
        updated = new_section

    readme_path.parent.mkdir(parents=True, exist_ok=True)
    readme_path.write_text(updated.strip(), encoding="utf-8")
    if do_print:
        print(f"{readme_path} successfully updated with dataset section.")


def dataset_path_update(
    data_files: list[str] | None = None,
    dmp_path: str = DEFAULT_DMP_PATH,
    git_msg: str = None,
    default_dataset_path:dict=None,
):
    if isinstance(data_files, str):
        data_files = [data_files]

    if not data_files:
        return
    if not git_msg:
        git_msg = f"Setting dataset path for: {data_files[0]}"

    os.chdir(PROJECT_ROOT)

    if default_dataset_path is None:
        default_dataset_path, _ = toml_dataset_path()

    file_descriptions = read_toml(
        folder = PROJECT_ROOT,
        json_filename = "./file_descriptions.json",
        tool_name = "file_descriptions",
        toml_path = "pyproject.toml",
    )

    change_flag = False
    for f in data_files:
        flag, dmp_path = dataset(destination=f, json_path=dmp_path, do_print=False)
        if not flag:
            continue
        change_flag = True
        if os.path.exists(".datalad"):
            if set_datalad:
                set_datalad(f)
        elif os.path.exists(".dvc"):
            if set_dvc:
                set_dvc(f)

    try:
        markdown_table, _ = generate_dataset_table(dmp_path, file_descriptions)
        if markdown_table:
            dataset_to_readme(markdown_table=markdown_table, do_print=False)
    except Exception as e:
        print(f"Error: {e}")

    try:
        _ = _sync_sensitive_policy_artifacts_from_dmp(dmp_path)
    except Exception as e:
        print(f"Error syncing sensitive policy artifacts: {e}")

    if (
        change_flag
        and os.path.exists(".git")
        and not os.path.exists(".datalad")
        and not os.path.exists(".dvc")
    ):
        with change_dir(default_dataset_path["parent_path"]):
            if os.path.exists(".git"):
                if git_commit:
                    _ = git_commit(msg=git_msg, path=os.getcwd())
                if git_log_to_file:
                    git_log_to_file(os.path.join(".gitlog"))


@ensure_correct_kernel
def main(
    dmp_path: str = DEFAULT_DMP_PATH,
    do_print: bool = True,
    git_msg: str = "Running 'set-dataset'",
    default_dataset_path:dict=None,
):
    global PROJECT_ROOT
    PROJECT_ROOT = bootstrap_runtime_root()

    if os.path.exists(".datalad"):
        if datalad_cleaning:
            datalad_cleaning(PROJECT_ROOT)
    elif os.path.exists(".dvc"):
        if dvc_cleaning:
            dvc_cleaning(PROJECT_ROOT)

    if default_dataset_path is None:
        default_dataset_path, _ = toml_dataset_path()

    data_files, _ = get_data_files(cfg=default_dataset_path, ignore=IGNORE_DICT)

    create_or_update_dmp_from_schema(dmp_path=dmp_path)

    json_path = remove_missing_datasets(json_path=dmp_path)

    if not data_files:
        return

    file_descriptions = read_toml(
        folder=PROJECT_ROOT,
        json_filename="./file_descriptions.json",
        tool_name="file_descriptions",
        toml_path="pyproject.toml",
    )

    change_flag = False
    for f in data_files:
        flag, json_path = dataset(destination=f, json_path=json_path, do_print=do_print)
        if not flag:
            continue
        change_flag = True
        if os.path.exists(".datalad"):
            if set_datalad:
                set_datalad(f)
        elif os.path.exists(".dvc"):
            if set_dvc:
                set_dvc(f)

    try:
        markdown_table, _ = generate_dataset_table(json_path, file_descriptions)
        if markdown_table:
            dataset_to_readme(markdown_table, do_print=do_print)
    except Exception as e:
        print(f"Error: {e}")

    try:
        _ = _sync_sensitive_policy_artifacts_from_dmp(json_path)
    except Exception as e:
        print(f"Error syncing sensitive policy artifacts: {e}")

    if (
        change_flag
        and os.path.exists(".git")
        and not os.path.exists(".datalad")
        and not os.path.exists(".dvc")
    ):
        with change_dir(default_dataset_path["parent_path"]):
            if os.path.exists(".git"):
                if git_commit:
                    _ = git_commit(msg=git_msg, path=os.getcwd())
                if git_log_to_file:
                    git_log_to_file(os.path.join(".gitlog"))


if __name__ == "__main__":
    main()
