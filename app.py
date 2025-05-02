from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import fitz  # PyMuPDF
import tempfile

app = Flask(__name__)
CORS(app)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
KINTONE_DOMAIN = "https://nunokawa.cybozu.com"
API_TOKEN = "iRuCw2VNtl3euFtsM1iiZN9RfqpQI6MHlmTcEKMw"
APP_ID = 563
FIELD_CODE_ATTACHMENT = "添付ファイル"
FIELD_CODE_SUMMARY = "要約文章"

# -------------------------------
# PDFをkintoneから取得して保存
# -------------------------------
def fetch_pdf_from_kintone(record_id):
    headers = {
        "X-Cybozu-API-Token": API_TOKEN,
        "Content-Type": "application/json"
    }
    params = {
        "app": APP_ID,
        "id": record_id
    }
   res = requests.get(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, params=params)
print("✅ kintone APIレスポンスコード:", res.status_code)
print("📦 レスポンス内容:", res.text)

    file_info = res.json()["record"][FIELD_CODE_ATTACHMENT]["value"][0]
    file_key = file_info["fileKey"]
    file_name = file_info["name"]

    res_file = requests.post(f"{KINTONE_DOMAIN}/k/v1/file.json", headers=headers, json={"fileKey": file_key})
    temp_path = os.path.join(tempfile.gettempdir(), file_name)
    with open(temp_path, "wb") as f:
        f.write(res_file.content)
    return temp_path

# -------------------------------
# PDF → テキスト抽出（PyMuPDF）
# -------------------------------
def extract_text_from_pdf(file_path):
    doc = fitz.open(file_path)
    text = ""
    for page in doc:
        text += page.get_text()
    return text

# -------------------------------
# Gemini要約
# -------------------------------
def gemini_summarize(text, prompt="以下を要約してください："):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"{prompt}\n\n{text}"}
                ]
            }
        ]
    }
    res = requests.post(url, json=payload)
    gemini = res.json()
    return gemini.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "⚠ 要約できませんでした")

# -------------------------------
# kintoneに要約を書き戻す
# -------------------------------
def write_back_to_kintone(record_id, summary_text):
    headers = {
        "X-Cybozu-API-Token": API_TOKEN,
        "Content-Type": "application/json"
    }
    body = {
        "app": APP_ID,
        "id": record_id,
        "record": {
            FIELD_CODE_SUMMARY: {"value": summary_text}
        }
    }
    res = requests.put(f"{KINTONE_DOMAIN}/k/v1/record.json", headers=headers, json=body)
    return res.status_code, res.text

# -------------------------------
# メインルート：/summarize
# -------------------------------
@app.route("/", methods=["POST"])
def summarize():
    try:
        data = request.json
        record_id = data.get("recordId")
        prompt = data.get("prompt", "以下を要約してください：")

        pdf_path = fetch_pdf_from_kintone(record_id)
        text = extract_text_from_pdf(pdf_path)
        summary = gemini_summarize(text, prompt)
        status, response_text = write_back_to_kintone(record_id, summary)

        return jsonify({"summary": summary, "kintone_status": status, "kintone_response": response_text})

    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
