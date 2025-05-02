from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import fitz  # PyMuPDF
import tempfile
import traceback
import base64
from dotenv import load_dotenv  # .envから環境変数を読み込む

# .env読み込み（ローカル開発時のみ。Renderでは不要）
load_dotenv()

app = Flask(__name__)
CORS(app)

# 環境変数から設定を読み込む
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
KINTONE_DOMAIN = "https://nunokawa.cybozu.com"
API_TOKEN = os.environ.get("API_TOKEN")
KINTONE_USER = os.environ.get("KINTONE_USER")
KINTONE_PASS = os.environ.get("KINTONE_PASS")
APP_ID = 563
FIELD_CODE_ATTACHMENT = "添付ファイル"
FIELD_CODE_SUMMARY = "要約文章"

# ----------------------------------------
# PDFをkintoneから取得して保存（ベーシック認証）
# ----------------------------------------
# --- ここを置き換える ---
file_headers = {
    "X-Cybozu-API-Token": API_TOKEN
}

res_file = requests.get(
    f"{KINTONE_DOMAIN}/k/v1/file.json",
    headers=file_headers,
    params={"fileKey": file_key}
)

print("📡 file.json レスポンスコード:", res_file.status_code, flush=True)
print("📡 内容（先頭100文字）:", res_file.content[:100], flush=True)

# ファイル保存
temp_path = os.path.join(tempfile.gettempdir(), file_name)
with open(temp_path, "wb") as f:
    f.write(res_file.content)

print(f"📁 PDF saved to: {temp_path} (size: {len(res_file.content)} bytes)", flush=True)
return temp_path


# ----------------------------------------
# PDF → テキスト抽出（PyMuPDF）
# ----------------------------------------
def extract_text_from_pdf(file_path):
    doc = fitz.open(file_path)
    text = ""
    for page in doc:
        text += page.get_text()
    return text

# ----------------------------------------
# Gemini APIで要約
# ----------------------------------------
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
    
    try:
        gemini = res.json()
        return gemini.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "⚠ 要約できませんでした")
    except Exception as e:
        print("❌ Gemini API解析エラー:", e, flush=True)
        print("📡 Geminiレスポンス:", res.text[:200], flush=True)
        return "⚠ Geminiからの要約に失敗しました"

# ----------------------------------------
# kintoneに要約を書き戻す
# ----------------------------------------
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

# ----------------------------------------
# メインエンドポイント
# ----------------------------------------
@app.route("/", methods=["POST"])
def summarize():
    print("🚀 /summarize POST 受信！", flush=True)
    try:
        data = request.json
        record_id = data.get("recordId")
        prompt = data.get("prompt", "以下を要約してください：")

        pdf_path = fetch_pdf_from_kintone(record_id)
        text = extract_text_from_pdf(pdf_path)
        summary = gemini_summarize(text, prompt)
        status, response_text = write_back_to_kintone(record_id, summary)

        return jsonify({
            "summary": summary,
            "kintone_status": status,
            "kintone_response": response_text
        })

    except Exception as e:
        print("❌ 例外発生:", str(e), flush=True)
        traceback.print_exc()
        return jsonify({"error": str(e)})

# ----------------------------------------
# アプリ起動
# ----------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
