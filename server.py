import json
import os
import re
import random
import time
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai

load_dotenv()

app = Flask(__name__)
CORS(app)

GENAI_API_KEY = os.environ.get("GENAI_API_KEY")

if not GENAI_API_KEY:
    raise RuntimeError("GENAI_API_KEY を設定してください")

client = genai.Client(api_key=GENAI_API_KEY)

print("loading...")
with open("blog_data.json", "r", encoding="utf-8") as fl:
    all_articles = json.load(fl)

url_to_article = {a["url"]: a for a in all_articles}
print(str(len(all_articles)) + " articles loaded")


def _nums(*parts):
    result = []
    for p in parts:
        if isinstance(p, tuple):
            result.extend(range(p[0], p[1] + 1))
        else:
            result.append(p)
    return result

CATEGORY_ARTICLES = {
    "陰陽論・生命哲学": _nums((1,7), (9,10), (13,16), (19,28), (30,40), (42,70)),
    "宇宙・銀河の歴史": _nums((71,93), 101, (103,108), (110,121), (123,131)),
    "宇宙医学・健康":   _nums(132, 134, (136,139), (143,177), (179,189), (193,229), (231,243), 245, 248, 249, (251,260)),
    "ウイルス・感染症": _nums((262,292), (294,323), 325, (327,355), (357,360), 363, (365,376), (378,388), (390,402)),
    "日本・龍神島の歴史": _nums(8, 11, 12, 17, 18, (135,138), 140, 178, 230, 250, 361, 362),
    "天体・地球環境":   _nums(5, (94,100), 102, 109, 122, 132, 133, (141,142), 190, 212, 261, 293, (367,370), 399),
}

def _category_urls(category):
    nums = CATEGORY_ARTICLES.get(category, [])
    return [f"https://seimeiron.com/blog{n:03d}/" for n in nums]


# ウォームアップ用エンドポイント（スリープ対策）
@app.route("/warmup", methods=["GET"])
def warmup():
    return jsonify({"status": "ok"})


@app.route("/generate-quiz", methods=["POST"])
def generate_quiz():
    data = request.json or {}
    category = data.get("category", "").strip()
    difficulty = data.get("difficulty", "normal")

    try:
        context_parts = []
        sources = []

        if category and category in CATEGORY_ARTICLES:
            category_urls = _category_urls(category)
            url = random.choice(category_urls)
            article = url_to_article.get(url) or random.choice(all_articles)
        else:
            article = random.choice(all_articles)
            url = article.get("url", "")

        title = article.get("title", "")
        content = article.get("content", "")
        context_parts.append("タイトル: " + title + "\n本文:\n" + content)
        sources.append({"title": title, "url": url})

        context_text = "\n---\n".join(context_parts)
        difficulty_label = {
            "easy": "かんたん（基本的な内容）",
            "normal": "ふつう（標準的な理解）",
            "hard": "むずかしい（深い理解が必要）"
        }.get(difficulty, "ふつう")

        # クイズと解説を1回のAPIで同時生成
        prompt = (
            "以下のブログ記事の内容をもとに、日本語の4択クイズを1問と、その解説を作成してください。\n"
            f"難易度は「{difficulty_label}」です。\n\n"
            "必ずJSON形式のみで返してください。前置きや説明文は一切不要です。\n"
            "JSONの形式：\n"
            '{"question": "問題文", '
            '"choices": ["選択肢A", "選択肢B", "選択肢C", "選択肢D"], '
            '"answer_index": 正解の番号（0〜3の整数）, '
            '"explanation": "解説文（200〜300文字程度）"}\n\n'
            "ブログ記事：\n" + context_text
        )

        # Gemini呼び出し（503時は最大3回リトライ）
        last_error = None
        quiz_data = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=prompt,
                )
                raw = response.text.strip()
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if not m:
                    raise json.JSONDecodeError("JSONが見つかりません", raw, 0)
                quiz_data = json.loads(m.group())
                break
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(1)

        if quiz_data is None:
            raise last_error

        # explanationが空の場合のフォールバック
        if not quiz_data.get("explanation"):
            answer_idx = quiz_data.get("answer_index", 0)
            choices = quiz_data.get("choices", [])
            correct = choices[answer_idx] if answer_idx < len(choices) else ""
            quiz_data["explanation"] = f"正解は「{correct}」です。ブログ記事を参考にしてください。"

        quiz_data["source_urls"]   = [s["url"] for s in sources]
        quiz_data["source_titles"] = [s["title"] for s in sources]
        quiz_data["context"]       = context_text

        return jsonify(quiz_data)

    except json.JSONDecodeError as e:
        print("JSONパースエラー:", str(e))
        return jsonify({"error": "クイズの生成に失敗しました。もう一度お試しください。"}), 500
    except Exception as e:
        print("エラー:", str(e))
        return jsonify({"error": "エラーが発生しました: " + str(e)}), 500


@app.route("/explain", methods=["POST"])
def explain():
    # クイズ生成時にすでに解説を含めているので、ここはフォールバック用
    data = request.json or {}
    question          = data.get("question", "")
    choices           = data.get("choices", [])
    answer_index      = data.get("answer_index", 0)
    user_answer_index = data.get("user_answer_index", 0)
    explanation       = data.get("explanation", "")
    source_urls       = data.get("source_urls", [])
    source_titles     = data.get("source_titles", [])
    context           = data.get("context", "")

    is_correct = (answer_index == user_answer_index)

    # すでに解説がある場合はそのまま返す
    if explanation:
        return jsonify({
            "is_correct":    is_correct,
            "explanation":   explanation,
            "source_urls":   source_urls,
            "source_titles": source_titles,
        })

    # 解説がない場合のみGemini呼び出し
    correct_label = choices[answer_index] if answer_index < len(choices) else ""
    user_label    = choices[user_answer_index] if user_answer_index < len(choices) else ""
    result_label  = "正解" if is_correct else "不正解"

    try:
        prompt = (
            "以下のクイズについて、わかりやすい日本語で解説してください。\n\n"
            f"問題：{question}\n"
            f"正解：{correct_label}\n"
            f"ユーザーの回答：{user_label}（{result_label}）\n\n"
            "ブログ記事の内容：\n" + context + "\n\n"
            "必ずJSON形式のみで返してください。前置きや説明文は一切不要です。\n"
            "JSONの形式：\n"
            '{"explanation": "解説文（200〜300文字程度）"}'
        )

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )

        raw = response.text.strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise json.JSONDecodeError("JSONが見つかりません", raw, 0)
        explain_data = json.loads(match.group())

        return jsonify({
            "is_correct":    is_correct,
            "explanation":   explain_data.get("explanation", ""),
            "source_urls":   source_urls,
            "source_titles": source_titles,
        })

    except json.JSONDecodeError as e:
        print("JSONパースエラー:", str(e))
        return jsonify({"error": "解説の生成に失敗しました。"}), 500
    except Exception as e:
        print("エラー:", str(e))
        return jsonify({"error": "エラーが発生しました: " + str(e)}), 500


if __name__ == "__main__":
    app.run(port=5000)
