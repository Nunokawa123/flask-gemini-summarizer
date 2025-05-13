from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import fitz  # PyMuPDF
import tempfile
import traceback
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account
from pdf2image import convert_from_path
from PIL import Image
import pytesseract
import json
import io
from google.cloud import vision
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- 環境変数読み込み ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
KINTONE_DOMAIN = "https://nunokawa.cybozu.com"
API_TOKEN = os.environ.get("API_TOKEN")
APP_ID = 563
FIELD_CODE_ATTACHMENT = "添付ファイル"
FIELD_CODE_ORIGINAL_LINK = "原本リンク"
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
PORT = int(os.environ.get("PORT", 10000))

# --- kintone PDF取得 ---
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

# --- Driveにアップロード ---
def upload_to_drive_and_get_link(local_path, file_name, folder_id):
    creds = service_account.Credentials.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON)
    service = build("drive", "v3", credentials=creds)
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype="application/pdf")
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    service.permissions().create(fileId=uploaded["id"], body={"role": "reader", "type": "anyone"}).execute()
    return f"https://drive.google.com/file/d/{uploaded['id']}/view?usp=sharing"

# --- kintone書き戻し ---
def write_back_to_kintone(record_id, field_code, value):
    headers = {"X-Cybozu-API-Token": API_TOKEN, "Content-Type": "application/json"}
    body = {"app": APP_ID, "id": record_id, "record": {field_code: {"value": value}}}
    res = requests.put(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, json=body)
    return res.status_code, res.text

# --- 添付ファイル削除 ---
def clear_attachment_field(record_id, field_code="添付ファイル"):
    headers = {"X-Cybozu-API-Token": API_TOKEN, "Content-Type": "application/json"}
    body = {"app": APP_ID, "id": record_id, "record": {field_code: {"value": []}}}
    res = requests.put(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, json=body)
    return res.status_code, res.text

# --- 検索可能PDF生成 ---
def create_searchable_pdf_from_vision(image_list, text_list, output_path):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4
    for img, text in zip(image_list, text_list):
        c.drawImage(ImageReader(img), 0, 0, width=width, height=height)
        c.setFont("Helvetica", 10)
        c.setFillColorRGB(1, 1, 1, alpha=0.0)  # 透明テキスト
        c.drawString(10, height - 30, text[:300])  # 簡略に描画（必要に応じ調整）
        c.showPage()
    c.save()
    return output_path

# --- メインエンドポイント ---
@app.route("/", methods=["POST"])
def summarize():
    try:
        data = json.loads(request.data.decode("utf-8"))
        record_id = data.get("recordId")

        # PDF取得
        pdf_path, title = fetch_pdf_from_kintone(record_id)
        images = convert_from_path(pdf_path, dpi=300)

        # Vision OCR
        client = vision.ImageAnnotatorClient.from_service_account_info(GOOGLE_SERVICE_ACCOUNT_JSON)
        ocr_texts = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            buf.seek(0)
            image = vision.Image(content=buf.read())
            response = client.document_text_detection(image=image)
            text = response.full_text_annotation.text if response and response.full_text_annotation else ""
            ocr_texts.append(text)

        # 検索可能PDF作成
        searchable_pdf_path = os.path.join(tempfile.gettempdir(), f"検索可能_{title}.pdf")
        create_searchable_pdf_from_vision(images, ocr_texts, searchable_pdf_path)

        # Driveにアップロード
        link = upload_to_drive_and_get_link(searchable_pdf_path, os.path.basename(searchable_pdf_path), DRIVE_FOLDER_ID)

        # kintoneに書き戻し
        write_back_to_kintone(record_id, FIELD_CODE_ORIGINAL_LINK, link)
        clear_attachment_field(record_id)

        return jsonify({"searchable_pdf_link": link})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
