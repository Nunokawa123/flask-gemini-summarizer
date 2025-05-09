import os, tempfile, traceback, json, re
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
from fpdf import FPDF
import fitz
from pdf2image import convert_from_path
from PIL import Image
import pytesseract
import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account
from google.cloud import vision
import io

app = Flask(__name__)
CORS(app)

# 環境変数
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
KINTONE_DOMAIN = "https://nunokawa.cybozu.com"
API_TOKEN = os.environ.get("API_TOKEN")
APP_ID = 563
FIELD_ATTACHMENT = "添付ファイル"
FIELD_SUMMARY_TEXT = "要約文章"
FIELD_ORIGINAL_LINK = "原本リンク"
FIELD_SUMMARY_LINK = "要約リンク"
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
PORT = int(os.environ.get("PORT", 10000))

# PDF取得
def fetch_pdf_from_kintone(record_id):
    headers = {"X-Cybozu-API-Token": API_TOKEN}
    res = requests.get(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, params={"app": APP_ID, "id": record_id})
    record = res.json().get("record", {})
    file_info = record[FIELD_ATTACHMENT]["value"][0]
    file_key = file_info["fileKey"]
    file_name = file_info["name"]
    file_data = requests.get(f"{KINTONE_DOMAIN}/k/v1/file.json", headers=headers, params={"fileKey": file_key})
    path = os.path.join(tempfile.gettempdir(), file_name)
    with open(path, "wb") as f:
        f.write(file_data.content)
    return path, file_name

# Google Drive アップロード
def upload_to_drive_and_get_link(path, name, folder_id):
    creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON)
    service = build("drive", "v3", credentials=creds)
    metadata = {"name": name, "parents": [folder_id]}
    media = MediaFileUpload(path, mimetype="application/pdf")
    file = service.files().create(body=metadata, media_body=media, fields="id").execute()
    service.permissions().create(fileId=file["id"], body={"role": "reader", "type": "anyone"}).execute()
    return f"https://drive.google.com/file/d/{file['id']}/view?usp=sharing"

# Gemini 要約
def gemini_summarize(text, prompt="以下を要約してください："):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": f"{prompt}\n\n{text}"}]}]}
    res = requests.post(url, json=payload)
    try:
        data = res.json()
        raw = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        return clean_markdown(raw)
    except Exception:
        return "⚠ 要約に失敗しました"

# Markdown削除・文毎に改行整形
def clean_markdown(text):
    text = re.sub(r'\*\*|###|__|~~|`|[*#>]', '', text)
    text = re.sub(r'\n+', '\n', text).strip()
    text = re.sub(r'(?<=[。！？])\s*', '\n', text)
    return text

# OCR with Cloud Vision fallback
def extract_text_from_pdf(file_path):
    text = ""
    try:
        doc = fitz.open(file_path)
        for page in doc:
            text += page.get_text()
    except Exception:
        pass
    if not text.strip():
        try:
            for img in convert_from_path(file_path, dpi=300):
                text += pytesseract.image_to_string(img, lang='jpn')
        except Exception:
            pass
    if not text.strip():
        try:
            vision_client = vision.ImageAnnotatorClient.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON)
            for img in convert_from_path(file_path, dpi=300):
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                image = vision.Image(content=buf.getvalue())
                result = vision_client.document_text_detection(image=image)
                text += result.full_text_annotation.text
        except Exception:
            pass
    return text.strip()

# タイトル抽出
def extract_title_line(text):
    for line in text.splitlines():
        if re.search(r'[^\s]{6,}', line):
            return f"【{line.strip()}】"
    return "【AI要約レポート】"

# PDF作成
def create_summary_pdf(summary_text, original_title):
    today = datetime.now().strftime("%Y%m%d")
    filename = f"要約_{original_title.replace('.pdf','')}_{today}.pdf"
    path = os.path.join(tempfile.gettempdir(), filename)
    font_path = os.path.join("fonts", "mplus-1p-regular.ttf")

    class SummaryPDF(FPDF):
        def header(self):
            self.set_font("Mplus", '', 10)
            self.cell(0, 10, datetime.now().strftime("%Y-%m-%d"), ln=True, align='R')
            self.ln(3)

        def footer(self):
            self.set_y(-15)
            self.set_font("Mplus", '', 8)
            self.cell(0, 10, f"Page {self.page_no()}", align='C')

        def add_body(self, text):
            lines = text.splitlines()
            self.set_font("Mplus", '', 12)
            for line in lines:
                if line.startswith("【") and line.endswith("】"):
                    self.set_font("Mplus", '', 16)
                    self.cell(0, 12, line, ln=True, align='L')
                    self.set_font("Mplus", '', 12)
                else:
                    self.multi_cell(0, 10, line)
                    self.ln(2)

    pdf = SummaryPDF()
    pdf.add_font("Mplus", "", font_path, uni=True)
    pdf.add_page()
    pdf.add_body(summary_text)
    pdf.output(path)
    return path, filename

# kintoneへ書き戻し
def write_back_to_kintone(record_id, field_code, value):
    headers = {"X-Cybozu-API-Token": API_TOKEN, "Content-Type": "application/json"}
    payload = {"app": APP_ID, "id": record_id, "record": {field_code: {"value": value}}}
    return requests.put(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, json=payload)

# エンドポイント
@app.route("/", methods=["POST"])
def summarize():
    try:
        data = request.json
        record_id = data.get("recordId")
        prompt = data.get("prompt", "以下を要約してください：")
        pdf_path, file_name = fetch_pdf_from_kintone(record_id)
        original_link = upload_to_drive_and_get_link(pdf_path, file_name, DRIVE_FOLDER_ID)
        write_back_to_kintone(record_id, FIELD_ORIGINAL_LINK, original_link)

        text = extract_text_from_pdf(pdf_path)
        summary = gemini_summarize(text, prompt)
        write_back_to_kintone(record_id, FIELD_SUMMARY_TEXT, summary)

        summary_pdf_path, summary_pdf_name = create_summary_pdf(summary, file_name)
        summary_link = upload_to_drive_and_get_link(summary_pdf_path, summary_pdf_name, DRIVE_FOLDER_ID)
        write_back_to_kintone(record_id, FIELD_SUMMARY_LINK, summary_link)

        return jsonify({"summary": summary, "original_link": original_link, "summary_pdf_link": summary_link})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
