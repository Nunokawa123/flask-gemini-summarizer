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
FIELD_CODE_DOC_TYPE = "文書種類"
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

def classify_folder_by_radio_field(record_data):
    doc_type = record_data.get(FIELD_CODE_DOC_TYPE, {}).get("value", "その他")
    if doc_type in ["国税速報", "税理士新聞", "TAINS", "研修資料"]:
        return doc_type
    return "その他"

def upload_to_drive_with_doc_type(local_path, file_name, category_type, doc_type):
    folder_id = FOLDER_STRUCTURE.get(category_type, {}).get(doc_type)
    if not folder_id:
        print(f"❗ 無効なカテゴリ {category_type} または文書種類 {doc_type}")
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
    return temp_path, title, record_data

def summarize():
    try:
        data = request.json
        record_id = data.get("recordId")
        prompt = data.get("prompt", "以下を要約してください：")
        pdf_path, title, record_data = fetch_pdf_from_kintone(record_id)
        doc_type = classify_folder_by_radio_field(record_data)

        original_link = upload_to_drive_with_doc_type(pdf_path, os.path.basename(pdf_path), "原本", doc_type)
        write_back_to_kintone(record_id, FIELD_CODE_ORIGINAL_LINK, original_link)

        text = extract_text_from_pdf(pdf_path)
        summary = gemini_summarize(text, prompt)
        write_back_to_kintone(record_id, FIELD_CODE_SUMMARY, summary)

        summary_pdf_path, summary_file_name = create_summary_pdf(summary, title.replace(".pdf", ""), prompt)
        summary_link = upload_to_drive_with_doc_type(summary_pdf_path, os.path.basename(summary_pdf_path), "要約", doc_type)
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

@app.route("/", methods=["POST", "OPTIONS", "HEAD"])
def main():
    if request.method in ["OPTIONS", "HEAD"]:
        return '', 200
    return summarize()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
