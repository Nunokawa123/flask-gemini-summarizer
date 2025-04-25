from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

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

