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
from pdf2image import convert_from_path
from PIL import Image
import pytesseract
import json
import re

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- 環境変数読み込み ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
KINTONE_DOMAIN = "https://nunokawa.cybozu.com"
API_TOKEN = os.environ.get("API_TOKEN")
APP_ID = 563
FIELD_CODE_ATTACHMENT = "添付ファイル"
FIELD_CODE_SUMMARY = "要約文章"
FIELD_CODE_ORIGINAL_LINK = "原本リンク"
FIELD_CODE_SUMMARY_LINK = "要約リンク"
GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
PORT = int(os.environ.get("PORT", 10000))

FOLDER_STRUCTURE = {
    "原本": {
        "国税速報": os.environ.get("ORIGINAL_KOKUZEI_ID"),
        "税理士新聞": os.environ.get("ORIGINAL_SHINBUN_ID"),
        "TAINS": os.environ.get("ORIGINAL_TAINS_ID"),
        "研修資料": os.environ.get("ORIGINAL_KENSHU_ID"),
        "書籍": os.environ.get("ORIGINAL_BOOK_ID"),
        "その他": os.environ.get("ORIGINAL_OTHER_ID")
    },
    "要約": {
        "国税速報": os.environ.get("SUMMARY_KOKUZEI_ID"),
        "税理士新聞": os.environ.get("SUMMARY_SHINBUN_ID"),
        "TAINS": os.environ.get("SUMMARY_TAINS_ID"),
        "研修資料": os.environ.get("SUMMARY_KENSHU_ID"),
        "書籍": os.environ.get("SUMMARY_BOOK_ID"),
        "その他": os.environ.get("SUMMARY_OTHER_ID")
    }
}

def classify_folder_name(title):
    keywords = ["国税速報", "税理士新聞", "TAINS", "研修資料", "書籍"]
    for keyword in keywords:
        if keyword in title:
            return keyword
    return "その他"

def upload_to_drive_with_classification(local_path, file_name, category_type, title):
    folder_name = classify_folder_name(title)
    folder_id = FOLDER_STRUCTURE.get(category_type, {}).get(folder_name)

    if not folder_id:
        print(f"❗ 無効なカテゴリ {category_type} またはフォルダ名 {folder_name}")
        raise ValueError("フォルダIDが見つかりません")

    creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON)
    service = build("drive", "v3", credentials=creds)

    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype="application/pdf")
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    service.permissions().create(fileId=uploaded["id"], body={"role": "reader", "type": "anyone"}).execute()
    return f"https://drive.google.com/file/d/{uploaded['id']}/view?usp=sharing"


def fetch_pdf_from_kintone(record_id):
    headers = {"X-Cybozu-API-Token": API_TOKEN}
    params = {"app": APP_ID, "id": record_id}
    res = requests.get(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, params=params)
    record_data = res.json().get("record", {})
    file_info = record_data[FIELD_CODE_ATTACHMENT]["value"][0]
    file_key = file_info["fileKey"]
    original_name = file_info["name"]

    res_file = requests.get(f"{KINTONE_DOMAIN}/k/v1/file.json", headers=headers, params={"fileKey": file_key})
    today = datetime.now().strftime("%Y%m%d")
    title = original_name.replace(".pdf", "")
    renamed_name = f"原本_{title}_{today}.pdf"
    temp_path = os.path.join(tempfile.gettempdir(), renamed_name)
    with open(temp_path, "wb") as f:
        f.write(res_file.content)
    return temp_path, title

def gemini_summarize(text, prompt="以下を要約してください："):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": f"{prompt}\n\n{text}"}]}]}
    res = requests.post(url, json=payload)
    try:
        gemini = res.json()
        raw = gemini.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "⚠ 要約できませんでした")
        clean = re.sub(r'[*#]{1,}', '', raw)
        return clean.strip()
    except Exception:
        return "⚠ Geminiからの要約に失敗しました"

def ocr_with_google_vision(file_path):
    from google.cloud import vision
    import io
    client = vision.ImageAnnotatorClient.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON)
    images = convert_from_path(file_path, dpi=300)
    full_text = ""
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        image = vision.Image(content=buf.read())
        response = client.document_text_detection(image=image)
        if response.error.message:
            raise Exception(response.error.message)
        full_text += response.full_text_annotation.text + "\n"
    return full_text

def extract_text_from_pdf(file_path):
    text = ""
    try:
        doc = fitz.open(file_path)
        for page in doc:
            text += page.get_text()
    except Exception as e:
        print(f"⚠️ fitzエラー: {e}")
    if not text.strip():
        try:
            images = convert_from_path(file_path, dpi=300)
            for img in images:
                text += pytesseract.image_to_string(img, lang='jpn')
            print("🧠 pytesseract成功")
        except Exception as e:
            print(f"❌ pytesseract失敗: {e}")
    if not text.strip():
        try:
            text = ocr_with_google_vision(file_path)
            print("📷 Cloud Vision成功")
        except Exception as e:
            print(f"❌ Cloud Vision失敗: {e}")
    return text

def create_summary_pdf(summary_text, title, prompt_text):
    today = datetime.now().strftime("%Y%m%d")
    file_name = f"要約_{title}_{today}.pdf"
    pdf_path = os.path.join(tempfile.gettempdir(), file_name)
    font_path = os.path.join("fonts", "mplus-1p-regular.ttf")

    class SummaryPDF(FPDF):
        def header(self):
            self.set_font("Mplus", '', 10)
            self.cell(0, 10, datetime.now().strftime("%Y-%m-%d"), ln=True, align='R')
            self.ln(5)

        def footer(self):
            self.set_y(-15)
            self.set_font("Mplus", '', 8)
            self.cell(0, 10, f"Page {self.page_no()}", align='C')

        def add_title(self, title):
            self.set_font("Mplus", '', 16)
            self.cell(0, 12, f"【{title}】", ln=True, align='L')
            self.ln(6)

        def add_paragraphs(self, text):
            self.set_font("Mplus", '', 12)
            for line in text.split('\n'):
                cleaned = line.strip()
                if cleaned:
                    if cleaned.startswith("【") and cleaned.endswith("】"):
                        self.set_font("Mplus", '', 14)
                        self.cell(0, 10, cleaned, ln=True)
                        self.set_font("Mplus", '', 12)
                    elif cleaned.startswith("・") or cleaned.startswith("■"):
                        self.multi_cell(0, 10, cleaned, align='L')
                    else:
                        self.multi_cell(0, 10, cleaned, align='L')
                        self.ln(2)

    pdf = SummaryPDF()
    pdf.add_font("Mplus", "", font_path, uni=True)
    pdf.add_page()
    pdf.add_title(title)
    pdf.add_paragraphs(summary_text)
    pdf.output(pdf_path)
    return pdf_path, file_name

def write_back_to_kintone(record_id, field_code, value):
    headers = {"X-Cybozu-API-Token": API_TOKEN, "Content-Type": "application/json"}
    body = {"app": APP_ID, "id": record_id, "record": {field_code: {"value": value}}}
    res = requests.put(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, json=body)
    return res.status_code, res.text

def clear_attachment_field(record_id, field_code="添付ファイル"):
    headers = {"X-Cybozu-API-Token": API_TOKEN, "Content-Type": "application/json"}
    body = {"app": APP_ID, "id": record_id, "record": {field_code: {"value": []}}}
    res = requests.put(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, json=body)
    return res.status_code, res.text

@app.route("/", methods=["POST", "OPTIONS", "HEAD"])
def summarize():
    if request.method == "OPTIONS" or request.method == "HEAD":
        return '', 200
    try:
        data = request.json
        record_id = data.get("recordId")
        prompt = data.get("prompt", "以下を要約してください：")
        pdf_path, title = fetch_pdf_from_kintone(record_id)

        original_link = upload_to_drive_with_classification(pdf_path, os.path.basename(pdf_path), "原本", title)
        write_back_to_kintone(record_id, FIELD_CODE_ORIGINAL_LINK, original_link)

        text = extract_text_from_pdf(pdf_path)
        summary = gemini_summarize(text, prompt)
        write_back_to_kintone(record_id, FIELD_CODE_SUMMARY, summary)

        summary_pdf_path, summary_file_name = create_summary_pdf(summary, title.replace(".pdf", ""), prompt)
        summary_link = upload_to_drive_with_classification(summary_pdf_path, os.path.basename(summary_pdf_path), "要約", title)
        write_back_to_kintone(record_id, FIELD_CODE_SUMMARY_LINK, summary_link)

        clear_attachment_field(record_id)

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
