import streamlit as st
import os
import openpyxl
import json
import re
import time
from pathlib import Path
from google import genai
from dotenv import load_dotenv

# 載入環境變數
load_dotenv()

def build_language_system_rule(lang: str, word_limit: int) -> str:
    return f"""
# LANGUAGE CONSTRAINT — ABSOLUTE RULE (HIGHEST PRIORITY)

The user has selected the output language: {lang}

You MUST write the ENTIRE response strictly in this language.
Any violation makes the response INVALID.
You MUST keep the total output within {word_limit} characters/words for the JSON values.

- If lang is "English":
  - Respond in English ONLY
  - DO NOT output any Chinese characters (no 中文/漢字)
- If lang is "繁體中文":
  - Respond in Traditional Chinese ONLY
- If lang is "日本語":
  - すべて日本語で回答してください

Return JSON ONLY. No extra text outside JSON.
""".strip()

def is_language_valid(text: str, lang: str) -> bool:
    if lang == "English":
        return not re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", text)
    if lang == "繁體中文":
        return not re.search(r"[\u3040-\u30ff]", text)
    if lang == "日本語":
        return bool(re.search(r"[\u3040-\u30ff]", text))
    return True

def count_output_length(text: str, lang: str) -> int:
    if lang == "English":
        return len(re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", text))
    return len(re.findall(r"\S", text))

def validate_report_output(report: dict, lang: str, word_limit: int) -> tuple[bool, str]:
    combined_text = " ".join(str(v) for v in report.values())
    if not is_language_valid(combined_text, lang):
        return False, "語言不符合選擇"
    length = count_output_length(combined_text, lang)
    if length > word_limit:
        return False, f"超過字數限制（{length}/{word_limit}）"
    return True, ""

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
                all_scored_items.append({"name": str(p_name), "score": float(score_val)})
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
    lang = st.selectbox("輸出語言", ["繁體中文", "English", "日本語"], index=0)
    word_limit = st.number_input("字數限制", value=800)

# 【修改點 1】：移除提示詞上傳區，僅保留 Excel 上傳
up_excel = st.file_uploader("上傳 Excel 檔案", type=["xlsx"])

# 【修改點 2】：設定固定的提示詞檔名 (請確保 GitHub 上的檔名與此完全一致)
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
                
                if not items:
                    st.warning("該檔案中無符合篩選條件的低分項目。")
                else:
                    st.info(f"偵測模式：{mode} | 項目總數：{len(items)}")
                    
                    final_text = ""
                    progress_bar = st.progress(0)
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
                    }
                    H = HEADERS.get(lang, HEADERS["繁體中文"])

                    # 核心：將 AI 呼叫移入迴圈內，確保每一項都分析到
                    for index, item in enumerate(items):
                        st.write(f"正在分析第 {index+1}/{len(items)} 項：{item}...")
                        
                        pdf_tests = "RBC, Hgb, Hct, MCV, MCH, MCHC, Platelet, WBC, Neutrophil, Lymphocyte, Monocyte, Eosinophil, Basophil, Cholesterol, HDL-Cho, LDL-Cho, Triglyceride, Glucose(Fasting/2hrPC), HbA1c, T-Bilirubin, D-Bilirubin, Total Protein, Albumin, Globulin, sGOT, sGPT, Alk-P, r-GTP, BUN, Creatinine, UA, eGFR, AFP, CEA, CA-199, CA-125, CA-153, PSA, CA-724, NSE, cyfra 21-1, SCC, LDH, CPK, HsCRP, Homocysteine, T4, T3, TSH, Free T4, Na, K, Cl, Ca, Phosphorus, EBVCA-IgA, RA, CRP, H. Pylori Ab"
                        
                        # 強化語言要求，確保 AI 看到
                        user_instruction = f"""
                        ### IMPORTANT LANGUAGE REQUIREMENT: 
                        All content in the JSON response MUST be written in {lang}. 
                        (目前的語言要求：{lang})

                        受試者資料：{user_info.get('gender')}/{user_info.get('age')}歲。
                        分析項目：{item}。
                        字數限制：{word_limit} 字。
                        【追蹤項目】：僅限挑選：[{pdf_tests}]。
                        
                        請嚴格回傳 JSON 格式：
                        {{
                          "maintenance": "...",
                          "tracking": "...",
                          "nutrition": "...",
                          "supplements": "...",
                          "lifestyle": "..."
                        }}
                        """
                        
                        task_prompt = f"""
                        # LANGUAGE CONSTRAINT (CRITICAL)
                        - YOU MUST RESPOND EXCLUSIVELY IN: {lang}
                        - IF {lang} IS "English", DO NOT USE ANY CHINESE CHARACTERS.
                        - IF {lang} IS "日本語", すべて日本語で回答してください。

                        # SUBJECT DATA
                        - Gender/Age: {user_info.get('gender')}/{user_info.get('age')}
                        - Target Item: {item}
                        - Word Limit: {word_limit}

                        # REFERENCE DATA (FOR TRACKING SECTION)
                        - Valid Tracking Items: [{pdf_tests}]

                        # RESPONSE FORMAT
                        Please provide the analysis strictly in the following JSON structure:
                        {{
                        "maintenance": "...",
                        "tracking": "...",
                        "nutrition": "...",
                        "supplements": "...",
                        "lifestyle": "..."
                        }}
                        """

                        # 2. 使用 system_instruction 分離角色與任務
                        system_prompt = bg_prompt + "\n\n" + build_language_system_rule(lang, word_limit)
                        full_combined_prompt = f"{system_prompt}\n\n{user_instruction}\n\n{task_prompt}"
                        report = None
                        failure_reason = ""
                        for attempt in range(2):
                            if attempt == 1:
                                full_combined_prompt += (
                                    f"\n\n# RETRY NOTICE\n"
                                    f"The previous response was invalid: {failure_reason}.\n"
                                    f"Please respond again strictly in {lang} and within the limit.\n"
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
                            valid, failure_reason = validate_report_output(candidate_report, lang, word_limit)
                            if valid:
                                report = candidate_report
                                break

                        if report:
                            section = H["intro"].format(item=item) + "\n\n"
                            section += f'{H["maintenance"]}\n{format_output(report.get("maintenance"))}\n\n'
                            section += f'{H["tracking"]}\n{format_output(report.get("tracking"))}\n\n'
                            section += f'{H["nutrition"]}\n{format_output(report.get("nutrition"))}\n\n'
                            section += f'{H["supplements"]}\n{format_output(report.get("supplements"))}\n\n'
                            section += f'{H["lifestyle"]}\n{format_output(report.get("lifestyle"))}\n\n'
                            final_text += section + "="*50 + "\n\n"
                        else:
                            st.warning(f"第 {index+1} 項分析失敗：{failure_reason}")
                        
                        progress_bar.progress((index + 1) / len(items))
                        if len(items) > 1:
                            time.sleep(5) # 避免頻率限制

                    st.success("🎉 分析完成！")
                    st.text_area("結果預覽", final_text, height=400)
                    st.download_button("📥 下載報告", final_text, file_name="分析報告.txt")

        except Exception as e:
            st.error(f"分析失敗：{e}")



