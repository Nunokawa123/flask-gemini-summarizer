from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import fitz  # PyMuPDF
import tempfile
import traceback
from datetime import datetime
from fpdf import FPDF
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account
import json

app = Flask(__name__)
CORS(app)

# 環境変数
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
KINTONE_DOMAIN = "https://nunokawa.cybozu.com"
API_TOKEN = os.environ.get("API_TOKEN")
APP_ID = 563
FIELD_CODE_ATTACHMENT = "添付ファイル"
FIELD_CODE_SUMMARY = "要約文章"
FIELD_CODE_ORIGINAL_LINK = "原本リンク"
FIELD_CODE_SUMMARY_LINK = "要約リンク"
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
PORT = int(os.environ.get("PORT", 10000))

# -------------------------------
# kintoneからPDF取得
# -------------------------------
def fetch_pdf_from_kintone(record_id):
    headers = {"X-Cybozu-API-Token": API_TOKEN}
    params = {"app": APP_ID, "id": record_id}
    res = requests.get(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, params=params)
    record_data = res.json().get("record", {})
    file_info = record_data[FIELD_CODE_ATTACHMENT]["value"][0]
    file_key = file_info["fileKey"]
    file_name = file_info["name"]

    res_file = requests.get(f"{KINTONE_DOMAIN}/k/v1/file.json", headers=headers, params={"fileKey": file_key})
    temp_path = os.path.join(tempfile.gettempdir(), file_name)
    with open(temp_path, "wb") as f:
        f.write(res_file.content)
    return temp_path, file_name

# -------------------------------
# Google Driveアップロード
# -------------------------------
def upload_to_drive_and_get_link(local_path, file_name, folder_id):
    creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON)
    service = build("drive", "v3", credentials=creds)
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype="application/pdf")
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    service.permissions().create(fileId=uploaded["id"], body={"role": "reader", "type": "anyone"}).execute()
    return f"https://drive.google.com/file/d/{uploaded['id']}/view?usp=sharing"

# -------------------------------
# テキスト抽出
# -------------------------------
def extract_text_from_pdf(file_path):
    doc = fitz.open(file_path)
    text = ""
    for page in doc:
        text += page.get_text()
    return text

# -------------------------------
# Geminiで要約
# -------------------------------
def gemini_summarize(text, prompt="以下を要約してください："):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": f"{prompt}\n\n{text}"}]}]}
    res = requests.post(url, json=payload)
    try:
        gemini = res.json()
        return gemini.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "⚠ 要約できませんでした")
    except Exception:
        return "⚠ Geminiからの要約に失敗しました"

# -------------------------------
# 要約PDF作成
# -------------------------------
def create_summary_pdf(text, title_base):
    date_str = datetime.now().strftime("%Y%m%d")
    file_name = f"要約_{title_base}_{date_str}.pdf"
    pdf_path = os.path.join(tempfile.gettempdir(), file_name)
    font_path = "./fonts/mplus-1p-regular.ttf"

    class SummaryPDF(FPDF):
        def header(self):
            self.set_font("Mplus", "", 10)
            self.cell(0, 10, datetime.now().strftime("%Y-%m-%d"), ln=True, align='R')
            self.ln(5)

        def footer(self):
            self.set_y(-15)
            self.set_font("Mplus", "", 8)
            self.cell(0, 10, f"Page {self.page_no()}", align='C')

        def body(self, text):
            self.set_font("Mplus", "", 12)
            for line in text.split('\n'):
                self.multi_cell(0, 10, line)
                self.ln(2)

    pdf = SummaryPDF()
    pdf.add_font("Mplus", "", font_path, uni=True)
    pdf.add_page()
    pdf.body(text)
    pdf.output(pdf_path)
    return pdf_path, file_name

# -------------------------------
# kintone書き戻し
# -------------------------------
def write_back_to_kintone(record_id, field_code, value):
    headers = {"X-Cybozu-API-Token": API_TOKEN, "Content-Type": "application/json"}
    body = {"app": APP_ID, "id": record_id, "record": {field_code: {"value": value}}}
    res = requests.put(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, json=body)
    return res.status_code, res.text

# -------------------------------
# メイン処理
# -------------------------------
@app.route("/", methods=["POST"])
def summarize():
    try:
        data = request.json
        record_id = data.get("recordId")
        prompt = data.get("prompt", "以下を要約してください：")

        # ステップ1：PDF取得
        pdf_path, file_name = fetch_pdf_from_kintone(record_id)
        title_base = os.path.splitext(file_name)[0].replace(" ", "_")

        # ステップ2：原本をDriveに保存
        original_link = upload_to_drive_and_get_link(pdf_path, file_name, DRIVE_FOLDER_ID)
        write_back_to_kintone(record_id, FIELD_CODE_ORIGINAL_LINK, original_link)

        # ステップ3：要約処理
        text = extract_text_from_pdf(pdf_path)
        summary = gemini_summarize(text, prompt)
        write_back_to_kintone(record_id, FIELD_CODE_SUMMARY, summary)

        # ステップ4：要約PDF作成
        summary_pdf_path, summary_pdf_name = create_summary_pdf(summary, title_base)

        # ステップ5：要約PDFをDriveに保存
        summary_link = upload_to_drive_and_get_link(summary_pdf_path, summary_pdf_name, DRIVE_FOLDER_ID)
        write_back_to_kintone(record_id, FIELD_CODE_SUMMARY_LINK, summary_link)

        return jsonify({
            "summary": summary,
            "original_link": original_link,
            "summary_pdf_link": summary_link
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
