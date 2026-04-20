from .normalization_and_templates import (
    Any,
    DEFAULT_DMP_PATH,
    Path,
    _apply_cookiecutter_meta,
    _deref,
    _resolve_first,
    apply_defaults_in_place,
    data_type_from_path,
    deepcopy,
    dmp_default_templates,
    fetch_schema,
    get_repokit_info_payload,
    load_json,
    now_iso_minute,
    set_repokit_info_payload,
)
from .schema_io import (
    DATASET_ID_KEY_ORDER,
    DATASET_KEY_ORDER,
    DISTRIBUTION_KEY_ORDER,
    DMP_KEY_ORDER,
    HOST_KEY_ORDER,
    LICENSE_ITEM_KEY_ORDER,
    METADATA_ITEM_KEY_ORDER,
    PROJECT_ROOT,
    SCHEMA_URLS,
    SCHEMA_VERSION,
    SEC_PRIV_ITEM_KEY_ORDER,
    TECH_RES_ITEM_KEY_ORDER,
    save_json,
)
from .. import bootstrap_runtime_root
def _enum_default(prop_name: str | None, options: list[Any]) -> Any:
    """
    Choose a default for enum fields using project rules:
    - language -> 'eng'
    - {'yes','no','unknown'} -> 'unknown'
    - contact.contact_id.type -> 'orcid'
    - dmp_id.type -> 'doi'
    - otherwise: first option
    """
    str_opts = [o for o in options if isinstance(o, str)]
    lower = {o.lower() for o in str_opts}

    # language code
    if prop_name and prop_name.lower().endswith("language") and "eng" in lower:
        return "eng"

    # yes/no/unknown triad
    if {"yes", "no", "unknown"}.issubset(lower):
        return "unknown"

    # specific typed IDs via full path hints
    if prop_name:
        p = prop_name.lower()
        if p.endswith("contact_id.type") and "orcid" in lower:
            return "orcid"
        if p.endswith("dmp_id.type") and "doi" in lower:
            return "doi"

    # generic 'type' fallbacks (if path isn't specific)
    if "orcid" in lower:
        return "orcid"
    if "doi" in lower:
        return "doi"

    # final fallback
    return options[0] if options else None


def _default_for_schema(
    schema: dict[str, Any], node: dict[str, Any], prop_name: str | None = None
) -> Any:
    """
    Best-effort default constructor from a JSON Schema node (schema-driven).
    - objects  -> {}
    - arrays   -> []
    - string   -> ""
    - integer  -> 0
    - number   -> 0.0
    - boolean  -> False
    - enums    -> pick via _enum_default(...) using project rules
    - respects 'default' if present, resolves $ref one level
    """
    node = _deref(schema, node)

    # explicit schema default wins
    if "default" in node:
        return deepcopy(node["default"])

    # enum handling (multiple choice)
    if "enum" in node and isinstance(node["enum"], list):
        choice = _enum_default(prop_name, node["enum"])
        if choice is not None:
            return deepcopy(choice)

    # Resolve type (could be list like ["null","string"])
    typ = node.get("type")
    if isinstance(typ, list):
        for t in ("object", "array", "string", "integer", "number", "boolean"):
            if t in typ:
                typ = t
                break
        if isinstance(typ, list):
            typ = typ[0]  # fallback

    if typ == "object":
        return {}
    if typ == "array":
        return []
    if typ == "string":
        return ""
    if typ == "integer":
        return 0
    if typ == "number":
        return 0.0
    if typ == "boolean":
        return False

    # If only $ref given, resolve again
    if "$ref" in node:
        return _default_for_schema(schema, node, prop_name=prop_name)

    return None


def repair_empty_enums(
    obj: Any, schema: dict[str, Any], node: dict[str, Any], path: str | None = None
) -> None:
    """
    Traverse existing object and if a property is an enum but current value is "",
    replace it with the enum default according to project rules.
    """
    node = _deref(schema, node)
    if isinstance(obj, dict):
        props = node.get("properties", {})
        for k, v in obj.items():
            pnode = _deref(schema, props.get(k, {}))
            key_path = f"{path}.{k}" if path else k
            # repair empty string on enums
            if isinstance(v, str) and v == "" and "enum" in pnode:
                obj[k] = _enum_default(key_path, pnode["enum"]) or v
            # recurse
            repair_empty_enums(v, schema, pnode, path=key_path)
    elif isinstance(obj, list):
        items = _deref(schema, node.get("items", {}))
        for i, it in enumerate(obj):
            repair_empty_enums(it, schema, items, path=f"{path}[{i}]" if path else f"[{i}]")


# ──────────────────────────────────────────────────────────────────────────────
# Schema-driven required-field filling
# ──────────────────────────────────────────────────────────────────────────────


def _ensure_required_object_from_schema(
    obj: dict[str, Any],
    schema: dict[str, Any],
    obj_schema: dict[str, Any],
    path: str | None = None,
) -> None:
    """
    Ensure that every key listed in obj_schema['required'] exists on obj,
    initializing with schema-driven defaults (with enum rules). Recurse into
    nested structures. Also traverse existing non-required object/array fields
    to satisfy their nested requireds.
    """
    s = _deref(schema, obj_schema)
    props: dict[str, Any] = s.get("properties", {})
    required = s.get("required", [])

    # Fill required keys (and recurse)
    for key in required:
        prop_schema = props.get(key, {})
        key_path = f"{path}.{key}" if path else key
        if key not in obj or obj[key] is None:
            obj[key] = _default_for_schema(schema, prop_schema, prop_name=key_path)

        if isinstance(obj[key], dict):
            _ensure_required_object_from_schema(obj[key], schema, prop_schema, path=key_path)

        elif isinstance(obj[key], list):
            prop_s = _deref(schema, prop_schema)
            items_schema = _deref(schema, prop_s.get("items", {}))
            min_items = prop_s.get("minItems", 0)

            # If minItems > 0 and list empty, seed one default item
            if min_items and not obj[key]:
                default_item = _default_for_schema(schema, items_schema, prop_name=f"{key_path}[]")
                obj[key].append(default_item if default_item is not None else {})

            # For any existing items that are objects, ensure their requireds
            for i, it in enumerate(obj[key]):
                if isinstance(it, dict):
                    _ensure_required_object_from_schema(
                        it, schema, items_schema, path=f"{key_path}[{i}]"
                    )

    # Traverse existing non-required object/array properties to satisfy nested requireds
    for key, val in list(obj.items()):
        if key not in props:
            continue
        prop_schema = props[key]
        key_path = f"{path}.{key}" if path else key
        prop_s = _deref(schema, prop_schema)

        if isinstance(val, dict):
            _ensure_required_object_from_schema(val, schema, prop_s, path=key_path)
        elif isinstance(val, list):
            items_schema = _deref(schema, prop_s.get("items", {}))
            for i, it in enumerate(val):
                if isinstance(it, dict):
                    _ensure_required_object_from_schema(
                        it, schema, items_schema, path=f"{key_path}[{i}]"
                    )


def ensure_required_by_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """
    Ensure required fields exist for the root 'dmp' object (and nested objects)
    by reading ONLY from the JSON Schema. No field names are hardcoded.
    """
    # Locate the top-level 'dmp' property schema
    root_props = schema.get("properties", {})
    dmp_schema_node = root_props.get("dmp")
    if not isinstance(dmp_schema_node, dict):
        return  # nothing to do if schema shape is unexpected

    dmp_obj = data.setdefault("dmp", {})
    _ensure_required_object_from_schema(dmp_obj, schema, dmp_schema_node, path="dmp")


# ──────────────────────────────────────────────────────────────────────────────
# Shaping & normalization (using centralized defaults + migrations)
# ──────────────────────────────────────────────────────────────────────────────


def ensure_dmp_shape(data: dict[str, Any], **_: Any) -> dict[str, Any]:
    """
    Ensure a well-formed RDA-DMP container.
    Uses centralized defaults and preserves existing values where present.
    Also migrates legacy:
      - dataset["repokit_info"]  -> dataset["extension"] [{"repokit_info": {...}}]
    """
    templates = dmp_default_templates()

    # Already DMP-shaped?
    if isinstance(data.get("dmp"), dict):
        dmp = data["dmp"]
        apply_defaults_in_place(dmp, templates["root"])
        dmp["schema"] = SCHEMA_URLS[SCHEMA_VERSION]  # enforce exact link

        # project must be an array
        if not isinstance(dmp.get("project"), list):
            dmp["project"] = []

        # migrate legacy top-level dataset.repokit_info
        for ds in dmp.get("dataset", []):
            if isinstance(ds.get("repokit_info"), dict):
                set_repokit_info_payload(ds, ds.pop("repokit_info"))

        return {"dmp": dmp}

    # Fresh container
    root = deepcopy(templates["root"])
    return {"dmp": root}


def normalize_root_in_place(data: dict[str, Any], schema: dict[str, Any] | None) -> None:
    """
    Normalize root-level structures and keep extensions under dmp.extension.
    Also ensure/shape the dmp.project array.
    """
    templates = dmp_default_templates()
    dmp = data.setdefault("dmp", {})
    apply_defaults_in_place(dmp, templates["root"])
    dmp["schema"] = SCHEMA_URLS[SCHEMA_VERSION]  # enforce

    # Project array (shape or create a stub)
    projects: list[dict[str, Any]] = dmp.setdefault("project", [])
    if not isinstance(projects, list):
        projects = []
        dmp["project"] = projects
    if not projects:
        # seed a single project using defaults; title mirrors dmp.title if present
        prj = deepcopy(templates["project"])
        prj["title"] = dmp.get("title") or "Project"
        projects.append(prj)

    # Optionally complement from schema if available
    if schema:
        proj_schema = (
            _resolve_first(schema, ["#/definitions/Project", "#/definitions/project"]) or None
        )
        if proj_schema:
            for prj in projects:
                _ensure_object_fields_from_schema(prj, schema, proj_schema, path="dmp.project[]")
                apply_defaults_in_place(prj, templates["project"])
        else:
            for prj in projects:
                apply_defaults_in_place(prj, templates["project"])
    else:
        for prj in projects:
            apply_defaults_in_place(prj, templates["project"])


def normalize_datasets_in_place(data: dict[str, Any], schema: dict[str, Any] | None) -> None:
    """
    Ensure presence of expected RDA-DMP fields on each dataset & distribution.
    Also ensures dataset-level custom fields are under dataset.extension.repokit_info.
    """
    templates = dmp_default_templates()
    dmp = data.setdefault("dmp", {})
    datasets: list[dict[str, Any]] = dmp.setdefault("dataset", [])
    if not isinstance(datasets, list):
        dmp["dataset"] = datasets = []

    ds_schema = dist_schema = None
    if schema:
        ds_schema = (
            _resolve_first(schema, ["#/definitions/Dataset", "#/definitions/dataset"]) or None
        )
        dist_schema = (
            _resolve_first(schema, ["#/definitions/Distribution", "#/definitions/distribution"])
            or None
        )

    for ds in datasets:
        # legacy migration
        if isinstance(ds.get("repokit_info"), dict):
            set_repokit_info_payload(ds, ds.pop("repokit_info"))

        # dataset defaults (central)
        apply_defaults_in_place(ds, templates["dataset"])

        # schema-top up (optional)
        if ds_schema:
            _ensure_object_fields_from_schema(ds, schema, ds_schema, path="dmp.dataset[]")

        # distribution array + defaults for each distribution
        ds.setdefault("distribution", [])
        if not ds["distribution"]:
            # create one distribution seeded with dataset title
            dist = deepcopy(templates["distribution"])
            # dist["title"] = ds.get("title") or dist["title"]
            ds["distribution"].append(dist)

        for dist in ds["distribution"]:
            apply_defaults_in_place(dist, templates["distribution"])
            # schema-top up (optional)
            if dist_schema:
                _ensure_object_fields_from_schema(
                    dist, schema, dist_schema, path="dmp.dataset[].distribution[]"
                )

        # repokit_info payload
        x = get_repokit_info_payload(ds) or {}
        apply_defaults_in_place(x, templates["repokit_info"])
        if not x.get("data_type"):
            hint = (ds.get("distribution") or [{}])[0].get("access_url") or ""
            x["data_type"] = data_type_from_path(hint)
        set_repokit_info_payload(ds, x)


def _ensure_object_fields_from_schema(
    target: dict[str, Any],
    schema: dict[str, Any],
    obj_schema: dict[str, Any],
    prefill: dict[str, Any] | None = None,
    path: str | None = None,
) -> None:
    """
    Ensure keys exist on target based on 'properties' of obj_schema.
    Values are best-effort defaults; existing values are preserved.
    """
    obj_schema = _deref(schema, obj_schema)
    props = obj_schema.get("properties", {})
    for key, prop_schema in props.items():
        if key not in target:
            key_path = f"{path}.{key}" if path else key
            target[key] = _default_for_schema(schema, prop_schema, prop_name=key_path)
    for key in obj_schema.get("required", []):
        if key not in target:
            key_path = f"{path}.{key}" if path else key
            target[key] = _default_for_schema(schema, props.get(key, {}), prop_name=key_path)
    if prefill:
        for k, v in prefill.items():
            target.setdefault(k, v)


def _order_dict(d: dict[str, Any], order: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in order:
        if k in d:
            out[k] = d[k]
    for k, v in d.items():
        if k not in out:
            out[k] = v
    return out


def reorder_dmp_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict where:
    - data['dmp'] keys follow DMP_KEY_ORDER (as before)
    - each dataset follows DATASET_KEY_ORDER
    - each dataset.distribution[] follows DISTRIBUTION_KEY_ORDER
    - common nested objects are ordered (dataset_id, metadata items, etc.)
    """
    dmp = data.get("dmp", {})
    # Root ordering first
    ordered_root: dict[str, Any] = _order_dict(dmp, DMP_KEY_ORDER)

    # Reorder datasets (and their children)
    ds_list = ordered_root.get("dataset", [])
    new_ds_list: list[dict[str, Any]] = []
    if isinstance(ds_list, list):
        for ds in ds_list:
            if not isinstance(ds, dict):
                new_ds_list.append(ds)
                continue

            ds2 = _order_dict(ds, DATASET_KEY_ORDER)

            # dataset_id
            if isinstance(ds2.get("dataset_id"), dict):
                ds2["dataset_id"] = _order_dict(ds2["dataset_id"], DATASET_ID_KEY_ORDER)

            # metadata[]
            if isinstance(ds2.get("metadata"), list):
                ds2["metadata"] = [
                    _order_dict(m, METADATA_ITEM_KEY_ORDER) if isinstance(m, dict) else m
                    for m in ds2["metadata"]
                ]

            # security_and_privacy[]
            if isinstance(ds2.get("security_and_privacy"), list):
                ds2["security_and_privacy"] = [
                    _order_dict(x, SEC_PRIV_ITEM_KEY_ORDER) if isinstance(x, dict) else x
                    for x in ds2["security_and_privacy"]
                ]

            # technical_resource[]
            if isinstance(ds2.get("technical_resource"), list):
                ds2["technical_resource"] = [
                    _order_dict(x, TECH_RES_ITEM_KEY_ORDER) if isinstance(x, dict) else x
                    for x in ds2["technical_resource"]
                ]

            # distribution[]
            if isinstance(ds2.get("distribution"), list):
                new_dists: list[dict[str, Any]] = []
                for dist in ds2["distribution"]:
                    if not isinstance(dist, dict):
                        new_dists.append(dist)
                        continue
                    dist2 = _order_dict(dist, DISTRIBUTION_KEY_ORDER)

                    # host
                    if isinstance(dist2.get("host"), dict):
                        dist2["host"] = _order_dict(dist2["host"], HOST_KEY_ORDER)

                    # license[]
                    if isinstance(dist2.get("license"), list):
                        dist2["license"] = [
                            _order_dict(lic, LICENSE_ITEM_KEY_ORDER)
                            if isinstance(lic, dict)
                            else lic
                            for lic in dist2["license"]
                        ]

                    new_dists.append(dist2)
                ds2["distribution"] = new_dists

            new_ds_list.append(ds2)

    ordered_root["dataset"] = new_ds_list

    # Return new container with ordered root
    return {"dmp": ordered_root}


def create_or_update_dmp_from_schema(
    dmp_path: Path = DEFAULT_DMP_PATH,
    project_root: Path | None = None,
) -> Path:
    """
    - If DMP doesn't exist: create a fresh DMP scaffold.
    - If it exists: load it, normalize root & project & datasets (preserving values).
    - Always set dmp["schema"] to the GitHub 'tree' URL for 1.X.
    - Wrap root custom fieldsand dataset custom fields (repokit_info)
      under their respective 'extension' arrays.
    - Pull metadata from cookiecutter (title, description, contact, project).
    - Ensure the top-level 'dmp' object is saved in the exact key order you specified.
    """
    schema = fetch_schema()
    project_root = project_root or PROJECT_ROOT

    if not dmp_path.exists():
        # Fresh shape
        shaped = ensure_dmp_shape({})
        _apply_cookiecutter_meta(project_root=project_root, data=shaped, overwrite=True)
        normalize_root_in_place(shaped, schema=schema)
        normalize_datasets_in_place(shaped, schema=schema)

        # Fill required fields based purely on the schema (no hardcoding)
        ensure_required_by_schema(shaped, schema)
        # Also repair any existing empty enums ("") to rule-compliant defaults
        repair_empty_enums(
            shaped.get("dmp", {}), schema, schema.get("properties", {}).get("dmp", {}), path="dmp"
        )

        shaped = reorder_dmp_keys(shaped)

        save_json(dmp_path, shaped)
        return dmp_path

    # Update/normalize existing
    data = load_json(dmp_path)
    data = ensure_dmp_shape(data)
    normalize_root_in_place(data, schema=schema)
    normalize_datasets_in_place(data, schema=schema)
    _apply_cookiecutter_meta(project_root=project_root, data=data, overwrite=False)

    data["dmp"]["schema"] = SCHEMA_URLS[SCHEMA_VERSION]  # enforce requested value
    data["dmp"]["modified"] = now_iso_minute()  # ensure date-time with Z

    # Schema-driven required-field filling + enum repair
    ensure_required_by_schema(data, schema)
    repair_empty_enums(
        data.get("dmp", {}), schema, schema.get("properties", {}).get("dmp", {}), path="dmp"
    )

    data = reorder_dmp_keys(data)

    save_json(dmp_path, data)

    return dmp_path


def main() -> None:
    global PROJECT_ROOT
    PROJECT_ROOT = bootstrap_runtime_root()
    create_or_update_dmp_from_schema(dmp_path=DEFAULT_DMP_PATH, project_root=PROJECT_ROOT)


if __name__ == "__main__":
    main()
