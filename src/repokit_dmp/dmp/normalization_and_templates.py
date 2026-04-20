from .schema_io import (
    Any,
    DEFAULT_DMP_PATH,
    JSON_FILENAME,
    LEGACY_REPOKIT_INFO_KEYS,
    LICENSE_LINKS,
    PROJECT_ROOT,
    Path,
    REPOKIT_INFO_KEY,
    SCHEMA_CACHE_FILES,
    SCHEMA_DOWNLOAD_URLS,
    SCHEMA_VERSION,
    TOML_PATH,
    TOOL_NAME,
    _set_contacts,
    datetime,
    dmp_default_templates,
    json,
    load_json,
    read_toml,
)
import urllib
from copy import deepcopy
from repokit_common import toml_dataset_path, write_toml
def _apply_cookiecutter_meta(
    project_root: Path, data: dict[str, Any], overwrite: bool = False
) -> None:
    """
    Read cookiecutter and fill DMP meta iff missing:
      - dmp.title (PROJECT_NAME/REPO_NAME)
      - dmp.description (PROJECT_DESCRIPTION)
      - dmp.contact (first author/email[/orcid])
      - dmp.project[0].title/description
    """
    cookie = (
        read_toml(
            folder=str(project_root),
            json_filename=JSON_FILENAME,
            tool_name=TOOL_NAME,
            toml_path=TOML_PATH,
        )
        or {}
    )

    dmp = data.setdefault("dmp", {})
    templates = dmp_default_templates()

    # ensure base defaults are present before applying cookiecutter hints
    apply_defaults_in_place(dmp, templates["root"])

    # title / description (do not overwrite if already populated)
    proj_title = cookie.get("PROJECT_NAME") or cookie.get("REPO_NAME")
    if proj_title:
        if overwrite:
            dmp["title"] = proj_title
        else:
            dmp["title"] = dmp.get("title") or proj_title

    proj_desc = cookie.get("PROJECT_DESCRIPTION")
    if proj_desc:
        if overwrite:
            dmp["description"] = proj_desc
        else:
            dmp["description"] = dmp.get("description") or proj_desc

    # contact & contributor
    dmp = _set_contacts(dmp, cookie, overwrite)

    # project[0] (minimal)
    projects: list[dict[str, Any]] = dmp.setdefault("project", [])
    if not projects:
        projects.append(deepcopy(templates["project"]))
    prj0 = projects[0]

    if proj_title and not prj0.get("title"):
        prj0["title"] = proj_title
    if proj_desc and not prj0.get("description"):
        prj0["description"] = proj_desc
    apply_defaults_in_place(prj0, templates["project"])


def _cookie_meta_from_dmp(data: dict[str, Any]) -> dict[str, str]:
    """
    Extract cookiecutter-style fields from a DMP structure.

    Inverse of `_apply_cookiecutter_meta` / `_set_contacts`:

      dmp.title            -> PROJECT_NAME
      dmp.description      -> PROJECT_DESCRIPTION
      dmp.contact + contributor[*]
        -> AUTHORS, EMAIL, ORCIDS (semicolon-separated)
      dataset[].distribution[].license[].license_ref
        -> DATA_LICENSE (via LICENSE_LINKS inverse mapping, first match wins)

    Returns only keys that can be inferred from the DMP.
    """
    dmp = (data or {}).get("dmp") or {}
    out: dict[str, str] = {}

    # --- Project title / description ---
    title = (dmp.get("title") or "").strip()
    desc = (dmp.get("description") or "").strip()
    if title:
        out["PROJECT_NAME"] = title
        # REPO_NAME is often same as project name if not explicitly set
        out.setdefault("REPO_NAME", title)
    if desc:
        out["PROJECT_DESCRIPTION"] = desc

    # --- Authors / emails / orcids (contact + contributors) ---
    authors: list[str] = []
    emails: list[str] = []
    orcids: list[str] = []

    def _add_person(name: Any, mbox: Any, id_obj: Any, id_key: str) -> None:
        if isinstance(name, str) and name.strip():
            authors.append(name.strip())
        if isinstance(mbox, str) and mbox.strip():
            emails.append(mbox.strip())
        if isinstance(id_obj, dict):
            ident = id_obj.get("identifier")
            id_type = (id_obj.get("type") or "").lower()
            if isinstance(ident, str) and ident.strip():
                if not id_type or id_type == id_key:
                    orcids.append(ident.strip())

    contact = dmp.get("contact") or {}
    _add_person(
        contact.get("name"),
        contact.get("mbox"),
        contact.get("contact_id"),
        "orcid",
    )

    for c in dmp.get("contributor") or []:
        if not isinstance(c, dict):
            continue
        _add_person(
            c.get("name"),
            c.get("mbox"),
            c.get("contributor_id"),
            "orcid",
        )

    # Cookie expects multi-values in a single string; split_multi() on read
    # will likely handle ';', ',', and newlines, so we join with '; '.
    if authors:
        out["AUTHORS"] = "; ".join(authors)
    if emails:
        out["EMAIL"] = "; ".join(emails)
    if orcids:
        out["ORCIDS"] = "; ".join(orcids)

    # --- DATA_LICENSE (inverse of LICENSE_LINKS) ---
    # Look at the first non-empty license_ref in any dataset/distribution
    license_ref: str | None = None
    for ds in dmp.get("dataset") or []:
        if not isinstance(ds, dict):
            continue
        for dist in ds.get("distribution") or []:
            if not isinstance(dist, dict):
                continue
            for lic in dist.get("license") or []:
                if not isinstance(lic, dict):
                    continue
                ref = (lic.get("license_ref") or "").strip()
                if ref:
                    license_ref = ref
                    break
            if license_ref:
                break
        if license_ref:
            break

    if license_ref:
        # Invert LICENSE_LINKS mapping (short -> URL) to recover a short code
        inverse = {v: k for k, v in LICENSE_LINKS.items()}
        code = inverse.get(license_ref)
        if code:
            out["DATA_LICENSE"] = code

    return out


def update_cookiecutter_from_dmp(
    dmp_path: Path = DEFAULT_DMP_PATH,
    overwrite: bool = True,
) -> Path | None:
    """
    Update cookiecutter fields using values from the DMP.

    - Loads DMP from `dmp_path`.
    - Extracts cookie-like fields with `_cookie_meta_from_dmp`.
    - Updates (and optionally overwrites) keys in cookiecutter.json.

    If `cookie_path` is None, uses PROJECT_ROOT / "cookiecutter.json".

    Returns the path to the updated cookiecutter file, or None on failure.
    """

    dmp_data = load_json(dmp_path)
    if not dmp_data:
        print(f"No DMP found at {dmp_path}; nothing to update.")
        return None

    new_fields = _cookie_meta_from_dmp(dmp_data)
    if not new_fields:
        print("No cookiecutter fields could be inferred from DMP; nothing to update.")
        return None

    cookie = read_toml(
        folder=str(PROJECT_ROOT),
        json_filename=JSON_FILENAME,
        tool_name=TOOL_NAME,
        toml_path=TOML_PATH,
    )

    # Apply updates
    for key, value in new_fields.items():
        if overwrite or not cookie.get(key):
            cookie[key] = value

    # Write back
    write_toml(
        data=cookie,
        folder=str(PROJECT_ROOT),
        json_filename=JSON_FILENAME,
        tool_name=TOOL_NAME,
        toml_path=TOML_PATH,
    )


def now_iso_minute() -> str:
    """RFC 3339 / JSON Schema 'date-time' with UTC 'Z' and seconds precision."""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_schema(
    schema_url: str = SCHEMA_DOWNLOAD_URLS[SCHEMA_VERSION],
    cache_path: Path = SCHEMA_CACHE_FILES[SCHEMA_VERSION],
    force: bool = False,
) -> dict[str, Any]:
    """Download the schema (or use cached copy)."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and not force:
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    with urllib.request.urlopen(schema_url) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data


# --- shared, soft-dependency validator ---------------------------------------
def validate_against_schema(
    data: dict[str, Any], schema: dict[str, Any] | None = None
) -> list[str]:
    """
    Return a list of error messages. If jsonschema is unavailable, returns [].
    """
    try:
        from jsonschema import Draft7Validator
    except Exception:
        return []
    schema = schema or fetch_schema()
    v = Draft7Validator(schema)
    errs = sorted(v.iter_errors(data), key=lambda e: list(e.path))

    errs = [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errs]
    if errs:
        print("⚠️ Schema validation issues (new file, after schema-driven auto-fix):")
        for e in errs[:50]:
            print(" -", e)


def _resolve_ref(schema: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve an internal JSON Pointer like '#/definitions/Dataset'."""
    if not ref.startswith("#/"):
        return {}
    node: Any = schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _deref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Return node with $ref resolved (one level); shallow merge with local overrides."""
    if "$ref" in node:
        base = _resolve_ref(schema, node["$ref"])
        merged = dict(base)
        merged.update({k: v for k, v in node.items() if k != "$ref"})
        return merged
    return node


def _resolve_first(schema: dict[str, Any], candidates: list[str]) -> dict[str, Any]:
    """Try multiple $ref candidates and return the first that resolves."""
    for c in candidates:
        node = _resolve_ref(schema, c)
        if node:
            return node
    return {}


def to_bytes_mb(mb) -> int | None:
    try:
        return int(round(float(mb) * 1024 * 1024))
    except Exception:
        return None


def norm_rel_urlish(p: str | None) -> str | None:
    """
    Normalise a path-or-URL-ish string:

    - Returns None for empty / non-string input.
    - Converts backslashes to forward slashes.
    - If it's an absolute path under PROJECT_ROOT, make it relative to PROJECT_ROOT.
    - Strips leading './'.
    """
    if not p or not isinstance(p, str):
        return None

    p2 = p.strip().replace("\\", "/")
    if not p2:
        return None

    # Try to treat as filesystem path and relativise under PROJECT_ROOT if possible
    try:
        path_obj = Path(p2)
        if path_obj.is_absolute():
            try:
                rel = path_obj.relative_to(PROJECT_ROOT)
                p2 = rel.as_posix()
            except ValueError:
                # Not under PROJECT_ROOT → leave as-is
                p2 = path_obj.as_posix()
        else:
            # Already relative; normalise but don't resolve
            p2 = path_obj.as_posix()
    except Exception:
        # If Path() chokes on something truly URL-ish, just keep the cleaned string
        pass

    # Strip leading "./" segments
    while p2.startswith("./"):
        p2 = p2[2:]

    return p2 or None


def data_type_from_path(p: str) -> str:
    """
    Infer a data type from a path using cfg.

    - If cfg["sub_dir"] is True:
        type = first component under parent_path
        e.g. ./data/raw/file.csv  -> "raw"

    - If cfg["sub_dir"] is False:
        always return "Uncategorised".
    """
    cfg, _ = toml_dataset_path()

    parent = Path(cfg["parent_path"])
    use_subdirs = bool(cfg.get("sub_dir", False))

    # If we're not using subdirs for typing, always uncategorised
    if not use_subdirs:
        return "Uncategorised"

    # Normalise and make path relative to parent; fall back if impossible
    norm = Path(p.replace("\\", "/"))
    try:
        rel = norm.relative_to(parent)
    except ValueError:
        # Try with resolved paths (handles mixed absolute/relative)
        try:
            rel = norm.resolve().relative_to(parent.resolve())
        except Exception:
            return "Uncategorised"

    parts = rel.parts

    # Expect parent/<type>/...
    if len(parts) >= 2:
        # parts[0] = subdir under parent
        return parts[0]

    return "Uncategorised"


def _ensure_extension(obj: dict[str, Any]) -> list[dict[str, Any]]:
    obj.setdefault("extension", [])
    return obj["extension"]


def _find_extension_index(obj: dict[str, Any], key: str) -> int:
    ext = obj.get("extension") or []
    for i, item in enumerate(ext):
        if isinstance(item, dict) and key in item:
            return i
    return -1


def get_extension_payload(obj: dict[str, Any], key: str) -> dict[str, Any] | None:
    i = _find_extension_index(obj, key)
    if i == -1:
        return None
    payload = obj["extension"][i].get(key)
    return payload if isinstance(payload, dict) else None


def set_extension_payload(obj: dict[str, Any], key: str, payload: dict[str, Any]) -> None:
    ext = _ensure_extension(obj)
    i = _find_extension_index(obj, key)
    if i == -1:
        ext.append({key: dict(payload)})
    else:
        if not isinstance(ext[i][key], dict):
            ext[i][key] = dict(payload)
        else:
            ext[i][key].update({k: v for k, v in payload.items()})


# ──────────────────────────────────────────────────────────────────────────────
# CENTRALIZED DEFAULTS
# ──────────────────────────────────────────────────────────────────────────────


def _drop_extension_keys(obj: dict[str, Any], keys: tuple[str, ...]) -> None:
    exts = obj.get("extension")
    if isinstance(exts, dict):
        for k in keys:
            exts.pop(k, None)
        return
    if not isinstance(exts, list):
        return

    obj["extension"] = [
        ext
        for ext in exts
        if not (
            isinstance(ext, dict)
            and any(k in ext or ext.get("name") == k or ext.get("extension") == k for k in keys)
        )
    ]


def get_repokit_info_payload(obj: dict[str, Any]) -> dict[str, Any] | None:
    payload = get_extension_payload(obj, REPOKIT_INFO_KEY)
    if payload is not None:
        return payload

    for key in LEGACY_REPOKIT_INFO_KEYS:
        payload = get_extension_payload(obj, key)
        if payload is not None:
            return payload

    return None


def set_repokit_info_payload(obj: dict[str, Any], payload: dict[str, Any]) -> None:
    _drop_extension_keys(obj, (REPOKIT_INFO_KEY, *LEGACY_REPOKIT_INFO_KEYS))
    set_extension_payload(obj, REPOKIT_INFO_KEY, payload)


def today_iso() -> str:
    """JSON Schema 'date' string."""
    return datetime.utcnow().strftime("%Y-%m-%d")


def _deep_apply_defaults(target: Any, template: Any) -> Any:
    """
    Deep, non-destructive default overlay.

    Rules (conservative, editor-safe):
    - dict vs dict: add ONLY missing keys (recursively for dicts); do not overwrite existing values.
    - list: if target is a list, ALWAYS keep it as-is (even if empty). Never inject template list items.
    - type mismatch: if target exists and is not the same container type as template, keep target as-is.
    - primitives: if target is None, use template; otherwise keep target.
    """
    # dicts
    if isinstance(template, dict):
        if not isinstance(target, dict):
            # target already has a non-dict value; preserve it
            return target
        # add only missing keys from template
        out = dict(target)
        for k, v in template.items():
            if k in out:
                # recurse only if both sides are dicts; otherwise preserve user's value
                if isinstance(out[k], dict) and isinstance(v, dict):
                    out[k] = _deep_apply_defaults(out[k], v)
                else:
                    # keep user's existing non-dict (or list) value
                    pass
            else:
                out[k] = deepcopy(v)
        return out

    # lists
    if isinstance(template, list):
        # If the user already has a list (even empty), keep it verbatim.
        if isinstance(target, list):
            return target
        # If user has some other value, keep it; otherwise fall back to template for "missing"
        return target if target is not None else deepcopy(template)

    # primitives
    return target if target is not None else deepcopy(template)


def apply_defaults_in_place(target: dict, template: dict) -> None:
    """
    In-place wrapper using conservative overlay: only add missing keys;
    never replace existing lists or primitives.
    """
    merged = _deep_apply_defaults(target, template)
    target.clear()
    target.update(merged)
