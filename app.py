from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import fitz  # PyMuPDF
import tempfile
import traceback
import json

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Flask初期化
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
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

# -------------------------------
# PDFをkintoneから取得
# -------------------------------
def fetch_pdf_from_kintone(record_id):
    headers = {"X-Cybozu-API-Token": API_TOKEN}
    params = {"app": APP_ID, "id": record_id}
    res = requests.get(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, params=params)
    res.raise_for_status()

    record_data = res.json().get("record", {})
    if FIELD_CODE_ATTACHMENT not in record_data or not record_data[FIELD_CODE_ATTACHMENT]["value"]:
        raise Exception("添付ファイルが見つかりません")

    file_info = record_data[FIELD_CODE_ATTACHMENT]["value"][0]
    file_key = file_info["fileKey"]
    file_name = file_info["name"]

    res_file = requests.get(f"{KINTONE_DOMAIN}/k/v1/file.json", headers=headers, params={"fileKey": file_key})
    res_file.raise_for_status()

    temp_path = os.path.join(tempfile.gettempdir(), file_name)
    with open(temp_path, "wb") as f:
        f.write(res_file.content)
    return temp_path, file_name

# -------------------------------
# PDFからテキスト抽出（PyMuPDF）
# -------------------------------
def extract_text_from_pdf(file_path):
    doc = fitz.open(file_path)
    text = ""
    for page in doc:
        text += page.get_text()
    return text

# -------------------------------
# Google Driveにアップロード（SA方式）
# -------------------------------
def upload_to_drive_and_get_link(local_pdf_path, file_name, folder_id):
    creds_info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_info)
    service = build('drive', 'v3', credentials=creds)

    file_metadata = {'name': file_name, 'parents': [folder_id]}
    media = MediaFileUpload(local_pdf_path, mimetype='application/pdf')
    uploaded = service.files().create(body=file_metadata, media_body=media, fields='id').execute()

    # 公開設定（誰でも閲覧可）
    service.permissions().create(
        fileId=uploaded['id'],
        body={'role': 'reader', 'type': 'anyone'}
    ).execute()

    return f"https://drive.google.com/file/d/{uploaded['id']}/view?usp=sharing"

# -------------------------------
# Gemini APIで要約
# -------------------------------
def gemini_summarize(text, prompt="以下を要約してください："):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {
                "parts": [{"text": f"{prompt}\n\n{text}"}]
            }
        ]
    }
    res = requests.post(url, json=payload)
    try:
        gemini = res.json()
        return gemini.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "⚠ 要約できませんでした")
    except Exception as e:
        print("Geminiレスポンスエラー:", e)
        return "⚠ Geminiからの要約に失敗しました"

# -------------------------------
# kintoneに書き戻し
# -------------------------------
def write_back_to_kintone(record_id, field_code, value):
    headers = {
        "X-Cybozu-API-Token": API_TOKEN,
        "Content-Type": "application/json"
    }
    body = {
        "app": APP_ID,
        "id": record_id,
        "record": {
            field_code: {"value": value}
        }
    }
    res = requests.put(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, json=body)
    return res.status_code, res.text

# -------------------------------
# エンドポイント
# -------------------------------
@app.route("/", methods=["POST"])
def summarize():
    try:
        data = request.json
        record_id = data.get("recordId")
        prompt = data.get("prompt", "以下を要約してください：")

        # 1. PDF取得
        pdf_path, file_name = fetch_pdf_from_kintone(record_id)

        # 2. Driveアップロード → 公開リンク取得
        drive_link = upload_to_drive_and_get_link(pdf_path, file_name, DRIVE_FOLDER_ID)

        # 3. kintoneへ原本リンク書き戻し
        status1, res1 = write_back_to_kintone(record_id, FIELD_CODE_ORIGINAL_LINK, drive_link)

        # 4. 要約生成
        text = extract_text_from_pdf(pdf_path)
        summary = gemini_summarize(text, prompt)

        # 5. kintoneへ要約文書き戻し
        status2, res2 = write_back_to_kintone(record_id, FIELD_CODE_SUMMARY, summary)

        return jsonify({
            "summary": summary,
            "original_link": drive_link,
            "writeback_summary_status": status2,
            "writeback_original_status": status1
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})

# -------------------------------
# 実行（Render対応）
# -------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
