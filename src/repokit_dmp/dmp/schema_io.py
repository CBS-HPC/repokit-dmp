import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from repokit_common import (
    PROJECT_ROOT,
    read_toml,
    split_multi,
    JSON_FILENAME,
    TOML_PATH,
    TOOL_NAME,
)


def now_iso_minute() -> str:
    return datetime.utcnow().replace(second=0, microsecond=0).isoformat() + "Z"


def today_iso() -> str:
    return datetime.utcnow().date().isoformat()


def load_json(path: Path | str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    if path.stat().st_size == 0:
        return {}
    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_json(path: Path | str | os.PathLike[str], data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Config (RDA-DMP 1.)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


SCHEMA_DOWNLOAD_URLS: dict[str, str] = {
    "1.0": (
        "https://raw.githubusercontent.com/RDA-DMP-Common/"
        "RDA-DMP-Common-Standard/master/examples/JSON/JSON-schema/"
        "1.0/maDMP-schema-1.0.json"
    ),
    "1.1": (
        "https://raw.githubusercontent.com/RDA-DMP-Common/"
        "RDA-DMP-Common-Standard/master/examples/JSON/JSON-schema/"
        "1.1/maDMP-schema-1.1.json"
    ),
    "1.2": (
        "https://raw.githubusercontent.com/RDA-DMP-Common/"
        "RDA-DMP-Common-Standard/master/examples/JSON/JSON-schema/"
        "1.2/maDMP-schema-1.2.json"
    ),
}

SCHEMA_CACHE_FILES: dict[str, Path] = {
    "1.0": Path("./bin/maDMP-schema-1.0.json"),
    "1.1": Path("./bin/maDMP-schema-1.1.json"),
    "1.2": Path("./bin/maDMP-schema-1.2.json"),
}

# Always store this exact value in dmp["schema"]
SCHEMA_URLS: dict[str, str] = {
    "1.0": (
        "https://github.com/RDA-DMP-Common/RDA-DMP-Common-Standard/"
        "tree/master/examples/JSON/JSON-schema/1.0"
    ),
    "1.1": (
        "https://github.com/RDA-DMP-Common/RDA-DMP-Common-Standard/"
        "tree/master/examples/JSON/JSON-schema/1.1"
    ),
    "1.2": (
        "https://github.com/RDA-DMP-Common/RDA-DMP-Common-Standard/"
        "tree/master/examples/JSON/JSON-schema/1.2"
    ),
}

DEFAULT_DMP_PATH = Path("./dmp.json")
REPOKIT_INFO_KEY = "repokit_info"
LEGACY_REPOKIT_INFO_KEYS: tuple[str, ...] = ()


def schema_version_from_url(url: str, default: str = "1.2") -> str:
    """
    Return the schema version (e.g. "1.0", "1.1", "1.2") if the URL
    exactly matches one of the known SCHEMA_URLS values.
    Otherwise, return the default ("1.2").
    """
    if not isinstance(url, str):
        return default
    for ver, known_url in SCHEMA_URLS.items():
        if url.strip() == known_url:
            return ver
    return default


dmp = load_json(DEFAULT_DMP_PATH)

schema_url = dmp.get("dmp", {}).get("schema")
if schema_url:
    SCHEMA_VERSION = schema_version_from_url(schema_url)
else:
    SCHEMA_VERSION = "1.2"

DMP_KEY_ORDER = [
    "schema",
    "title",
    "description",
    "language",
    "created",
    "modified",
    "ethical_issues_exist",
    "ethical_issues_description",
    "ethical_issues_report",
    "dmp_id",
    "contact",
    "contributor",
    "project",
    "dataset",
    "extension",
]

# Order of fields inside each dataset object
DATASET_KEY_ORDER: list[str] = [
    "title",
    "description",
    "issued",
    "modified",
    "language",
    "keyword",
    "is_reused",
    "personal_data",
    "sensitive_data",
    "type",
    "preservation_statement",
    "dataset_id",
    "distribution",
    "data_quality_assurance",
    "metadata",
    "security_and_privacy",
    "technical_resource",
    "extension",
]

# Order of fields inside each distribution object
DISTRIBUTION_KEY_ORDER: list[str] = [
    # "title",
    # "description",
    "access_url",
    # "download_url",
    "format",
    "byte_size",
    "data_access",
    "host",
    "available_until",
    "license",
]

# Small nested objects
DATASET_ID_KEY_ORDER = ["identifier", "type"]
METADATA_ITEM_KEY_ORDER = ["language", "metadata_standard_id", "description"]
SEC_PRIV_ITEM_KEY_ORDER = ["title", "description"]
TECH_RES_ITEM_KEY_ORDER = ["name", "description"]
HOST_KEY_ORDER = ["title", "url"]
LICENSE_ITEM_KEY_ORDER = ["license_ref", "start_date"]


# Central license mapping: short code â†’ canonical URL
LICENSE_LINKS: dict[str, str] = {
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "None": "",
}

# Extra enums we want the editor to offer in addition to (or instead of) schema-provided enums
EXTRA_ENUMS: dict[str, list[str]] = {
    # For dataset.distribution[].license[].license_ref we want the *URL* values as the choices
    "dmp.dataset[].distribution[].license[].license_ref": list(LICENSE_LINKS.values()),
}

# Minimal offline fallbacks for the common triads
LOCAL_FALLBACK_ENUMS: dict[str, list[str]] = {
    "dmp.dataset[].personal_data": ["yes", "no", "unknown"],
    "dmp.dataset[].sensitive_data": ["yes", "no", "unknown"],
    # You can add more safe fallbacks here if useful
}


# Minimal, readable mapping. Add/adjust as needed.

DK_UNI_MAP = {
    # Copenhagen Business School
    "cbs.dk": {
        "name": "Copenhagen Business School",
        "abbreviation": "CBS",
        "ror": "https://ror.org/04sppb023",
        "dataverse_alias": "cbs",
        "dataverse_default_base_url": "https://demo.dataverse.deic.dk",
    },
    # University of Copenhagen
    "ku.dk": {
        "name": "University of Copenhagen",
        "abbreviation": "KU",
        "ror": "https://ror.org/035b05819",
        "dataverse_alias": "ku",
        "dataverse_default_base_url": "https://dataverse.deic.dk",  # production for KU
    },
    # University of Southern Denmark
    "sdu.dk": {
        "name": "University of Southern Denmark",
        "abbreviation": "SDU",
        "ror": "https://ror.org/03yrrjy16",
        "dataverse_alias": "sdu",
        "dataverse_default_base_url": "https://demo.dataverse.deic.dk",
    },
    # Aarhus University
    "au.dk": {
        "name": "Aarhus University",
        "abbreviation": "AU",
        "ror": "https://ror.org/01aj84f44",
        "dataverse_alias": "au",
        "dataverse_default_base_url": "https://demo.dataverse.deic.dk",
    },
    # Technical University of Denmark
    "dtu.dk": {
        "name": "Technical University of Denmark",
        "abbreviation": "DTU",
        "ror": "https://ror.org/04qtj9h94",
        "dataverse_alias": "dtu",
        "dataverse_default_base_url": "https://demo.dataverse.deic.dk",
    },
    # Aalborg University
    "aau.dk": {
        "name": "Aalborg University",
        "abbreviation": "AAU",
        "ror": "https://ror.org/04m5j1k67",
        "dataverse_alias": "aau",
        "dataverse_default_base_url": "https://demo.dataverse.deic.dk",
    },
    # Roskilde University
    "ruc.dk": {
        "name": "Roskilde University",
        "abbreviation": "RUC",
        "ror": "https://ror.org/014axpa37",
        "dataverse_alias": "ruc",
        "dataverse_default_base_url": "https://demo.dataverse.deic.dk",
    },
    # IT University of Copenhagen
    "itu.dk": {
        "name": "IT University of Copenhagen",
        "abbreviation": "ITU",
        "ror": "https://ror.org/02309jg23",
        "dataverse_alias": "itu",
        "dataverse_default_base_url": "https://demo.dataverse.deic.dk",
    },
}


def dmp_default_templates(now_dt: str | None = None, today: str | None = None) -> dict:
    """
    Single source of truth for default values.
    Returns dict with keys: root, project, dataset, distribution, repokit_info.
    All optional strings default to "", booleans are true bools,
    and date/time fields match the schema's formats.
    """
    now_dt = now_dt or now_iso_minute()  # date-time with Z
    today = today or today_iso()

    cookie = (
        read_toml(
            folder=str(PROJECT_ROOT),
            json_filename=JSON_FILENAME,
            tool_name=TOOL_NAME,
            toml_path=TOML_PATH,
        )
        or {}
    )

    return {
        "root": {
            "schema": SCHEMA_URLS[SCHEMA_VERSION],
            "title": "",
            "description": "",
            "language": "eng",
            "created": now_dt,  # required date-time
            "modified": now_dt,  # required date-time
            "ethical_issues_exist": "unknown",
            "ethical_issues_description": "",
            "ethical_issues_report": "https://example.org/ethics-report",
            "dmp_id": {  # required (identifier, type)
                "identifier": "https://example.org/dmp",
                "type": "url",
            },
            "contact": {  # required (contact_id, mbox, name)
                "name": "",
                "mbox": "",
                "contact_id": {
                    "identifier": "https://orcid.org/0000-0000-0000-0000",
                    "type": "orcid",
                },
                "affiliation": {
                    "name": "",
                    "abbreviation": "",
                    "region": None,
                    "affiliation_id": {"type": "ror", "identifier": ""},
                },
            },
            # "contributor": [{                    # required (contact_id, mbox, name)
            #    "name": "",
            #    "mbox": "",
            #    "contributor_id": {
            #        "identifier": "https://orcid.org/0000-0000-0000-0000",
            #        "type": "orcid",
            #    },
            # "affiliation": {
            # "name": "Copenhagen Business School",
            # "abbreviation": "CBS",
            # "region": None,
            # "affiliation_id": {
            #    "type": "ror",
            #    "identifier": "https://ror.org/04sppb023"
            # }
            # }],
            "project": [],  # array
            "dataset": [],  # required array
            "extension": [],
        },
        "project": {  # items must have title
            "title": "",
            "description": "",
            "start": today,  # format: date if set
            "end": "",  # format: date if set
            "funding": [],
            # "funding": [{"funder_id": "", "funder_status": "","grant_id": {"identifier": "", "type": ""}}],
        },
        "dataset": {
            "title": "",  # required
            "description": "",
            "issued": "",  # format: date
            "modified": "",  # optional string if you use it
            "language": "eng",
            "keyword": [],
            "type": "",
            "is_reused": False,  # boolean
            "dataset_id": {
                "identifier": "",
                "type": "doi",
            },
            "personal_data": "unknown",  # enum
            "sensitive_data": "unknown",  # enum
            "distribution": [],
            "preservation_statement": "",
            "data_quality_assurance": [],
            "metadata": [
                {
                    "language": "eng",
                    "metadata_standard_id": {"identifier": "", "type": "url"},
                    "description": "",
                }
            ],
            "security_and_privacy": [{"title": "", "description": ""}],
            "technical_resource": [{"name": "", "description": ""}],
            "extension": [],
        },
        "distribution": {
            # "title": "",              # required
            # "description": "",               # string
            "access_url": "",  # string (url if you have one)
            # "download_url": "",              # string (url if you have one)
            "format": [],
            "byte_size": 0,  # integer
            "data_access": "open",  # enum
            # "host": {                        # required object: title+url
            #    "title": "Project repository",
            #    "url": "https://example.org",
            # },
            "available_until": "",  # format: date if set
            "license": [
                {
                    "license_ref": LICENSE_LINKS.get(cookie.get("DATA_LICENSE"), ""),
                    "start_date": today,
                }
            ],
        },
        "repokit_info": {  # extension payload (not part of RDA schema; free-form)
            "data_type": "Uncategorised",
            "destination": "",
            "number_of_files": 0,
            "total_size_mb": 0,
            "file_formats": [],
            "data_files": [],
            "data_size_mb": [],
            "hash": "",
        },
    }


def _affiliation_from_email(email: str) -> dict | None:
    """
    Guess affiliation from a Danish university email.
    Returns an 'affiliation' dict like your DMP schema expects, or None if unknown.

    Example return:
    {
        "name": "Copenhagen Business School",
        "abbreviation": "CBS",
        "region": None,
        "affiliation_id": {"type": "ror", "identifier": "https://ror.org/04sppb023"}
    }
    """
    if not isinstance(email, str) or "@" not in email:
        return None

    domain = email.split("@", 1)[1].strip().lower()

    def _matches_suffix(dom: str, suffix: str) -> bool:
        # handles "dept.ku.dk", "student.cbs.dk", etc.
        return dom == suffix or dom.endswith("." + suffix)

    for suffix, org in DK_UNI_MAP.items():
        if _matches_suffix(domain, suffix):
            return {
                "name": org["name"],
                "abbreviation": org["abbreviation"],
                "region": None,
                "affiliation_id": {
                    "type": "ror",
                    "identifier": org["ror"],
                }
                if org.get("ror")
                else None,
            }

    return {
        "name": "",
        "abbreviation": "",
        "region": None,
        "affiliation_id": {"type": "ror", "identifier": ""},
    }


def _set_contacts(dmp: dict, cookie: dict, overwrite: bool = False):
    authors = split_multi(cookie.get("AUTHORS"))
    emails = split_multi(cookie.get("EMAIL"))
    orcids = split_multi(cookie.get("ORCIDS"))

    name = authors[0] if authors else None
    mbox = emails[0] if emails else None
    orcid = orcids[0] if orcids else None

    if overwrite or ((name or mbox or orcid) and not dmp.get("contact")):
        info: dict[str, Any] = {}
        if name:
            info["name"] = name
        if mbox:
            info["mbox"] = mbox
        if orcid:
            info["contact_id"] = {"type": "orcid", "identifier": orcid}
        if mbox:
            info["affiliation"] = _affiliation_from_email(mbox)

        dmp["contact"] = info

    # contributors (idx 1..end)
    contributors = []
    max_len = max(len(authors), len(emails), len(orcids)) if (authors or emails or orcids) else 0

    for i in range(1, max_len):
        name = authors[i] if i < len(authors) else None
        mbox = emails[i] if i < len(emails) else None
        orcid = orcids[i] if i < len(orcids) else None

        if not (name or mbox or orcid):
            continue

        contributor_info: dict[str, Any] = {}
        if name:
            contributor_info["name"] = name
        if mbox:
            contributor_info["mbox"] = mbox
            contributor_info["affiliation"] = _affiliation_from_email(mbox)
        if orcid:
            contributor_info["contributor_id"] = {"type": "orcid", "identifier": orcid}

        contributors.append(contributor_info)

    if overwrite or (contributors and not dmp.get("contributor")):
        if not contributors:
            dmp.pop("contributor", None)
        else:
            dmp["contributor"] = contributors  # list of dicts

    return dmp
