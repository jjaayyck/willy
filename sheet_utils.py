import re

def parse_application_id(filename: str) -> str:
    name = filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    name = re.sub(r"\.(xlsx|xls)$", "", name, flags=re.IGNORECASE)

    m = re.match(r"^[^-]+-([^_]+)_.+$", name)
    if not m:
        raise ValueError(f"檔名格式不符合預期：{filename}")
    return m.group(1).strip()

def normalize_record_keys(records):
    return [{k.strip(): v for k, v in r.items()} for r in records]

def find_row_by_application_id(records, application_id, id_column="申請單編號"):
    application_id = str(application_id).strip()
    for r in records:
        if str(r.get(id_column, "")).strip() == application_id:
            return r
    raise LookupError(f"找不到申請單編號：{application_id}")