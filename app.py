import streamlit as st
import os
import openpyxl
import json
import re
import time
import gspread
from google import genai
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from sheet_utils import (
    parse_application_id,
    normalize_record_keys,
    find_row_by_application_id,
    extract_medical_histories,
    extract_lifestyle_habits,
)

# 載入環境變數
load_dotenv()

def build_language_system_rule(lang: str, word_limit: int) -> str:
    unit_desc = "words" if lang == "English" else "characters (non-space)"
    return f"""
# LANGUAGE CONSTRAINT — ABSOLUTE RULE (HIGHEST PRIORITY)

The user has selected the output language: {lang}

You MUST write the ENTIRE response strictly in this language.
Any violation makes the response INVALID.
You MUST keep the total output within {word_limit} {unit_desc} for the JSON values.

- If lang is "English":
  - Respond in English ONLY
  - DO NOT output any Chinese characters (no 中文/漢字)
- If lang is "繁體中文":
  - Respond in Traditional Chinese ONLY
- If lang is "日本語":
  - すべて日本語で回答してください
- If lang is "한국어":
  - 모든 내용을 한국어로 작성하세요
- If lang is "Tiếng Việt":
  - Trả lời hoàn toàn bằng tiếng Việt

Return JSON ONLY. No extra text outside JSON.
""".strip()

def is_language_valid(text: str, lang: str) -> bool:
    if lang == "English":
        return not re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", text)
    if lang == "繁體中文":
        return not re.search(r"[\u3040-\u30ff]", text)
    if lang == "日本語":
        return bool(re.search(r"[\u3040-\u30ff]", text))
    if lang == "한국어":
        return bool(re.search(r"[\uac00-\ud7af]", text))
    if lang == "Tiếng Việt":
        return bool(re.search(r"[A-Za-zÀ-ỹ]", text))
    return True

def count_output_length(text: str, lang: str) -> int:
    if lang == "English":
        return len(re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?", text))
    return len(re.findall(r"\S", text))

def truncate_to_limit(text: str, limit: int, lang: str) -> str:
    if limit <= 0:
        return ""
    if lang == "English":
        tokens = re.split(r"(\s+)", str(text))
        kept = []
        word_count = 0
        truncated = False
        for token in tokens:
            if not token:
                continue
            token_words = len(re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?", token))
            if token_words == 0:
                kept.append(token)
                continue
            if word_count + token_words > limit:
                truncated = True
                break
            kept.append(token)
            word_count += token_words
        result = "".join(kept).strip()
        return result + "…" if result and truncated else result

    non_space_count = 0
    chars = []
    was_truncated = False
    for ch in str(text):
        if not ch.isspace():
            non_space_count += 1
            if non_space_count > limit:
                was_truncated = True
                break
        chars.append(ch)
    result = "".join(chars).strip()
    return result + "…" if result and was_truncated else result

def normalize_report_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if not value:
            return ""
        return " ".join(str(v) for v in value.values())
    if isinstance(value, list):
        if not value:
            return ""
        return " ".join(str(v) for v in value)
    return str(value)

def min_section_length(word_limit: int) -> int:
    return max(40, int(word_limit * 0.08))

def min_total_length(word_limit: int) -> int:
    return max(280, int(word_limit * 0.85))

def validate_report_output(report: dict, lang: str, word_limit: int, strict_length: bool = False) -> tuple[bool, str, int]:
    combined_text = " ".join(normalize_report_value(v) for v in report.values())
    if not is_language_valid(combined_text, lang):
        return False, "語言不符合選擇", count_output_length(combined_text, lang)
    section_min = min_section_length(word_limit)
    required_keys = ["maintenance", "tracking", "nutrition", "supplements", "lifestyle"]
    for key in required_keys:
        section_text = normalize_report_value(report.get(key)).strip()
        if not section_text:
            return False, f"{key} 欄位內容為空", count_output_length(combined_text, lang)
        section_length = count_output_length(section_text, lang)
        if section_length < section_min:
            return False, f"{key} 欄位內容過短", count_output_length(combined_text, lang)
    length = count_output_length(combined_text, lang)
    total_min = min_total_length(word_limit)
    if length < total_min:
        return False, f"總字數過短（{length}/{total_min}）", length
    if length > word_limit:
        if strict_length:
            return False, f"超過字數限制（{length}/{word_limit}）", length
        return True, f"超過字數限制（{length}/{word_limit}），將自動壓縮", length
    return True, "", length

def enforce_report_length(report: dict, word_limit: int, lang: str) -> tuple[dict, int]:
    budget = build_length_budget(word_limit)
    adjusted = {}
    for key in ["maintenance", "tracking", "nutrition", "supplements", "lifestyle"]:
        section_text = normalize_report_value(report.get(key))
        adjusted[key] = truncate_to_limit(section_text, budget[key], lang).strip()
    total_length = count_output_length(" ".join(adjusted.values()), lang)
    return adjusted, total_length

def build_length_budget(word_limit: int) -> dict:
    weights = {
        "maintenance": 0.2,
        "tracking": 0.15,
        "nutrition": 0.2,
        "supplements": 0.2,
        "lifestyle": 0.25,
    }
    remaining = word_limit
    budget = {}
    ordered_keys = list(weights.keys())
    for key in ordered_keys[:-1]:
        allocation = max(1, int(word_limit * weights[key]))
        allocation = min(allocation, remaining)
        budget[key] = allocation
        remaining -= allocation
    budget[ordered_keys[-1]] = max(1, remaining)
    return budget

def format_budget_hint(budget: dict) -> str:
    return (
        f'maintenance≤{budget["maintenance"]}, '
        f'tracking≤{budget["tracking"]}, '
        f'nutrition≤{budget["nutrition"]}, '
        f'supplements≤{budget["supplements"]}, '
        f'lifestyle≤{budget["lifestyle"]}'
    )


def load_records_from_google_sheet(sheet_url: str, worksheet_name: str | None = None, worksheet_gid: int | None = None):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    service_account_info = None
    if "gcp_service_account" in st.secrets:
        service_account_info = dict(st.secrets["gcp_service_account"])
    else:
        service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if service_account_json:
            service_account_info = json.loads(service_account_json)

    if service_account_info:
        credentials = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    else:
        service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        if not service_account_file:
            raise ValueError("缺少 Google Service Account 設定，請設定 Streamlit secrets 或 GOOGLE_SERVICE_ACCOUNT_FILE / GOOGLE_SERVICE_ACCOUNT_JSON。")
        credentials = Credentials.from_service_account_file(service_account_file, scopes=scopes)

    gc = gspread.authorize(credentials)
    spreadsheet = gc.open_by_url(sheet_url)
    if worksheet_gid is not None:
        worksheet = spreadsheet.get_worksheet_by_id(worksheet_gid)
    elif worksheet_name:
        worksheet = spreadsheet.worksheet(worksheet_name)
    else:
        worksheet = spreadsheet.sheet1
    return normalize_record_keys(worksheet.get_all_records())

# --- 1. 核心邏輯：擷取 Excel 數據 ---
def extract_data_from_upload(uploaded_file, threshold_low=30, threshold_std=37):
    # Streamlit 上傳的檔案是 BytesIO 物件
    wb = openpyxl.load_workbook(uploaded_file, data_only=True)
    ws = wb.active
    
    # 版型判定
    count_a = sum(1 for r in range(3, 15) if ws.cell(row=r, column=1).value)
    count_b = sum(1 for r in range(3, 15) if ws.cell(row=r, column=2).value)
    is_5_slot = count_b >= count_a * 1.2

    user_info = {}
    if is_5_slot:
        user_info['age'] = ws.cell(row=2, column=5).value
        user_info['gender'] = ws.cell(row=2, column=6).value
        start_row, step, p_col = 3, 5, 2
    else:
        user_info['age'] = ws.cell(row=2, column=7).value
        user_info['gender'] = ws.cell(row=2, column=8).value
        start_row, step, p_col = 2, 3, 1

    all_scored_items = []
    for row in range(start_row, ws.max_row + 1, step):
        p_name = ws.cell(row=row, column=p_col).value
        score_val = ws.cell(row=row, column=10).value
        if p_name and score_val is not None:
            try:
                all_scored_items.append({"name": str(p_name).strip(), "score": float(score_val)})
            except: continue

    # 階層式篩選
    tier_1 = [item['name'] for item in all_scored_items if item['score'] < threshold_low]
    if tier_1:
        return user_info, tier_1, "極低分 (<30)"
    
    tier_2 = [item['name'] for item in all_scored_items if item['score'] < threshold_std]
    return user_info, tier_2, "標準篩選 (<37)"

# --- 2. 格式化工具 ---
def format_output(content):
    if isinstance(content, list):
        lines = []
        for idx, entry in enumerate(content, 1):
            if isinstance(entry, dict):
                val_str = " ".join([str(v) for v in entry.values()])
                lines.append(f"{idx}. {val_str}")
            else:
                lines.append(f"{idx}. {entry}")
        return "\n".join(lines)
    return str(content).strip()

# --- 3. Streamlit 網頁介面 ---
st.set_page_config(page_title="AI 營養報告生成器", layout="wide")
st.title("🧬 印度AI 細胞解碼報告生成器")

with st.sidebar:
    st.header("⚙️ 參數設定")
    # API Key 優先讀取 Secrets，若無則顯示輸入框
    api_key_val = os.getenv("GEMINI_API_KEY", "")
    api_key = st.text_input("Gemini API Key", type="password", value=api_key_val)
    lang = st.selectbox("輸出語言", ["繁體中文", "English", "日本語", "한국어", "Tiếng Việt"], index=0)
    word_limit = st.number_input("每個項目的字數限制", value=800)

# 【修改點 1】：移除提示詞上傳區，僅保留 Excel 上傳
up_excel = st.file_uploader("上傳檢測 Excel 檔案", type=["xlsx"])

# 固定設定：Google Sheet 與提示詞檔
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1JDaap1KOnKn4ZefISp27edfW1nWJyf4EFWWrd4dxVdU/edit?resourcekey=&gid=1866179831#gid=1866179831"
GOOGLE_SHEET_WORKSHEET = ""
GOOGLE_SHEET_GID = 1866179831
PROMPT_FILE_NAME = "系統提示詞_v3.1_純文字.txt"

if st.button("🚀 開始分析報告") and up_excel and api_key:
    # 檢查提示詞檔案是否存在
    if not os.path.exists(PROMPT_FILE_NAME):
        st.error(f"❌ 找不到設定檔：{PROMPT_FILE_NAME}。請確認檔案已上傳至 GitHub。")
    else:
        try:
            client = genai.Client(api_key=api_key)
            
            # 【修改點 3】：自動讀取本地檔案中的提示詞
            with open(PROMPT_FILE_NAME, "r", encoding="utf-8") as f:
                bg_prompt = f.read()
        
            with st.spinner("正在逐項分析中，請稍候..."):
                user_info, items, mode = extract_data_from_upload(up_excel)

                # 解析申請單編號（檔名格式不符時給出警告，繼續執行）
                try:
                    application_id = parse_application_id(up_excel.name)
                except ValueError as e:
                    application_id = ""
                    st.warning(f"⚠️ 無法從檔名解析申請單編號：{e}（病史將顯示為未提供）")

                # 從 Google Sheet 讀取資料
                records = load_records_from_google_sheet(GOOGLE_SHEET_URL, GOOGLE_SHEET_WORKSHEET or None, GOOGLE_SHEET_GID)

                # ===== 診斷輸出（debug，確認後可移除）=====
                st.write(f"🔍 DEBUG: 共讀取 {len(records)} 筆記錄")
                if records:
                    st.write(f"🔍 DEBUG: 欄位名稱 = {list(records[0].keys())}")
                # ===== 診斷輸出結束 =====

                # 找對應資料列（找不到時顯示警告，繼續執行）
                matched_row = find_row_by_application_id(records, application_id)

                # ===== 診斷輸出（debug，確認後可移除）=====
                st.write(f"🔍 DEBUG: matched_row = {'找到了' if matched_row else 'None'}")
                if matched_row:
                    st.write(f"🔍 DEBUG: matched_row keys = {list(matched_row.keys())}")
                # ===== 診斷輸出結束 =====

                if matched_row is None and application_id:
                    st.warning(f"⚠️ Google Sheet 中找不到申請單編號：{application_id}（病史將顯示為未提供）")

                personal_history, family_history = extract_medical_histories(matched_row)
                lifestyle_habits = extract_lifestyle_habits(matched_row)

                smoking_status = lifestyle_habits.get("smoking", "")
                drinking_status = lifestyle_habits.get("drinking", "")
                betel_nut_status = lifestyle_habits.get("betel_nut", "")

                # ===== 診斷輸出（debug，確認後可移除）=====
                st.write(f"🔍 DEBUG: personal_history = '{personal_history}'")
                st.write(f"🔍 DEBUG: family_history = '{family_history}'")
                # ===== 診斷輸出結束 =====

                personal_history = personal_history or "未提供"
                family_history = family_history or ""
                smoking_status = smoking_status or ""
                drinking_status = drinking_status or ""
                betel_nut_status = betel_nut_status or ""
                has_family_history = bool(family_history)
                st.caption(f"檔名：{up_excel.name}｜申請單編號：{application_id or '（無法解析）'}")
                st.caption(f"Google Sheet：{GOOGLE_SHEET_URL}")
                habit_display_parts = []
                if smoking_status:
                    habit_display_parts.append(f"抽菸：{smoking_status}")
                if drinking_status:
                    habit_display_parts.append(f"喝酒：{drinking_status}")
                if betel_nut_status:
                    habit_display_parts.append(f"吃檳榔：{betel_nut_status}")
                habit_display = "｜".join(habit_display_parts) if habit_display_parts else "（未提供）"
                family_display = family_history if has_family_history else "（不參考）"
                st.info(f"個人疾病史：{personal_history}｜家族疾病史：{family_display}｜生活習慣：{habit_display}")

                if not items:
                    st.warning("該檔案中無符合篩選條件的低分項目。")
                else:
                    st.info(f"偵測模式：{mode} | 項目總數：{len(items)}")
                
                final_text = ""
                progress_bar = st.progress(0)
                live_result_container = st.container()
                HEADERS = {
                    "繁體中文": {
                        "intro": "您的檢測結果【{item}】預防評分為低分。",
                        "maintenance": "■ 細胞維護：",
                        "tracking": "■ 主要追蹤項目：",
                        "nutrition": "■ 細胞營養：",
                        "supplements": "■ 功能性營養群建議：",
                        "lifestyle": "■ 生活策略小提醒：",
                    },
                    "English": {
                        "intro": "Your result for 【{item}】 is a low prevention score.",
                        "maintenance": "■ Cellular maintenance:",
                        "tracking": "■ Key tracking labs:",
                        "nutrition": "■ Cellular nutrition:",
                        "supplements": "■ Functional nutrients & supplements:",
                        "lifestyle": "■ Lifestyle tips:",
                    },
                    "日本語": {
                        "intro": "検査結果【{item}】は低スコアです。",
                        "maintenance": "■ 細胞メンテナンス：",
                        "tracking": "■ 追跡すべき検査項目：",
                        "nutrition": "■ 細胞栄養：",
                        "supplements": "■ 栄養補助（サプリ）提案：",
                        "lifestyle": "■ 生活習慣のヒント：",
                    },
                    "한국어": {
                        "intro": "검사 결과【{item}】의 예방 점수가 낮습니다.",
                        "maintenance": "■ 세포 유지:",
                        "tracking": "■ 주요 추적 항목:",
                        "nutrition": "■ 세포 영양:",
                        "supplements": "■ 기능성 영양소/보충제 제안:",
                        "lifestyle": "■ 생활 전략 팁:",
                    },
                    "Tiếng Việt": {
                        "intro": "Kết quả kiểm tra【{item}】có điểm phòng ngừa thấp.",
                        "maintenance": "■ Duy trì tế bào:",
                        "tracking": "■ Các chỉ số cần theo dõi:",
                        "nutrition": "■ Dinh dưỡng tế bào:",
                        "supplements": "■ Gợi ý dưỡng chất/bổ sung:",
                        "lifestyle": "■ Mẹo lối sống:",
                    },
                }
                H = HEADERS.get(lang, HEADERS["繁體中文"])

                # 【強效機制】：手動定義關鍵主題與基因的對應關係，避免 AI 混淆
                CRITICAL_GENE_MAPPING = {
                    "胃癌": "MTHFR",
                    "大腸直腸癌": "MTHFR",
                    "卵巢癌": "MTHFR",
                    "前列腺癌": "MTHFR",
                    "頭頸癌": "CYP1A1",
                    "肝癌": "CYP1A1",
                    "肺癌": "EGF",
                    "乳癌": "BRCA1",
                    "子宮內膜癌": "MDM2",
                    "胰臟癌": "TERT",
                    "肝臟解毒": "NAT2",
                }

                # 特定主題的機制防呆
                TOPIC_MECHANISM_RULES = {
                    "胃癌": "【強制機制要求】：必須且只能討論「葉酸代謝、DNA 甲基化、黏膜修復」，嚴禁提及「肝臟解毒」、「CYP1A1」、「致癌物代謝」。",
                    "頭頸癌": "【強制機制要求】：必須且只能討論「黏膜防禦、局部炎症、DNA 穩定性」，嚴禁提及「解毒能力」。",
                    "大腸直腸癌": "【強制機制要求】：必須聚焦「葉酸代謝、DNA 甲基化、腸道黏膜修復」。",
                }

                # 特定主題的追蹤項目防呆
                TRACKING_TESTS_MAPPING = {
                    "胃癌": "【強制追蹤項目】：必須建議追蹤 H. Pylori Ab, CEA, CA-724 (若列表有)。",
                    "腎臟功能": "【強制追蹤項目】：必須建議追蹤 BUN, Creatinine, eGFR, UA。",
                    "肝臟解毒": "【強制追蹤項目】：必須建議追蹤 sGOT, sGPT, r-GTP, Alk-P, T-Bilirubin, D-Bilirubin。",
                    "肝癌": "【強制追蹤項目】：必須建議追蹤 AFP, sGOT, sGPT。",
                    "肺癌": "【強制追蹤項目】：必須建議追蹤 cyfra 21-1, NSE, SCC, CEA。",
                    "大腸直腸癌": "【強制追蹤項目】：必須建議追蹤 CEA。",
                    "乳癌": "【強制追蹤項目】：必須建議追蹤 CA-153, CEA。",
                    "卵巢癌": "【強制追蹤項目】：必須建議追蹤 CA-125, CEA。",
                    "前列腺癌": "【強制追蹤項目】：必須建議追蹤 PSA。",
                    "胰臟癌": "【強制追蹤項目】：必須建議追蹤 CA-199, CEA。",
                    "頭頸癌": "【強制追蹤項目】：必須建議追蹤 SCC, EBVCA-IgA。",
                    "中風": "【強制追蹤項目】：必須建議追蹤 Cholesterol, LDL-Cho, HDL-Cho, Triglyceride, HsCRP, Homocysteine。",
                    "心肌梗塞": "【強制追蹤項目】：必須建議追蹤 CPK, LDH, HsCRP, Homocysteine, LDL-Cho。",
                    "糖尿病預防": "【強制追蹤項目】：必須建議追蹤 Glucose(Fasting/2hrPC), HbA1c。",
                    "脂質代謝能力": "【強制追蹤項目】：必須建議追蹤 Cholesterol, LDL-Cho, HDL-Cho, Triglyceride。",
                    "細胞炎症調控": "【強制追蹤項目】：必須建議追蹤 CRP, HsCRP, WBC。",
                }

                # 核心：將 AI 呼叫移入迴圈內，確保每一項都分析到
                for index, item in enumerate(items):
                    st.write(f"正在分析第 {index+1}/{len(items)} 項：{item}...")
                    
                    # 獲取手動指定的基因（如果有）
                    manual_gene = CRITICAL_GENE_MAPPING.get(item, "")
                    gene_instruction = f"本項目對應的主要基因必須為：{manual_gene}。" if manual_gene else "請依據提示詞中的對應表選取正確基因。"
                    gene_instruction_en = f"The primary gene for this topic MUST be: {manual_gene}." if manual_gene else "Select the correct gene based on the mapping table in the system prompt."

                    pdf_tests = "RBC, Hgb, Hct, MCV, MCH, MCHC, Platelet, WBC, Neutrophil, Lymphocyte, Monocyte, Eosinophil, Basophil, Cholesterol, HDL-Cho, LDL-Cho, Triglyceride, Glucose(Fasting/2hrPC), HbA1c, T-Bilirubin, D-Bilirubin, Total Protein, Albumin, Globulin, sGOT, sGPT, Alk-P, r-GTP, BUN, Creatinine, UA, eGFR, AFP, CEA, CA-199, CA-125, CA-153, PSA, CA-724, NSE, cyfra 21-1, SCC, LDH, CPK, HsCRP, Homocysteine, T4, T3, TSH, Free T4, Na, K, Cl, Ca, Phosphorus, EBVCA-IgA, RA, CRP, H. Pylori Ab"
                    generation_limit = max(1, int(word_limit))
                    target_min = min_total_length(generation_limit)
                    length_unit = "words" if lang == "English" else "non-space characters"
                    budget_hint = format_budget_hint(build_length_budget(generation_limit))
                    section_min = min_section_length(word_limit)
                    
                    family_history_instruction_zh = (
                        f"家族疾病史：{family_history}。" if has_family_history else "家族疾病史：不參考。"
                    )
                    family_history_instruction_en = (
                        f"- Family Medical History: {family_history}" if has_family_history else "- Family Medical History: N/A (do not reference family history)"
                    )

                    habit_lines_zh = []
                    habit_lines_en = []
                    has_bad_habit = False

                    if smoking_status and smoking_status not in ["無", "未提供", "否"]:
                        habit_lines_zh.append(f"抽菸問卷結果：{smoking_status}。")
                        habit_lines_en.append(f"- Smoking questionnaire: {smoking_status}")
                        has_bad_habit = True
                    if drinking_status and drinking_status not in ["無", "未提供", "否"]:
                        habit_lines_zh.append(f"喝酒問卷結果：{drinking_status}。")
                        habit_lines_en.append(f"- Alcohol questionnaire: {drinking_status}")
                        has_bad_habit = True
                    if betel_nut_status and betel_nut_status not in ["無", "未提供", "否"]:
                        habit_lines_zh.append(f"吃檳榔問卷結果：{betel_nut_status}。")
                        habit_lines_en.append(f"- Betel nut questionnaire: {betel_nut_status}")
                        has_bad_habit = True

                    if not has_bad_habit:
                        habit_instruction_zh = "【生活習慣設定】：此受測者「沒有」或未提供抽菸/喝酒/吃檳榔的習慣。絕對嚴禁在報告中出現「如果您有抽菸/喝酒/嚼檳榔習慣請戒除」、「避免抽菸/喝酒以降低風險」、「避免暴露於二手菸環境」等假設性語句。請將生活建議完全聚焦於「主動的飲食、運動、睡眠行為」。"
                        habit_instruction_en = "- Lifestyle Habits: The subject DOES NOT smoke, DOES NOT drink, and DOES NOT chew betel nut. You MUST NOT advise them to quit or reduce smoking/drinking/betel nut, and MUST NOT advise them to avoid second-hand smoke. Please focus entirely on proactive diet, exercise, and sleep habits."
                    else:
                        habit_instruction_zh = "\n                    ".join(habit_lines_zh)
                        habit_instruction_en = "\n                    ".join(habit_lines_en)

                    smoking_prompt_value = smoking_status if (smoking_status and smoking_status not in ["無", "未提供", "否"]) else "N/A"
                    drinking_prompt_value = drinking_status if (drinking_status and drinking_status not in ["無", "未提供", "否"]) else "N/A"
                    betel_prompt_value = betel_nut_status if (betel_nut_status and betel_nut_status not in ["無", "未提供", "否"]) else "N/A"
                    
                    # 機制防呆注入
                    mechanism_override = TOPIC_MECHANISM_RULES.get(item, "")
                    tracking_override = TRACKING_TESTS_MAPPING.get(item, "")

                    core_prompt = f"""
                    # CRITICAL REQUIREMENTS
                    - RESPOND EXCLUSIVELY IN: {lang} (NO CHINESE if English)
                    - TONE: Warm, clinical. Use "您" (You) strictly. NEVER use "受測者".
                    
                    # SUBJECT DATA
                    - Gender/Age: {user_info.get('gender')}/{user_info.get('age')}, ID: {application_id}
                    - Medical/Family: {personal_history} | {family_history_instruction_en}
                    - Habits: {smoking_prompt_value} (Smoke), {drinking_prompt_value} (Drink), {betel_prompt_value} (Betel)
                    {habit_instruction_en}
                    
                    # TARGET
                    - Item: {item}
                    - Gene (FORCED): {manual_gene if manual_gene else "Use prompt table"}
                    - Override: {mechanism_override}
                    
                    # CONSTRAINTS
                    - Goal Range: {target_min}~{generation_limit} {length_unit} (target this range, do not be brief)
                    - Section Limits: {budget_hint} (Min. {section_min} / section, >=2 sentences)
                    - Track Labs: Pick from [{pdf_tests}]. MUST INCLUDE: {tracking_override}
                    
                    # LIFESTYLE RULES
                    1. 4-6 highly detailed, strictly quantifiable proactive tips ("30 min aerobic 130bpm 3x/week", "sleep 7-8 hrs 11PM-7AM").
                    2. PROHIBITED: Vague fluff (meditation, relax, stress focus) OR avoidance of irrelevant passive risks (second-hand smoke/pollution, unless they actually smoke).
                    3. Ensure tips combat {item} mechanisms specifically. Do NOT contradict metrics across tips (e.g. pick ONE water target).

                    Please output ONLY valid JSON format:
                    {{
                      "maintenance": "...",
                      "tracking": "...",
                      "nutrition": "...",
                      "supplements": "...",
                      "lifestyle": "..."
                    }}
                    """

                    system_prompt = bg_prompt + "\n\n" + build_language_system_rule(lang, generation_limit)
                    full_combined_prompt = f"{system_prompt}\n\n{core_prompt}"
                    
                    report = None
                    best_short_report = None
                    best_short_length = 0
                    failure_reason = ""
                    output_length = 0
                    for attempt in range(3):
                        if attempt > 0:
                            if output_length > word_limit:
                                shrink_by = max(10, output_length - word_limit)
                                generation_limit = max(1, generation_limit - shrink_by)
                            target_min = min_total_length(generation_limit)
                            budget_hint = format_budget_hint(build_length_budget(generation_limit))
                            section_min = min_section_length(word_limit)
                            system_prompt = bg_prompt + "\n\n" + build_language_system_rule(lang, generation_limit)
                            
                            core_prompt_retry = f"""
                            # RETRY - REDUCE LENGTH & OBEY CONSTRAINTS
                            - Item: {item}
                            - Limits: {target_min}~{generation_limit} {length_unit}, budgets: {budget_hint}, min {section_min}/section.
                            - Lang: {lang}
                            - Target Gene: {manual_gene} | Override: {mechanism_override}
                            - If previous response was too short, expand each section with more clinical detail.
                            - Must use valid JSON format.
                            """
                            full_combined_prompt = f"{system_prompt}\n\n{core_prompt}\n{core_prompt_retry}"
                            full_combined_prompt += (
                                f"\n\n# RETRY NOTICE\n"
                                f"The previous response was invalid: {failure_reason}.\n"
                                f"Please respond again strictly in {lang} and within the target range.\n"
                            )
                        response = client.models.generate_content(
                            model="models/gemma-3-27b-it",
                            contents=full_combined_prompt,
                            config={
                                "temperature": 0.3,
                                "top_p": 0.95,
                            }
                        )

                        json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
                        if not json_match:
                            failure_reason = "未回傳有效 JSON"
                            continue

                        candidate_report = json.loads(json_match.group(0))
                        valid, failure_reason, output_length = validate_report_output(candidate_report, lang, word_limit, strict_length=False)
                        if valid:
                            report = candidate_report
                            break
                        if failure_reason.startswith("總字數過短") and output_length > best_short_length:
                            best_short_report = candidate_report
                            best_short_length = output_length

                    if report is None and best_short_report is not None:
                        report = best_short_report
                        output_length = best_short_length
                        failure_reason = f"未達目標字數下限，已採用最佳結果（{best_short_length}/{word_limit}）"
                        st.warning(f"第 {index+1} 項未達目標字數，已採用最佳可用結果。")

                    if report:
                        report, adjusted_length = enforce_report_length(report, word_limit, lang)
                        section = H["intro"].format(item=item) + "\n\n"
                        section += f'{H["maintenance"]}\n{format_output(report.get("maintenance"))}\n\n'
                        section += f'{H["tracking"]}\n{format_output(report.get("tracking"))}\n\n'
                        section += f'{H["nutrition"]}\n{format_output(report.get("nutrition"))}\n\n'
                        section += f'{H["supplements"]}\n{format_output(report.get("supplements"))}\n\n'
                        section += f'{H["lifestyle"]}\n{format_output(report.get("lifestyle"))}\n\n'
                        final_text += section + "="*50 + "\n\n"
                        with live_result_container:
                            st.markdown(f"### ✅ 第 {index+1}/{len(items)} 項完成：{item}")
                            st.text(section)
                        if output_length > word_limit:
                            st.info(
                                f"第 {index+1} 項原始字數 {output_length} 超過限制 {word_limit}，"
                                f"已自動壓縮至約 {adjusted_length} 字。"
                            )
                    else:
                        st.warning(f"第 {index+1} 項分析失敗：{failure_reason}")
                    
                    progress_bar.progress((index + 1) / len(items))
                    if len(items) > 1:
                        time.sleep(15) # 避免頻率限制

                st.success("🎉 分析完成！")
                st.text_area("結果預覽", final_text, height=400)
                st.download_button("📥 下載報告", final_text, file_name="分析報告.txt")

        except Exception as e:
            st.error(f"分析失敗：{e}")
