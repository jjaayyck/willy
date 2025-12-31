import streamlit as st
import os
import openpyxl
import json
import re
from pathlib import Path
from google import genai
from dotenv import load_dotenv

# 載入環境變數
load_dotenv()

# --- 核心邏輯：擷取 Excel 數據 (略作修改以適應 Streamlit 上傳對象) ---
def extract_data_from_upload(uploaded_file, threshold_low=30, threshold_std=37):
    # Streamlit 上傳的檔案是 BytesIO 物件
    wb = openpyxl.load_workbook(uploaded_file, data_only=True)
    ws = wb.active
    
    # 版型判定 [cite: 14]
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

    # 階層式篩選 [cite: 16]
    tier_1 = [item['name'] for item in all_scored_items if item['score'] < threshold_low]
    if tier_1:
        return user_info, tier_1, "極低分 (<30)"
    else:
        tier_2 = [item['name'] for item in all_scored_items if item['score'] < threshold_std]
        return user_info, tier_2, "標準篩選 (<37)"

# --- 格式化工具 [cite: 26, 30, 31] ---
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

# --- Streamlit 網頁介面 ---
st.set_page_config(page_title="AI 細胞解碼報告生成器", layout="centered")
st.title("🧬 AI 細胞解碼報告生成器")
st.write("上傳 Excel 檔案，自動生成結構化專業分析報告。")

# 側邊欄配置
with st.sidebar:
    st.header("⚙️ 設定")
    api_key = st.text_input("輸入 Gemini API Key", type="password", value=os.getenv("GEMINI_API_KEY", ""))
    lang = st.selectbox("報告語言", ["繁體中文", "English", "日本語"], index=0)
    word_limit = st.slider("字數限制", 300, 1500, 800)
    
# 上傳區
uploaded_file = st.file_uploader("選擇 Excel 檔案 (.xlsx)", type=["xlsx"])
prompt_file = st.file_uploader("上傳系統提示詞檔案 (.txt)", type=["txt"])

if st.button("🚀 開始分析") and uploaded_file and prompt_file and api_key:
    try:
        client = genai.Client(api_key=api_key)
        bg_prompt = prompt_file.read().decode("utf-8")
        
        with st.spinner("正在讀取數據與分析中..."):
            user_info, items, mode = extract_data_from_upload(uploaded_file)
            
            if not items:
                st.warning("該檔案中無符合篩選條件的低分項目。")
            else:
                st.info(f"偵測模式：{mode}，受試者：{user_info.get('gender')}/{user_info.get('age')}歲")
                
                # 準備 AI 指令 [cite: 23]
                items_str = "、".join(items)
                pdf_available_tests = "RBC, Hgb, Hct, MCV, MCH, MCHC, Platelet, WBC, Neutrophil, Lymphocyte, Monocyte, Eosinophil, Basophil, Cholesterol, HDL-Cho, LDL-Cho, Triglyceride, Glucose(Fasting/2hrPC), HbA1c, T-Bilirubin, D-Bilirubin, Total Protein, Albumin, Globulin, sGOT, sGPT, Alk-P, r-GTP, BUN, Creatinine, UA, eGFR, AFP, CEA, CA-199, CA-125, CA-153, PSA, CA-724, NSE, cyfra 21-1, SCC, LDH, CPK, HsCRP, Homocysteine, T4, T3, TSH, Free T4, Na, K, Cl, Ca, Phosphorus, EBVCA-IgA, RA, CRP, H. Pylori Ab"
                
                user_instruction = f"""
                受試者資料：{user_info.get('gender')}/{user_info.get('age')}歲。請使用【{lang}】回覆。
                針對項目分析：{items_str}。總字數控制在 {word_limit} 字以內。
                【追蹤項目約束】：僅限從清單挑選：[{pdf_available_tests}]。
                請嚴格以 JSON 格式回傳，Key 包含 maintenance, tracking, nutrition, supplements, lifestyle。
                """
                
                final_prompt = f"{bg_prompt}\n\n{user_instruction}"
                
                # 呼叫 AI (使用 gemma-3-12b-it) [cite: 17, 18]
                response = client.models.generate_content(
                    model="models/gemma-3-12b-it", 
                    contents=final_prompt,
                    config={"temperature": 0.1}
                )
                
                # 解析 JSON [cite: 19]
                json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
                report = json.loads(json_match.group(0)) if json_match else json.loads(response.text)
                
                # 後製排版並顯示結果 [cite: 25, 31]
                # --- 強大容錯版的後製排版  ---
                final_text = ""
                
                # 判定 AI 是否直接回傳內容 (跳過了項目名稱層級)
                is_direct = any(k in report for k in ["maintenance", "nutrition", "lifestyle"])

                if is_direct:
                    # 處理直接結構 (例如：{"maintenance": "...", ...})
                    display_name = items[0] if items else "檢測項目"
                    data = report
                    section = f"您的檢測結果【{display_name}】預防評分為低分。\n\n"
                    section += f"■ 細胞維護：\n{format_output(data.get('maintenance'))}\n\n"
                    section += f"■ 主要追蹤項目：\n{format_output(data.get('tracking'))}\n\n"
                    section += f"■ 細胞營養：\n{format_output(data.get('nutrition'))}\n\n"
                    section += f"■ 功能性營養群建議：\n{format_output(data.get('supplements'))}\n\n"
                    section += f"■ 生活策略小提醒：\n{format_output(data.get('lifestyle'))}\n\n"
                    final_text = section
                else:
                    # 處理嵌套結構 (原本的邏輯)
                    for item_name, data in report.items():
                        if isinstance(data, dict):
                            section = f"您的檢測結果【{item_name}】預防評分為低分。\n\n"
                            section += f"■ 細胞維護：\n{format_output(data.get('maintenance'))}\n\n"
                            section += f"■ 主要追蹤項目：\n{format_output(data.get('tracking'))}\n\n"
                            section += f"■ 細胞營養：\n{format_output(data.get('nutrition'))}\n\n"
                            section += f"■ 功能性營養群建議：\n{format_output(data.get('supplements'))}\n\n"
                            section += f"■ 生活策略小提醒：\n{format_output(data.get('lifestyle'))}\n\n"
                            section += "="*50 + "\n\n"
                            final_text += section
                
                st.success("分析完成！")
                st.text_area("預覽結果", final_text, height=400)
                
                # 提供下載 [cite: 32]
                st.download_button(
                    label="📥 下載文字報告 (.txt)",
                    data=final_text,
                    file_name=f"{uploaded_file.name.split('.')[0]}_報告.txt",
                    mime="text/plain"
                )
                
    except Exception as e:
        st.error(f"分析過程中發生錯誤：{e}")
else:
    if not (uploaded_file and prompt_file and api_key):
        st.info("請上傳檔案並確保設定已完成，然後點擊「開始分析」。")
