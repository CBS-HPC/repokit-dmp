from .metadata_and_paths import (
    Any,
    DEFAULT_DMP_PATH,
    DEFAULT_UPDATE_DIST_FIELDS,
    DEFAULT_UPDATE_FIELDS,
    IGNORE_DICT,
    Iterable,
    LICENSE_LINKS,
    PROJECT_ROOT,
    _is_restricted_dataset_path,
    _pseudonymize_data_files,
    check_path_format,
    data_type_from_path,
    datetime,
    deepcopy,
    dmp_default_templates,
    get_all_files,
    get_file_info,
    get_hash,
    get_repokit_info_payload,
    load_json,
    norm_rel_urlish,
    now_iso_minute,
    os,
    pathlib,
    read_toml,
    save_json,
    set_repokit_info_payload,
    to_bytes_mb,
)
def datasets_to_json(
    json_path=DEFAULT_DMP_PATH,
    entry=None,
    update_fields: list[str] | None = None,
    update_distribution_fields: list[str] | None = None,
    bump_modified_on_distribution_updates: bool = False,
    do_print: bool = True,
):
    """
    Upsert a dataset entry into {"dmp": {"dataset": [...]}}.

    Matching:
      - prefer by distribution rel URL (url_acess/url_access/access_url/download_url)
      - else by title

    Update policy (when a match is found):
      - changed_flag := (existing.repokit_info.hash != entry.repokit_info.hash)
      - overwrite ONLY:
          * repokit_info payload in `extension` (if provided)
          * top-level fields in `update_fields`
          * nested distribution fields in `update_distribution_fields`, matched by rel URL
      - modified := now_iso_minute() iff changed_flag
        (or also when distribution fields changed, if bump_modified_on_distribution_updates=True)
    """
    if update_fields is None:
        update_fields = DEFAULT_UPDATE_FIELDS
    if update_distribution_fields is None:
        update_distribution_fields = DEFAULT_UPDATE_DIST_FIELDS

    json_path = PROJECT_ROOT / pathlib.Path(json_path)
    data = load_json(json_path)
    dmp = data["dmp"]
    datasets = dmp.get("dataset", [])

    any_change_flag = False

    def _extract_rel_url(dist: dict) -> str | None:
        for k in ("url_acess", "url_access", "access_url", "download_url"):
            v = (dist or {}).get(k)
            if v:
                return norm_rel_urlish(v)
        return None

    def _collect_rel_urls(ds: dict) -> set[str]:
        out: set[str] = set()
        for d in (ds or {}).get("distribution", []) or []:
            u = _extract_rel_url(d)
            if u:
                out.add(u)
        return out

    new_rel_urls = _collect_rel_urls(entry or {})
    idx = None
    if new_rel_urls:
        for i, ds in enumerate(datasets):
            if _collect_rel_urls(ds) & new_rel_urls:
                idx = i
                break
    else:
        for i, ds in enumerate(datasets):
            if (ds or {}).get("title") == (entry or {}).get("title"):
                idx = i
                break

    if idx is not None:
        existing = datasets[idx]
        merged = deepcopy(existing)

        # --- 1) changed_flag from repokit_info.hash only ---
        existing_x = get_repokit_info_payload(existing) or {}
        entry_x = get_repokit_info_payload(entry or {}) or {}
        existing_hash = existing_x.get("hash")
        entry_hash = entry_x.get("hash")
        changed_flag = existing_hash != entry_hash

        # --- 2) optionally replace/insert repokit_info payload ---
        if entry_x:
            set_repokit_info_payload(merged, entry_x)

        # --- 3) overwrite ONLY whitelisted top-level fields ---
        for k in update_fields:
            if k in (entry or {}) and (entry[k] is not None):
                merged[k] = entry[k]

        # --- 4) overwrite ONLY whitelisted nested fields in distribution (URL-matched) ---
        dist_changed = False
        if "distribution" in (entry or {}):
            incoming_list = entry.get("distribution") or []
            existing_list = list(merged.get("distribution") or [])
            # map existing distributions by rel URL
            existing_by_u = {}
            for d in existing_list:
                u = _extract_rel_url(d or {})
                if u:
                    existing_by_u[u] = d
            # apply field updates for matched items
            for s in incoming_list:
                u = _extract_rel_url(s or {})
                if not u or u not in existing_by_u:
                    continue
                tgt = existing_by_u[u]
                for f in update_distribution_fields:
                    if f in s and s[f] is not None and s[f] != tgt.get(f):
                        tgt[f] = s[f]
                        dist_changed = True
            if dist_changed:
                merged["distribution"] = existing_list

        # --- 5) modified policy ---
        if changed_flag or (bump_modified_on_distribution_updates and dist_changed):
            merged["modified"] = now_iso_minute()
        else:
            merged["modified"] = existing.get("modified")

        record_changed = merged != existing
        datasets[idx] = merged
        if record_changed:
            any_change_flag = True

        if changed_flag:
            if do_print:
                print(f"Updated DMP entry for {existing_x.get('title', merged.get('title'))}.")
            any_change_flag = True
        else:
            if do_print:
                print(
                    f"No changes detected for DMP entry: {existing_x.get('title', merged.get('title'))}."
                )

    else:
        entry_x = get_repokit_info_payload(entry or {}) or {}
        datasets.append(entry)
        if do_print:
            print(f"Added DMP entry for {entry_x.get('destination', entry.get('title'))}.")
        any_change_flag = True

    # Sort by repokit_info.data_type then title
    def _sort_key(ds):
        x = get_repokit_info_payload(ds) or {}
        return (x.get("data_type") or "", ds.get("title") or "")

    datasets.sort(key=_sort_key)

    dmp["dataset"] = datasets
    if any_change_flag:
        dmp["modified"] = now_iso_minute()
        save_json(json_path, data)

    return any_change_flag, json_path


def remove_missing_datasets(json_path: str | os.PathLike = DEFAULT_DMP_PATH):
    """
    Ensure the DMP file exists and is shaped. For any dataset whose
    access/download URL (or extension.repokit_info.destination) no longer exists on disk,
    DO NOT delete the dataset; instead set:
      - extension.repokit_info = {}  (as {"repokit_info": {}} in a list-shaped extension)
      - distribution[*].access_url = ""

    Returns the absolute Path to the JSON file.
    """
    # Resolve path relative to project root (3 levels up from this file)
    root = PROJECT_ROOT
    json_path = (root / pathlib.Path(json_path)).resolve()

    # ---- Load & shape ----
    data = load_json(json_path) or {}
    if not isinstance(data, dict):
        data = {}

    dmp = data.get("dmp") or {}
    data["dmp"] = dmp  # reattach so mutations persist
    datasets = dmp.get("dataset")
    if not isinstance(datasets, list):
        datasets = []
    dmp["dataset"] = datasets  # ensure list exists

    # ---- Helpers ----
    def _looks_remote(value: str | None) -> bool:
        if not value or not isinstance(value, str):
            return False
        v = value.strip()
        if not v:
            return False
        if "://" in v:
            return True
        if v.startswith("www."):
            return True
        # Treat rclone-like refs (e.g., dropbox:path) as remote, but not Windows drives (C:\...)
        if ":" in v and not (len(v) >= 2 and v[1] == ":"):
            return True
        return False

    def _exists_on_disk(ds: dict) -> bool:
        # any distribution URL that resolves on local FS OR looks like a remote URL/ref
        for dist in ds.get("distribution") or []:
            if not isinstance(dist, dict):
                continue
            p = dist.get("access_url") or dist.get("download_url")
            if not p:
                continue
            if _looks_remote(p):
                return True
            if os.path.exists(p) or os.path.exists(str(p).replace("/", os.sep)):
                return True

        # extension.repokit_info.destination
        x = get_repokit_info_payload(ds) or {}
        dest = x.get("destination")
        if _looks_remote(dest):
            return True
        return bool(dest and os.path.exists(dest))

    def _set_repokit_info_to_empty(ds: dict) -> None:
        """
        Ensure extension has exactly ONE repokit_info entry formed as {"repokit_info": {}}.
        - If extension is a dict: extension["repokit_info"] = {}
        - If extension is a list: remove any repokit_info variants and append {"repokit_info": {}}
          Variants removed: {"repokit_info": ...}, {"name":"repokit_info",...}, {"extension":"repokit_info",...}
        """
        exts = ds.get("extension")

        # Dict-shaped extension
        if isinstance(exts, dict):
            exts["repokit_info"] = {}
            return

        # List-shaped (or missing): normalize to list and enforce single {"repokit_info": {}}
        if not isinstance(exts, list):
            ds["extension"] = [{"repokit_info": {}}]
            return

        new_exts = []
        for ext in exts:
            if not isinstance(ext, dict):
                new_exts.append(ext)
                continue
            # Drop any prior repokit_info in any known shape
            if "repokit_info" in ext or ext.get("name") == "repokit_info" or ext.get("extension") == "repokit_info":
                continue
            new_exts.append(ext)

        new_exts.append({"repokit_info": {}})
        ds["extension"] = new_exts

    # ---- Main pass ----
    updated = 0
    for ds in datasets:
        if not _exists_on_disk(ds):
            _set_repokit_info_to_empty(ds)
            # Clear access_url in all distributions
            for dist in ds.get("distribution") or []:
                if isinstance(dist, dict):
                    dist["access_url"] = ""
            updated += 1

    if updated:
        dmp["modified"] = now_iso_minute()
        print(f"Updated {updated} dataset(s).")
        save_json(json_path, data)

    return json_path


def dataset(destination, json_path=DEFAULT_DMP_PATH, do_print: bool = True):
    def make_dataset_entry(name, distribution, repokit_info_payload):
        templates = dmp_default_templates()

        # Start from a deep copy so the global template isn't mutated
        entry = deepcopy(templates["dataset"])

        # Simple overlays
        entry["title"] = name
        entry["issued"] = now_iso_minute()
        entry["modified"] = now_iso_minute()

        # distribution must be a list with one object; merge with its template
        dist = deepcopy(templates["distribution"])
        if isinstance(distribution, dict):
            dist.update(distribution)  # shallow overlay
        entry["distribution"] = [dist]

        # repokit_info lives under dataset.extension as a single item: {"repokit_info": {...}}
        xdcas = deepcopy(templates["repokit_info"])
        if isinstance(repokit_info_payload, dict):
            xdcas.update(repokit_info_payload)
        entry["extension"] = [{"repokit_info": xdcas}]

        return entry

    def make_repokit_info_payload(
        *,
        data_type: str | None = None,
        destination: str | None = None,
        number_of_files: int | None = None,
        total_size_mb: float | None = None,
        file_formats: Iterable[str] | None = None,
        data_files: Iterable[str] | None = None,
        data_size_mb: Iterable[float] | None = None,
        hash_value: str | None = None,  # 'hash' is a builtin, so use hash_value
    ) -> dict[str, Any]:
        """
        Build repokit_info by loading templates['repokit_info'] and only updating fields
        that already exist in the template.
        """
        templates = dmp_default_templates()
        x = deepcopy(templates["repokit_info"])

        # Prepare candidate updates (normalize types lightly)
        updates: dict[str, Any] = {
            "data_type": data_type,
            "destination": norm_rel_urlish(destination) if destination is not None else None,
            "number_of_files": int(number_of_files) if number_of_files is not None else None,
            "total_size_mb": int(round(total_size_mb)) if total_size_mb is not None else None,
            "file_formats": list(file_formats) if file_formats is not None else None,
            "data_files": list(data_files) if data_files is not None else None,
            "data_size_mb": list(data_size_mb) if data_size_mb is not None else None,
            "hash": hash_value,
        }

        # Only update keys that already exist in the template AND have a non-None value
        for k, v in updates.items():
            if v is not None and k in x:
                x[k] = v

        return x

    cookie = (
        read_toml(
            folder=str(PROJECT_ROOT),
            json_filename="cookiecutter.json",
            tool_name="cookiecutter",
            toml_path="pyproject.toml",
        )
        or {}
    )

    destination = check_path_format(destination)

    if os.path.isfile(destination):
        data_files = [destination]
    else:
        os.makedirs(destination, exist_ok=True)
        data_files = sorted(get_all_files(destination, ignore=IGNORE_DICT))

    number_of_files, total_size_mb, file_formats, individual_sizes_mb = get_file_info(data_files)

    name = os.path.basename(destination)
    data_type = data_type_from_path(destination)

    is_restricted_path = _is_restricted_dataset_path(destination)
    data_access = "closed" if is_restricted_path else ("open" if data_files else "closed")
    license_ref = "" if is_restricted_path else LICENSE_LINKS.get(cookie.get("DATA_LICENSE"), "")
    repokit_data_files = (
        _pseudonymize_data_files(data_files) if is_restricted_path else list(data_files)
    )

    # distribution (complete RDA-DMP shape with defaults; 1.2-compliant)
    distribution = {
        # "title": name,
        "access_url": norm_rel_urlish(destination),
        # "download_url": "",
        "format": [ext.strip(".") for ext in sorted(file_formats)],
        "byte_size": to_bytes_mb(total_size_mb),
        "data_access": data_access,
        # "host": {"title": "Project repository", "url": ""},
        "available_until": "",
        # "description": "",
        "license": [
            {
                "license_ref": license_ref,
                "start_date": datetime.now().strftime("%Y-%m-%d"),
            }
        ],
    }

    # DCAS payload wrapped under dataset.extension
    repokit_info_payload = make_repokit_info_payload(
        data_type=data_type,
        destination=destination,
        number_of_files=number_of_files,
        total_size_mb=int(round(total_size_mb)),
        file_formats=sorted(list(file_formats)),
        data_files=repokit_data_files,
        data_size_mb=individual_sizes_mb,
        hash_value=get_hash(destination),
    )

    entry = make_dataset_entry(name, distribution, repokit_info_payload)
    if is_restricted_path:
        entry["sensitive_data"] = "yes"

    update_fields = list(DEFAULT_UPDATE_FIELDS)
    dist_update_fields = list(DEFAULT_UPDATE_DIST_FIELDS)
    if is_restricted_path:
        update_fields.append("sensitive_data")
        dist_update_fields.extend(["data_access", "license"])

    change_flag, json_path = datasets_to_json(
        json_path=json_path,
        entry=entry,
        update_fields=update_fields,
        update_distribution_fields=dist_update_fields,
        do_print=do_print,
    )

    return change_flag, json_path
