from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

@app.route("/", methods=["POST"])
def summarize():
    try:
        data = request.json
        text = data.get("text", "")
        prompt = data.get("prompt", "以下を要約してください：")

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

        summary = gemini.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "⚠ 要約できませんでした")

        return jsonify({"summary": summary})
    
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))  # ← Renderが使うPORT環境変数を読み込む
    app.run(host="0.0.0.0", port=port)
