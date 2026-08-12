#!/usr/bin/env python3

"""
Publish a dataset from an maDMP to DeiC Dataverse (demo) with:
- Hardcoded target installation + collection (constants below)
- Packaging to respect Dataverse limits (<= 100 files total, <= 953.7 MB per file)
- Structure-preserving zips (first-level folders) with sharding/merging as needed
- Double-zip workaround so Dataverse does not auto-unpack our archives
- Rich maDMP -> Dataverse citation metadata mapping
- Robust retries for API calls and uploads, including PARALLEL file uploads

Usage (CLI):
  python dataverse.py --dmp path/to/dmp.json --token <API_TOKEN> --publish
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from random import random
from typing import Any
from threading import Thread
import requests
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed


from .publish import (
    DATAVERS_SUBJECTS,
    DATAVERSE_MAX_FILE_SIZE_BYTES,
    DATAVERSE_MAX_FILES_TOTAL,
    DEFAULT_TIMEOUT,
    # ── constants ─────────────────────────────────────────────
    RETRY_STATUS,
    PackItem,
    PublishError,
    # ── functions ────────────────────────────────────────────
    _get,
    _guess_dataset,
    _has_personal_or_sensitive,
    _norm_list,
    append_packaging_note,
    description_from_madmp,
    estimate_zip_total_bytes,
    files_from_repokit_info,
    realize_packaging_plan_parallel,
    sizes_bytes,
)

# ============================================================================
# Configuration
# ============================================================================

# Packaging heuristics
ZIP_COMPRESSION = getattr(zipfile, "ZIP_DEFLATED", 0)
# Aim a bit below the hard cap to buffer compression estimate error
TARGET_MAX_PER_ARCHIVE: int = int(DATAVERSE_MAX_FILE_SIZE_BYTES * 0.92)

# Important: Dataverse auto-unpacks single ZIP uploads.
# When we must package, we create *double-zipped* archives to prevent auto-unpack.
DOUBLE_ZIP_TO_PREVENT_UNPACK: bool = True

# Parallel upload tuning (threads)
DATAVERSE_UPLOAD_WORKERS: int = 5  # 3–6 is usually a sweet spot


# ============================================================================
# API helpers (with retries)
# ============================================================================


def _dv_headers(token: str) -> dict:
    return {"X-Dataverse-key": token}


def _retry_sleep(attempt: int) -> None:
    time.sleep(min(30, (attempt + 1) * 1.5) + random())


def _dv_post_json(
    url: str, token: str, payload: dict | None, timeout: int = DEFAULT_TIMEOUT
) -> requests.Response:
    for attempt in range(6):
        try:
            r = requests.post(url, headers=_dv_headers(token), json=payload, timeout=timeout)
        except requests.RequestException:
            _retry_sleep(attempt)
            continue
        if r.status_code in RETRY_STATUS:
            _retry_sleep(attempt)
            continue
        return r
    raise PublishError(f"Dataverse request failed after retries: POST {url}")


def _dv_create_dataset(
    base_url: str, token: str, dataverse_alias: str, dataset_version_json: dict
) -> dict:
    url = f"{base_url.rstrip('/')}/api/dataverses/{dataverse_alias}/datasets"
    r = _dv_post_json(url, token, dataset_version_json)
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise PublishError(f"Dataverse create dataset failed: {r.status_code} {detail}")
    return r.json()


def _dv_publish_dataset(
    base_url: str, token: str, dataset_id: int, release_type: str = "major"
) -> dict:
    assert release_type in {"major", "minor"}
    url = f"{base_url.rstrip('/')}/api/datasets/{dataset_id}/actions/:publish?type={release_type}"
    r = _dv_post_json(url, token, None)
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise PublishError(f"Dataverse publish failed: {r.status_code} {detail}")
    return r.json()


# ============================================================================
# Parallel upload helpers (Session + ThreadPool)
# ============================================================================


def _dv_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-Dataverse-key": token})
    return s


def _dv_upload_file_session(
    session: requests.Session,
    base_url: str,
    dataset_id: int,
    filepath: str,
    description: str | None,
    directory_label: str | None,
    restrict: bool,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Single file upload using an existing Session (keep-alive). Returns basename on success."""
    url = f"{base_url.rstrip('/')}/api/datasets/{dataset_id}/add"
    json_data: dict[str, Any] = {"description": description or ""}
    if directory_label:
        json_data["directoryLabel"] = directory_label
    if restrict:
        json_data["restrict"] = True

    data: dict[str, Any] = {"jsonData": json.dumps(json_data)}
    with open(filepath, "rb") as fh:
        files = {"file": (os.path.basename(filepath), fh)}
        r = session.post(url, files=files, data=data, timeout=timeout)

    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise PublishError(f"Dataverse upload failed ({filepath}): {r.status_code} {detail}")

    return os.path.basename(filepath)


def _dv_upload_many_parallel(
    base_url: str,
    token: str,
    dataset_id: int,
    paths: list[str],
    directory_label_for: dict[str, str | None],
    restrict: bool,
    workers: int = DATAVERSE_UPLOAD_WORKERS,
) -> tuple[list[str], list[str]]:
    """Upload many files in parallel (threaded). Returns (uploaded_basenames, error_messages)."""
    if not paths:
        return [], []
    uploaded: list[str] = []
    errors: list[str] = []
    with _dv_session(token) as sess, ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for fp in paths:
            lbl = directory_label_for.get(fp)
            fut = ex.submit(
                _dv_upload_file_session, sess, base_url, dataset_id, fp, None, lbl, restrict
            )
            futs[fut] = fp

        for fut in as_completed(futs):
            try:
                uploaded.append(fut.result())
            except Exception as e:
                errors.append(f"{os.path.basename(futs[fut])}: {e}")
    return uploaded, errors


# ============================================================================
# maDMP → Dataverse field mapping
# ============================================================================


def _normalize_orcid(raw: str | None) -> str | None:
    if not raw:
        return None
    s = (
        str(raw)
        .strip()
        .replace("https://orcid.org/", "")
        .replace("http://orcid.org/", "")
        .replace(" ", "")
    )
    s = s.replace("-", "")
    if len(s) != 16:
        return None
    core, last = s[:-1], s[-1:]
    if core.isdigit() and (last.isdigit() or last.upper() == "X"):
        return f"{s[0:4]}-{s[4:8]}-{s[8:12]}-{s[12:16]}"
    return None


def _affiliation_from_node(node: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(node, dict):
        return (None, None)
    name = node.get("name")
    ror = None
    aff_id = node.get("affiliation_id") or node.get("organization_id") or {}
    if isinstance(aff_id, dict) and (aff_id.get("type") or "").lower() == "ror":
        rid = aff_id.get("identifier")
        if isinstance(rid, str) and rid:
            ror = rid.strip()
    return (name, ror)


def _license_for_dataverse(ds: dict) -> dict | None:
    """
    Map maDMP license info to a Dataverse-accepted license object.
    Returns {"name": "...", "uri": "..."} for CC0 / CC BY family, else None.
    """

    def _best_license_url_or_text() -> str | None:
        for section_key in ("distribution", "rights"):
            for block in _norm_list(ds.get(section_key, [])):
                if not isinstance(block, dict):
                    continue
                for r in _norm_list(block.get("license") or block.get("rights") or []):
                    if isinstance(r, dict):
                        for k in ("license_ref", "rightsUri", "spdx", "url", "uri", "title"):
                            v = r.get(k)
                            if isinstance(v, str) and v.strip():
                                return v.strip()
                    elif isinstance(r, str) and r.strip():
                        return r.strip()
        return None

    raw = _best_license_url_or_text()
    if not raw:
        return None

    s = raw.strip().lower().rstrip("/")

    # CC0
    if "creativecommons.org" in s and ("publicdomain/zero" in s or "/cc0/" in s):
        return {"name": "CC0 1.0", "uri": "https://creativecommons.org/publicdomain/zero/1.0/"}

    # CC BY family
    if "creativecommons.org" in s and "/licenses/" in s:
        parts = [p for p in s.split("/") if p]
        try:
            code = parts[-2]  # e.g. "by-nc-sa"
            ver = parts[-1]  # e.g. "4.0"
        except Exception:
            code = ver = None
        code_map = {
            "by": "BY",
            "by-sa": "BY-SA",
            "by-nd": "BY-ND",
            "by-nc": "BY-NC",
            "by-nc-sa": "BY-NC-SA",
            "by-nc-nd": "BY-NC-ND",
        }
        if code in code_map and ver and any(ch.isdigit() for ch in ver):
            return {
                "name": f"CC {code_map[code]} {ver}",
                "uri": f"https://creativecommons.org/licenses/{code}/{ver}/",
            }
    return None


def _dv_field(name, value, tclass="primitive", multiple=False):
    return {"typeName": name, "typeClass": tclass, "multiple": multiple, "value": value}


def _dv_authors_field(ds: dict, dmp: dict) -> dict:
    """
    Dataverse 'author' is a compound field with subfields:
      - authorName (primitive)
      - authorAffiliation (primitive)
      - authorIdentifierScheme (controlledVocabulary)  e.g., "ORCID"
      - authorIdentifier (primitive)                   e.g., "https://orcid.org/0000-..."
    JSON shape: {"typeName":"author","typeClass":"compound","multiple":true,
                 "value":[
                    {"authorName": {...}, "authorAffiliation": {...}, ...},
                    ...
                 ]}
    """
    comps: list[dict] = []

    def _emit(name: str, aff: str | None, orcid: str | None):
        comp: dict[str, Any] = {"authorName": _dv_field("authorName", name)}
        if aff:
            comp["authorAffiliation"] = _dv_field("authorAffiliation", aff)
        if orcid:
            comp["authorIdentifierScheme"] = _dv_field(
                "authorIdentifierScheme", "ORCID", tclass="controlledVocabulary"
            )
            comp["authorIdentifier"] = _dv_field("authorIdentifier", f"https://orcid.org/{orcid}")
        comps.append(comp)

    # DMP contact first
    c = _get(dmp, ["dmp", "contact"]) or dmp.get("contact") or {}
    cname = c.get("name") or c.get("mbox") or "Unknown, Unknown"
    aff_name, _ = _affiliation_from_node(c.get("affiliation") or c.get("organization"))
    oc = None
    cid = c.get("contact_id") or {}
    if (cid.get("type") or "").lower() == "orcid":
        oc = _normalize_orcid(cid.get("identifier"))
    _emit(cname, aff_name, oc)

    # Contributors (root and dataset-level)
    def _iter_contrib_blocks():
        yield from _norm_list(_get(dmp, ["dmp", "contributor"]) or dmp.get("contributor"))
        yield from _norm_list(ds.get("contributor"))

    for cont in _iter_contrib_blocks():
        if not isinstance(cont, dict):
            continue
        nm = cont.get("name") or cont.get("fullname")
        if not nm:
            continue
        aff_name, _ = _affiliation_from_node(cont.get("affiliation") or cont.get("organization"))
        oc = None
        cid = cont.get("contributor_id") or {}
        if (cid.get("type") or "").lower() == "orcid":
            oc = _normalize_orcid(cid.get("identifier"))
        _emit(nm, aff_name, oc)

    return {"typeName": "author", "typeClass": "compound", "multiple": True, "value": comps}


def _dv_contacts_field(ds: dict, dmp: dict) -> dict | None:
    comps: list[dict] = []
    for c in _norm_list(ds.get("contact", [])):
        email = c.get("mbox") or c.get("email") or c.get("contactEmail")
        name = c.get("name") or c.get("fullname") or email or "Unknown"
        if email:
            comp = {
                "datasetContactEmail": _dv_field("datasetContactEmail", email),
                "datasetContactName": _dv_field("datasetContactName", name),
            }
            comps.append(comp)
    if comps:
        return {
            "typeName": "datasetContact",
            "typeClass": "compound",
            "multiple": True,
            "value": comps,
        }
    # Fallback to DMP contact
    c = _get(dmp, ["dmp", "contact"]) or dmp.get("contact") or {}
    email = c.get("mbox") or c.get("email")
    name = c.get("name") or email or "Unknown"
    if email:
        comp = {
            "datasetContactEmail": _dv_field("datasetContactEmail", email),
            "datasetContactName": _dv_field("datasetContactName", name),
        }
        comps.append(comp)
    return (
        {"typeName": "datasetContact", "typeClass": "compound", "multiple": True, "value": comps}
        if comps
        else None
    )


def _map_subject_to_dv(term: str) -> str | None:
    if not term:
        return None
    t = term.strip().lower()
    # try direct matches
    if t in DATAVERS_SUBJECTS:
        return DATAVERS_SUBJECTS[t]
    # a few helpful synonyms
    SYN = {
        "economics": "Business and Management",
        "management": "Business and Management",
        "finance": "Business and Management",
        "political science": "Social Sciences",
        "psychology": "Social Sciences",
        "sociology": "Social Sciences",
        "geology": "Earth and Environmental Sciences",
        "environment": "Earth and Environmental Sciences",
        "cs": "Computer and Information Science",
        "computer science": "Computer and Information Science",
        "it": "Computer and Information Science",
        "biomedicine": "Medicine, Health and Life Sciences",
        "life sciences": "Medicine, Health and Life Sciences",
    }
    if t in SYN:
        return SYN[t]
    return None


def _dv_subjects_field(ds: dict) -> dict:
    vals: list[str] = []
    for s in _norm_list(ds.get("subject")):
        term = s.get("term") if isinstance(s, dict) else s
        mapped = _map_subject_to_dv(term) if isinstance(term, str) else None
        if mapped and mapped not in vals:
            vals.append(mapped)
    if not vals:
        vals = ["Other"]
    return {
        "typeName": "subject",
        "typeClass": "controlledVocabulary",
        "multiple": True,
        "value": vals,
    }


def _dv_keywords_field(ds: dict) -> dict | None:
    comps: list[dict] = []
    for kw in _norm_list(ds.get("keyword") or []):
        if isinstance(kw, str) and kw.strip():
            comp = {"keywordValue": _dv_field("keywordValue", kw.strip())}
            comps.append(comp)
    return (
        {"typeName": "keyword", "typeClass": "compound", "multiple": True, "value": comps}
        if comps
        else None
    )


def _normalize_language(lang_raw: str | None) -> str | None:
    if not isinstance(lang_raw, str):
        return None
    s = lang_raw.strip().lower()
    MAP = {
        "en": "English",
        "eng": "English",
        "english": "English",
        "da": "Danish",
        "dan": "Danish",
        "dansk": "Danish",
    }
    return MAP.get(s) or None


def build_dataverse_dataset_version_from_madmp(
    dmp: dict,
    dataset_id: str | None = None,
    *,
    sensitive_flag: bool = False,
    skipped_files: list[str] | None = None,
) -> tuple[dict, dict]:
    ds = _guess_dataset(dmp, dataset_id)
    title = ds.get("title") or _get(dmp, ["dmp", "title"]) or "Untitled dataset"

    fields: list[dict] = []
    fields.append(_dv_field("title", title))
    fields.append(_dv_authors_field(ds, dmp))
    contacts_field = _dv_contacts_field(ds, dmp)
    if contacts_field:
        fields.append(contacts_field)

    # Description (+ privacy note if sensitive)
    base_desc = description_from_madmp(ds)
    if sensitive_flag:
        skipped = skipped_files or []
        if skipped:
            bullet = "\n".join(f"  - {os.path.basename(x)}" for x in skipped)
            base_desc += (
                "\n\n[NOTICE] Files withheld due to personal/sensitive data. "
                "A metadata-only record has been created. Skipped files:\n" + bullet
            )
        else:
            base_desc += "\n\n[NOTICE] Files withheld due to personal/sensitive data. A metadata-only record has been created."

    fields.append(
        {
            "typeName": "dsDescription",
            "typeClass": "compound",
            "multiple": True,
            "value": [{"dsDescriptionValue": _dv_field("dsDescriptionValue", base_desc)}],
        }
    )

    fields.append(_dv_subjects_field(ds))

    # Optional: keywords & language
    kws_field = _dv_keywords_field(ds)
    if kws_field:
        fields.append(kws_field)

    lang = _normalize_language(ds.get("language") or _get(dmp, ["dmp", "language"]))
    if lang:
        fields.append(
            {
                "typeName": "language",
                "typeClass": "controlledVocabulary",
                "multiple": True,
                "value": [lang],
            }
        )

    dataset_version: dict[str, Any] = {
        "datasetVersion": {
            "metadataBlocks": {"citation": {"displayName": "Citation Metadata", "fields": fields}}
        }
    }

    # Optional license (Dataverse JSON accepts license object with name+uri)
    lic = _license_for_dataverse(ds)
    if lic:
        dataset_version["datasetVersion"]["license"] = lic

    return dataset_version, {"dataset_title": title}


# ============================================================================
# Packaging for Dataverse (<=100 files total, <=953.7 MB per file)
# ============================================================================


@dataclass
class DVPlan:
    plan: list[PackItem]
    report: list[dict]
    workdir: str
    directory_label_for: dict[str, str | None]  # final path -> directory label
    double_zipped: bool = False


def _estimate_zip_size(members: list[str]) -> int:
    total, _ = sizes_bytes(members)
    return estimate_zip_total_bytes(members, total)


def _shard_members_by_size(members: list[str], max_bytes: int) -> list[list[str]]:
    """Greedy sharding: add files while estimated zip stays under target."""
    shards: list[list[str]] = []
    current: list[str] = []
    by_size = sorted(members, key=lambda p: os.path.getsize(p), reverse=True)
    for fp in by_size:
        trial = current + [fp]
        if _estimate_zip_size(trial) <= max_bytes or not current:
            current = trial
        else:
            shards.append(current)
            current = [fp]
    if current:
        shards.append(current)
    return shards


def _first_level_groups_safe(files: list[str]) -> tuple[Path, dict[str, list[str]]]:
    """Robust against relpath=='.' and cross-drive paths on Windows."""
    if not files:
        return Path("."), {"": []}
    files_abs = [os.path.abspath(f) for f in files]
    try:
        parent_str = os.path.commonpath(files_abs)
    except ValueError:
        parent_str = os.path.dirname(files_abs[0])
    parent = Path(parent_str)
    groups: dict[str, list[str]] = {}
    for f in files_abs:
        rel_str = os.path.relpath(f, start=parent_str)
        if rel_str in ("", "."):
            head = ""
        else:
            parts = rel_str.replace("\\", "/").split("/")
            head = "" if len(parts) == 1 else parts[0]
        groups.setdefault(head, []).append(f)
    return parent, groups


def _build_dataverse_packaging_plan(files: list[str]) -> DVPlan:
    """
    Preserve first-level folders. Create DEFLATED zips per folder (or per shard),
    ensuring each final archive <= TARGET_MAX_PER_ARCHIVE and total items <= 100.
    Singles (already small) are left as-is; oversize singles are skipped later.
    """
    if not files:
        tmp = tempfile.mkdtemp(prefix="dv_pack_")
        return DVPlan([], [], tmp, {})

    parent, groups = _first_level_groups_safe(files)
    dataset_root = parent.name or "dataset"

    workdir = tempfile.mkdtemp(prefix="dv_pack_")
    plan: list[PackItem] = []
    report: list[dict] = []
    directory_label_for: dict[str, str | None] = {}
    make_double_zip = DOUBLE_ZIP_TO_PREVENT_UNPACK

    singles: list[str] = []

    # Root files: if few and small, keep as singles; else shard into zips
    for sub, members in sorted(groups.items(), key=lambda kv: kv[0] or ""):
        if sub == "":
            if not members:
                continue
            all_fit = all(os.path.getsize(m) <= DATAVERSE_MAX_FILE_SIZE_BYTES for m in members)
            # Be conservative with singles in root: only a very small set
            if all_fit and len(members) <= 3:
                for m in members:
                    singles.append(m)
                    directory_label_for[m] = None  # root
                continue

            for idx, shard in enumerate(
                _shard_members_by_size(members, TARGET_MAX_PER_ARCHIVE), start=1
            ):
                zip_path = os.path.join(workdir, f"root_files_{idx:02d}.zip")
                plan.append(
                    PackItem("zip", zip_path=zip_path, members=shard, compression=ZIP_COMPRESSION)
                )
                _, shard_sizes = sizes_bytes(shard)
                report.append(
                    {
                        "archive": os.path.basename(zip_path),
                        "count": len(shard),
                        "bytes": sum(shard_sizes.values()),
                        "examples": [os.path.basename(m) for m in shard[:5]],
                        "first_level": "",
                        "dataset_root": dataset_root,
                    }
                )
                directory_label_for[zip_path] = None
        else:
            shards = _shard_members_by_size(members, TARGET_MAX_PER_ARCHIVE)
            for idx, shard in enumerate(shards, start=1):
                suffix = "" if len(shards) == 1 else f"_part{idx:02d}"
                zip_path = os.path.join(workdir, f"{sub}{suffix}.zip")
                plan.append(
                    PackItem("zip", zip_path=zip_path, members=shard, compression=ZIP_COMPRESSION)
                )
                _, shard_sizes = sizes_bytes(shard)
                report.append(
                    {
                        "archive": os.path.basename(zip_path),
                        "count": len(shard),
                        "bytes": sum(shard_sizes.values()),
                        "examples": [os.path.basename(m) for m in shard[:5]],
                        "first_level": sub,
                        "dataset_root": dataset_root,
                    }
                )
                directory_label_for[zip_path] = sub  # display old folder as label for the zip

    # Add remaining small singles (those not included in zips)
    for f in files:
        if f in singles or any(f in it.members for it in plan if it.kind == "zip"):
            continue
        if os.path.getsize(f) <= DATAVERSE_MAX_FILE_SIZE_BYTES:
            singles.append(f)
            rel = Path(os.path.relpath(f, start=parent))
            directory_label_for[f] = rel.parts[0] if len(rel.parts) > 1 else None

    # Reduce total count to <= 100 by merging zip groups if needed
    def _current_count() -> int:
        return len(singles) + sum(1 for it in plan if it.kind == "zip")

    if _current_count() > DATAVERSE_MAX_FILES_TOTAL:
        groups_z = []
        for it in plan:
            if it.kind != "zip":
                continue
            est = _estimate_zip_size(it.members)
            groups_z.append((est, it))

        groups_z.sort(key=lambda x: x[0], reverse=True)
        new_bins: list[list[PackItem]] = []
        new_bin_estimates: list[int] = []
        for est, it in groups_z:
            placed = False
            for bi in range(len(new_bins)):
                if new_bin_estimates[bi] + est <= TARGET_MAX_PER_ARCHIVE:
                    new_bins[bi].append(it)
                    new_bin_estimates[bi] += est
                    placed = True
                    break
            if not placed:
                new_bins.append([it])
                new_bin_estimates.append(est)

        # Rebuild plan if merging helps
        merged_plan: list[PackItem] = []
        merged_report: list[dict] = []
        if len(new_bins) < len(groups_z):
            for idx, bin_items in enumerate(new_bins, start=1):
                all_members: list[str] = []
                for it in bin_items:
                    all_members.extend(it.members)
                zip_path = os.path.join(workdir, f"merged_{idx:03d}.zip")
                merged_plan.append(
                    PackItem(
                        "zip", zip_path=zip_path, members=all_members, compression=ZIP_COMPRESSION
                    )
                )
                _, sizes_map = sizes_bytes(all_members)
                merged_report.append(
                    {
                        "archive": os.path.basename(zip_path),
                        "count": len(all_members),
                        "bytes": sum(sizes_map.values()),
                        "examples": [os.path.basename(m) for m in all_members[:5]],
                        "first_level": "",  # spans multiple folders
                        "dataset_root": dataset_root,
                    }
                )
                directory_label_for[zip_path] = None
            plan = merged_plan
            report = [
                r for r in report if r.get("archive", "").startswith(("root_files_",))
            ] + merged_report

        if _current_count() > DATAVERSE_MAX_FILES_TOTAL:
            raise PublishError(
                "After packaging, file count still exceeds 100 while staying under the per-file size cap. "
                "Please split the dataset into multiple records."
            )

    # Final sanity: ensure archived groups estimate under hard cap
    for it in [p for p in plan if p.kind == "zip"]:
        if _estimate_zip_size(it.members) > DATAVERSE_MAX_FILE_SIZE_BYTES:
            raise PublishError(
                "A packaged archive still exceeds the 953.7 MB per-file limit. "
                "Please split the dataset differently."
            )

    return DVPlan(
        plan=plan,
        report=report,
        workdir=workdir,
        directory_label_for=directory_label_for,
        double_zipped=make_double_zip,
    )


# ============================================================================
# Utility: allow directories to pass through to packaging (keeps dirs & files)
# ============================================================================


def paths_existing_for_packaging(paths: list[str]) -> list[str]:
    """
    Keep existing files OR directories (follow symlinks). Skip broken/unreadable paths.
    Returns normalized absolute paths.
    """
    out: list[str] = []
    for raw in paths:
        try:
            p = Path(raw)
            if not p.exists():
                continue
            t = p.resolve()
            if t.is_file() or t.is_dir():
                out.append(str(t))
        except OSError:
            continue
    return out


# ============================================================================
# Publish flow
# ============================================================================


def _dv_worker_upload_then_publish(
    *,
    dmp_path: str,
    dataset_title: str | None,
    base_url: str,
    token: str,
    ds_id: int,
    restrict_files: bool,
    publish: bool,
) -> None:
    """
    Background worker:
      - re-read DMP
      - compute file list
      - plan -> realize packaging (zips)
      - parallel upload to Dataverse
      - optional publish
    """
    try:
        with open(dmp_path, encoding="utf-8") as fh:
            _dmp = json.load(fh)
        ds = _guess_dataset(_dmp, dataset_title or None)

        # Privacy gate
        sensitive = _has_personal_or_sensitive(ds)

        # Files from repokit_info (keep files AND dirs for packaging)
        repokit_info_files = files_from_repokit_info(ds)

        # Resolve relative paths against DMP location
        dmp_dir = str(Path(dmp_path).resolve().parent)
        resolved: list[str] = []
        for p in repokit_info_files:
            resolved.append(p if os.path.isabs(p) else os.path.normpath(os.path.join(dmp_dir, p)))
        repokit_info_files = resolved

        uploads_raw = [] if sensitive else paths_existing_for_packaging(repokit_info_files)
        pre_count = len(uploads_raw)
        if sensitive or pre_count == 0:
            return  # metadata-only (by design)

        # Build one packaging plan (no zips yet)
        dvplan = _build_dataverse_packaging_plan(uploads_raw)

        # Realize plan (create inner zips) in parallel
        inner_paths = realize_packaging_plan_parallel(dvplan.plan)

        # Add small singles the plan intentionally left out of zips
        for p, _lbl in dvplan.directory_label_for.items():
            if os.path.isfile(p) and p not in inner_paths:
                inner_paths.append(p)

        # Optional double-zip wrapper (to avoid auto-unpack)
        final_paths: list[str] = []
        if dvplan.double_zipped:
            for fp in inner_paths:
                if (
                    fp.lower().endswith(".zip")
                    and os.path.getsize(fp) <= DATAVERSE_MAX_FILE_SIZE_BYTES
                ):
                    outer = os.path.join(
                        os.path.dirname(fp), os.path.basename(fp).replace(".zip", "_outer.zip")
                    )
                    with zipfile.ZipFile(
                        outer, "w", compression=ZIP_COMPRESSION, allowZip64=True
                    ) as zf:
                        zf.write(fp, arcname=os.path.basename(fp))
                    if os.path.getsize(outer) <= DATAVERSE_MAX_FILE_SIZE_BYTES:
                        final_paths.append(outer)
                        # carry forward the label mapping from inner zip to outer zip
                        dvplan.directory_label_for[outer] = dvplan.directory_label_for.get(fp)
                    # else skip as too big
                else:
                    final_paths.append(fp)
        else:
            final_paths = inner_paths

        # Guardrails
        final_paths = [
            fp
            for fp in final_paths
            if os.path.exists(fp) and os.path.getsize(fp) <= DATAVERSE_MAX_FILE_SIZE_BYTES
        ]
        if not final_paths:
            return

        # Parallel upload to Dataverse
        _uploaded, _errs = _dv_upload_many_parallel(
            base_url, token, ds_id, final_paths, dvplan.directory_label_for, restrict_files
        )
        # Optional publish
        if publish:
            _dv_publish_dataset(base_url, token, ds_id, release_type="major")

    except Exception as e:
        # swallow exceptions in background to avoid killing Streamlit; log if you prefer
        try:
            print("[dv worker error]", e)
        except Exception:
            pass
    finally:
        try:
            os.unlink(dmp_path)
        except Exception:
            pass


def streamlit_publish_to_dataverse(
    *,
    dataset: dict,
    dmp: dict,
    token: str,
    base_url: str,
    alias: str,
    publish: bool = False,
    allow_reused: bool = False,
    release_type: str = "major",
) -> dict[str, Any]:
    """
    Create a Dataverse dataset WITH metadata first (so the draft is visible immediately),
    then start a background worker that packages + uploads files and optionally publishes.

    IMPORTANT: This variant does *not* run the packaging planner up front.
    It only shows a lightweight preview (counts/bytes) and defers planning
    and zipping to the background worker right before upload.
    """
    # Block reused datasets unless explicitly allowed (keep existing behavior)
    if (
        str(dataset.get("is_reused", "")).strip().lower() in {"true", "1", "yes"}
        and not allow_reused
    ):
        raise PublishError("Blocked: dataset is marked re-used (is_reused=true) in the DMP.")

    # Write DMP to a temp file the worker can read later
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    ) as tf:
        json.dump(dmp, tf, ensure_ascii=False, indent=2)
        dmp_path = tf.name

    # Lightweight preview only (no planning)
    ds_preview = _guess_dataset(dmp, dataset.get("title") or None)
    sensitive_preview = _has_personal_or_sensitive(ds_preview)

    # Resolve + existence check (cheap)
    repokit_info_files = files_from_repokit_info(ds_preview)
    dmp_dir = str(Path(dmp_path).resolve().parent)
    resolved_preview = [
        p if os.path.isabs(p) else os.path.normpath(os.path.join(dmp_dir, p))
        for p in repokit_info_files
    ]
    uploads_raw_preview = (
        [] if sensitive_preview else [p for p in resolved_preview if os.path.exists(p)]
    )
    pre_total, _ = sizes_bytes(uploads_raw_preview)
    pre_count = len(uploads_raw_preview)

    # Build metadata for the visible draft
    skipped_due_to_privacy = resolved_preview if sensitive_preview else []
    dataset_version, _extra = build_dataverse_dataset_version_from_madmp(
        dmp,
        dataset.get("title") or None,
        sensitive_flag=sensitive_preview,
        skipped_files=skipped_due_to_privacy,
    )

    # Add a *lazy* PACKAGING NOTE (no planned paths/report yet)
    ds_fields = dataset_version["datasetVersion"]["metadataBlocks"]["citation"]["fields"]
    base_desc = next(
        (
            f["value"][0]["dsDescriptionValue"]["value"]
            for f in ds_fields
            if f.get("typeName") == "dsDescription"
        ),
        "No description provided.",
    )

    # Keep the preview cheap: show counts/bytes only; say packing will be computed at upload time.
    base_desc = append_packaging_note(
        base_desc,
        pre_files=pre_count,
        pre_bytes=pre_total,
        final_paths=[],  # ← no planning yet
        report=[],  # ← no report yet
    )
    base_desc += "\n\n[INFO] Packaging is deferred. Archives will be computed just before upload."

    if not sensitive_preview and DOUBLE_ZIP_TO_PREVENT_UNPACK:
        base_desc += "\n[NOTE] Zip containers may be double-zipped to prevent auto-unpacking."

    for f in ds_fields:
        if f.get("typeName") == "dsDescription":
            f["value"][0]["dsDescriptionValue"]["value"] = base_desc
            break

    # 1) Create the dataset (metadata-only) — immediately visible as a draft
    resp = _dv_create_dataset(base_url, token, alias, dataset_version)
    data = resp.get("data") or {}
    ds_id = data.get("id")
    pid = data.get("persistentId") or data.get("identifier")
    if not ds_id:
        try:
            os.unlink(dmp_path)
        except Exception:
            pass
        raise PublishError(f"Dataverse: missing dataset id in response: {resp}")

    # Compute 'restrict' now (cheap)
    def _first_distribution(_ds: dict) -> dict:
        d = _ds.get("distribution")
        if isinstance(d, dict):
            return d
        if isinstance(d, list) and d:
            return d[0]
        return {}

    access = (_first_distribution(ds_preview).get("data_access") or "").strip().lower()
    restrict_files = (access in {"shared", "closed"}) or sensitive_preview

    # 2) Show draft link immediately
    html_url = f"{base_url.rstrip('/')}/dataset.xhtml?persistentId={pid}" if pid else None
    if st is not None and html_url:
        st.success(
            f"✅ Dataverse draft created [Open in Dataverse]({html_url}). Packaging & upload will start shortly in the background."
        )

    # 3) Fire-and-forget background worker; it will do the *actual* planning right before upload
    Thread(
        target=_dv_worker_upload_then_publish,
        kwargs=dict(
            dmp_path=dmp_path,  # worker re-reads DMP
            dataset_title=dataset.get("title") or None,
            base_url=base_url,
            token=token,
            ds_id=ds_id,
            restrict_files=restrict_files,
            publish=(publish and release_type == "major"),
        ),
        daemon=True,
    ).start()
    return {"id": ds_id, "persistent_id": pid, "url": html_url}
