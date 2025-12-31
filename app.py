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
        
        with st.spinner("正在逐項分析中，請稍候..."):
            user_info, items, mode = extract_data_from_upload(uploaded_file)
            
            # --- 檢查 Excel 數值是否抓取失敗 ---
            if items is None or (len(items) == 0 and mode != "無符合項目"):
                st.error("❌ 偵測不到分數。請確認 Excel 已在您的電腦『存檔』過，以確保公式數值已寫入檔案。")
            elif not items:
                st.warning("該檔案中無符合篩選條件的低分項目。")
            else:
                st.info(f"偵測模式：{mode} | 項目總數：{len(items)}")
                
                final_text = ""
                progress_bar = st.progress(0)

                # --- 修改點：將 AI 呼叫移入迴圈內 ---
                for index, item in enumerate(items):
                    st.write(f"正在分析第 {index+1}/{len(items)} 項：{item}...")
                    
                    pdf_tests = "RBC, Hgb, Hct, MCV, MCH, MCHC, Platelet, WBC, Neutrophil, Lymphocyte, Monocyte, Eosinophil, Basophil, Cholesterol, HDL-Cho, LDL-Cho, Triglyceride, Glucose(Fasting/2hrPC), HbA1c, T-Bilirubin, D-Bilirubin, Total Protein, Albumin, Globulin, sGOT, sGPT, Alk-P, r-GTP, BUN, Creatinine, UA, eGFR, AFP, CEA, CA-199, CA-125, CA-153, PSA, CA-724, NSE, cyfra 21-1, SCC, LDH, CPK, HsCRP, Homocysteine, T4, T3, TSH, Free T4, Na, K, Cl, Ca, Phosphorus, EBVCA-IgA, RA, CRP, H. Pylori Ab"
                    
                    user_instruction = f"""
                    受試者：{user_info.get('gender')}/{user_info.get('age')}歲。使用【{lang}】。
                    分析項目：{item}。字數控制在 {word_limit} 字以內。
                    【追蹤項目】：僅限挑選：[{pdf_tests}]。
                    請嚴格以 JSON 回傳該項目的分析（不要包含其他文字）：
                    {{
                      "maintenance": "內容...",
                      "tracking": "內容...",
                      "nutrition": "內容...",
                      "supplements": "內容...",
                      "lifestyle": "內容..."
                    }}
                    """
                    
                    # 執行 AI 呼叫 (確保每次迴圈都跑一次)
                    response = client.models.generate_content(
                        model="models/gemma-3-12b-it", 
                        contents=f"{bg_prompt}\n\n{user_instruction}",
                        config={"temperature": 0.1}
                    )
                    
                    # 解析該項目的 JSON
                    json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
                    if json_match:
                        report = json.loads(json_match.group(0))
                        
                        # 格式化輸出
                        section = f"您的檢測結果【{item}】預防評分為低分。\n\n"
                        section += f"■ 細胞維護：\n{format_output(report.get('maintenance'))}\n\n"
                        section += f"■ 主要追蹤項目：\n{format_output(report.get('tracking'))}\n\n"
                        section += f"■ 細胞營養：\n{format_output(report.get('nutrition'))}\n\n"
                        section += f"■ 功能性營養群建議：\n{format_output(report.get('supplements'))}\n\n"
                        section += f"■ 生活策略小提醒：\n{format_output(report.get('lifestyle'))}\n\n"
                        final_text += section + "="*50 + "\n\n"
                    
                    # 進度更新與間隔避免 API 被鎖
                    progress_bar.progress((index + 1) / len(items))
                    if len(items) > 1:
                        import time
                        time.sleep(5) 

                st.success("🎉 全部項目分析完成！")
                st.text_area("預覽結果", final_text, height=400)
                
                st.download_button(
                    label="📥 下載完整文字報告 (.txt)",
                    data=final_text,
                    file_name=f"{uploaded_file.name.split('.')[0]}_分析報告.txt",
                    mime="text/plain"
                )
                
    except Exception as e:
        st.error(f"分析過程中發生錯誤：{e}")
else:
    if not (uploaded_file and prompt_file and api_key):
        st.info("請上傳檔案並確保設定已完成，然後點擊「開始分析」。")

