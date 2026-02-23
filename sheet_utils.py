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
    """找不到時回傳 None（不拋例外），讓呼叫端決定如何處理。"""
    if not application_id:
        return None
    application_id = str(application_id).strip()
    for r in records:
        if str(r.get(id_column, "")).strip() == application_id:
            return r
    raise LookupError(f"找不到申請單編號：{application_id}")


def safe_string(value) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    if value.lower() in {"nan", "none", "null"}:
        return ""
    return value


def normalize_key(key: str) -> str:
    return re.sub(r"[\s\-_()（）\[\]{}:：/\\]+", "", safe_string(key)).lower()


def find_best_matched_value(row: dict, candidate_keys: list[str], keyword_groups: list[tuple[str, ...]]) -> str:
    normalized_map = {normalize_key(k): v for k, v in row.items()}

    for key in candidate_keys:
        normalized_candidate = normalize_key(key)
        if normalized_candidate in normalized_map:
            value = safe_string(normalized_map[normalized_candidate])
            if value:
                return value

    for normalized_key, value in normalized_map.items():
        text = safe_string(value)
        if not text:
            continue
        for keywords in keyword_groups:
            if all(keyword in normalized_key for keyword in keywords):
                return text

    return ""


def extract_medical_histories(row, personal_keys=None, family_keys=None) -> tuple[str, str]:
    """row 為 None 時直接回傳空字串，不拋例外。"""
    if not row:
        return "", ""

    personal_keys = personal_keys or [
        "個人疾病史（可複選）", "個人疾病史(可複選)",   # Google Form 實際欄名
        "個人疾病史", "個人病史", "個人史", "過往病史", "既往病史",
    ]
    family_keys = family_keys or [
        "家族疾病史（可複選）", "家族疾病史(可複選)",   # Google Form 實際欄名
        "家族疾病史", "家族病史", "家族史",
    ]

    personal_keyword_groups = [
        ("個人", "疾病", "史"),
        ("個人", "病", "史"),
        ("既往", "病", "史"),
        ("過往", "病", "史"),
    ]
    family_keyword_groups = [
        ("家族", "疾病", "史"),
        ("家族", "病", "史"),
        ("家族", "史"),
    ]

    personal_history = find_best_matched_value(row, personal_keys, personal_keyword_groups)
    family_history = find_best_matched_value(row, family_keys, family_keyword_groups)

    return personal_history, family_history
