# -*- coding: utf-8 -*-
"""
問診システム サーバー(v2)

構成:
- /admin/login          : スタッフ用ログイン画面(共通パスワード)
- /admin/                : ログイン後のダッシュボード(患者IDを入力してリンクを発行)
- /admin/view/<id>       : ログイン必須。指定した患者IDの最新回答を表示
- /admin/qr/<token>.png  : 発行したリンクのQRコード画像
- /form/<token>          : 患者がスマホ等で回答するフォーム(ログイン不要・誰でも開ける)
- /submit                : フォームの送信先(POST)

トークンは推測されにくいランダム文字列です。患者IDそのものをURLに含めないため、
第三者が連番を試して他の患者の画面を開いてしまうリスクを防ぎます。

環境変数:
- ADMIN_PASSWORD : 管理画面ログイン用パスワード(必ず変更してください)
- SECRET_KEY      : Flaskセッション用の秘密鍵(必ずランダムな値に変更してください)
"""

import io
import json
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

import qrcode
from flask import (
    Flask,
    request,
    render_template_string,
    redirect,
    url_for,
    session,
    send_file,
    abort,
    jsonify,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
TOKENS_PATH = os.path.join(DATA_DIR, "_tokens.json")
DIRECTORY_PATH = os.path.join(DATA_DIR, "_directory.json")

JST = timezone(timedelta(hours=9))


def now_jst() -> datetime:
    return datetime.now(JST)


def now_jst_str() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S")


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-before-deploying")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-this-password")

# ブックマークレットからの登録リクエストを認証するための簡易キー。
# 必ず環境変数で上書きしてください(既定値のままは危険です)。
REGISTER_API_KEY = os.environ.get("REGISTER_API_KEY", "change-this-api-key")

# 問診管理アプリ(スタッフ用iPadアプリ)からのAPIアクセスを認証するキー。
STAFF_API_KEY = os.environ.get("STAFF_API_KEY", "change-this-staff-key")

# 受付端末(N2017.cgi)のURLの組み立て方。院内Wi-Fi経由でのみアクセス可能。
RS_BASE_URL_TEMPLATE = os.environ.get(
    "RS_BASE_URL_TEMPLATE", "http://192.168.12.40/~rsn/N2017.cgi?{id}===="
)

# ---------------------------------------------------------------------------
# トークン(発行リンク) の管理
# ---------------------------------------------------------------------------

def _load_tokens():
    if not os.path.exists(TOKENS_PATH):
        return {}
    with open(TOKENS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_tokens(tokens):
    with open(TOKENS_PATH, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)


def create_token(patient_id: str, form_type: str = "general") -> str:
    tokens = _load_tokens()
    token = secrets.token_urlsafe(16)
    tokens[token] = {
        "patient_id": patient_id,
        "form_type": form_type,
        "created_at": now_jst_str(),
    }
    _save_tokens(tokens)
    return token


def resolve_token(token: str):
    tokens = _load_tokens()
    entry = tokens.get(token)
    return entry["patient_id"] if entry else None


def resolve_token_type(token: str) -> str:
    tokens = _load_tokens()
    entry = tokens.get(token)
    return entry.get("form_type", "general") if entry else "general"


def resolve_token_full(token: str):
    tokens = _load_tokens()
    return tokens.get(token)


# ---------------------------------------------------------------------------
# 患者氏名台帳(受付フローで自動登録される)
# ---------------------------------------------------------------------------

def _load_directory():
    if not os.path.exists(DIRECTORY_PATH):
        return {}
    with open(DIRECTORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_directory(directory):
    with open(DIRECTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(directory, f, ensure_ascii=False, indent=2)


def upsert_directory(patient_id: str, name: str, dob: str, gender: str = ""):
    directory = _load_directory()
    existing = directory.get(patient_id, {})
    directory[patient_id] = {
        "name": name,
        "dob": dob,
        "gender": gender or existing.get("gender", ""),
        "updated_at": now_jst_str(),
    }
    _save_directory(directory)


def lookup_directory(patient_id: str):
    return _load_directory().get(patient_id)


def calculate_age(dob_str: str):
    if not dob_str:
        return None
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", dob_str)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    today = now_jst()
    age = today.year - y
    if (today.month, today.day) < (mo, d):
        age -= 1
    return age



# ---------------------------------------------------------------------------
# 患者データの保存
# ---------------------------------------------------------------------------

def data_path(patient_id: str) -> str:
    safe_id = "".join(ch for ch in patient_id if ch.isalnum())
    if not safe_id:
        abort(400, "invalid patient id")
    return os.path.join(DATA_DIR, f"{safe_id}.json")


def load_records(patient_id: str):
    path = data_path(patient_id)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_record(patient_id: str, record: dict):
    records = load_records(patient_id)
    records.append(record)
    with open(data_path(patient_id), "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 問診項目の定義(叩き台。必要に応じて編集してください)
# ---------------------------------------------------------------------------

FORM_TYPES = {
    "general": {
        "label": "一般問診(テスト用)",
        "fields": [
            {"key": "chief_complaint", "label": "本日困っていること・受診理由", "type": "textarea"},
            {"key": "symptom_onset", "label": "症状が出始めた時期(例: 3日前から、1週間前から)", "type": "text"},
            {"key": "history", "label": "既往歴(これまでにかかった病気)", "type": "textarea"},
            {"key": "allergy", "label": "アレルギー(薬・食物など)", "type": "textarea"},
            {"key": "medication", "label": "現在服用中のお薬", "type": "textarea"},
            {"key": "smoking", "label": "喫煙", "type": "radio", "options": ["なし", "以前吸っていた", "現在吸っている"]},
            {"key": "alcohol", "label": "飲酒", "type": "radio", "options": ["なし", "機会飲酒", "毎日飲む"]},
            {"key": "hepatitis", "label": "B型・C型肝炎、ピロリ菌の指摘", "type": "radio", "options": ["なし", "あり", "不明"]},
            {"key": "other_hospital", "label": "他科受診中の病院・診療科", "type": "textarea"},
            {"key": "care_level", "label": "要介護度", "type": "radio", "options": ["なし", "要支援", "要介護"]},
            {"key": "family_history", "label": "家族歴", "type": "textarea"},
            {"key": "family_living", "label": "同居家族", "type": "textarea"},
            {"key": "family_contact", "label": "緊急連絡先(ご家族の連絡先)", "type": "text"},
        ],
    },
    "urology_general": {
        "label": "泌尿器科 一般問診",
        "fields": [
            {"key": "ticketNumber", "label": "番号札の番号", "type": "text"},
            {"key": "pastIllnessStatus", "label": "治療中もしくは過去に治療をした病気", "type": "text"},
            {"key": "pastIllnessItems", "label": "既往症(該当項目)", "type": "text"},
            {"key": "pastIllnessOtherDetail", "label": "既往症その他の詳細", "type": "text"},
            {"key": "medicationStatus", "label": "現在飲んでいるお薬", "type": "text"},
            {"key": "medicationBook", "label": "お薬手帳の提出", "type": "text"},
            {"key": "medicationDetail", "label": "服用中の薬品名", "type": "text"},
            {"key": "agaStatus", "label": "AGA治療薬の服用", "type": "text"},
            {"key": "agaDetail", "label": "AGA治療薬の薬品名", "type": "text"},
            {"key": "familyCancerStatus", "label": "ご家族(血縁者)のがん", "type": "text"},
            {"key": "familyCancerItems", "label": "家族のがん(該当項目)", "type": "text"},
            {"key": "familyCancerOtherDetail", "label": "家族のがんその他の詳細", "type": "text"},
            {"key": "allergyStatus", "label": "薬のアレルギー", "type": "text"},
            {"key": "allergyDetail", "label": "アレルギーの薬品名", "type": "text"},
            {"key": "alcohol", "label": "飲酒", "type": "text"},
            {"key": "smoking", "label": "喫煙", "type": "text"},
            {"key": "smokeActivePerDay", "label": "喫煙(現在) 1日平均本数", "type": "text"},
            {"key": "smokeActiveStartAge", "label": "喫煙(現在) 開始年齢", "type": "text"},
            {"key": "smokeActiveYears", "label": "喫煙(現在) 喫煙年数", "type": "text"},
            {"key": "smokeQuitPerDay", "label": "喫煙(禁煙中) 1日平均本数", "type": "text"},
            {"key": "smokeQuitStartAge", "label": "喫煙(禁煙中) 開始年齢", "type": "text"},
            {"key": "smokeQuitEndAge", "label": "喫煙(禁煙中) 終了年齢", "type": "text"},
            {"key": "smokeQuitYears", "label": "喫煙(禁煙中) 喫煙年数", "type": "text"},
            {"key": "pregnant", "label": "妊娠中か", "type": "text"},
            {"key": "pregnantWeek", "label": "妊娠週数", "type": "text"},
            {"key": "breastfeeding", "label": "授乳中か", "type": "text"},
            {"key": "menstruating", "label": "生理中か", "type": "text"},
            {"key": "symptomOnset", "label": "症状はいつからか", "type": "text"},
            {"key": "feverStatus", "label": "発熱の有無", "type": "text"},
            {"key": "feverFrom", "label": "発熱期間(いつから)", "type": "text"},
            {"key": "feverTo", "label": "発熱期間(いつまで)", "type": "text"},
            {"key": "feverMaxTemp", "label": "最高体温", "type": "text"},
            {"key": "symptoms", "label": "今日はどうされましたか(該当症状)", "type": "text"},
            {"key": "backPainSide", "label": "背中の痛み(部位)", "type": "text"},
            {"key": "testicleDiscomfortType", "label": "睾丸の違和感(種類)", "type": "text"},
            {"key": "stdConcernDetail", "label": "性感染症が気になる(詳細)", "type": "text"},
            {"key": "stdDiseaseDetail", "label": "性感染症の病名", "type": "text"},
            {"key": "checkupAbnormalityDetail", "label": "健康診断で指摘された項目", "type": "text"},
            {"key": "freeInjectionItems", "label": "自由注射(種類)", "type": "text"},
            {"key": "voidingOneWeekPlus", "label": "排尿症状は以前から気になるか", "type": "text"},

            {"key": "ipss_residual", "label": "残尿感:排尿後に尿が残っている感じがありましたか", "type": "score_radio",
             "options": ["まったくなかった", "5回に1回未満", "2回に1回未満", "2回に1回くらい", "2回に1回以上", "ほとんどいつも"]},
            {"key": "ipss_frequency", "label": "頻尿:排尿後2時間以内にもう一度、排尿しなければならないことがありましたか", "type": "score_radio",
             "options": ["まったくなかった", "5回に1回未満", "2回に1回未満", "2回に1回くらい", "2回に1回以上", "ほとんどいつも"]},
            {"key": "ipss_intermittency", "label": "尿線途絶:排尿の途中で尿が途切れることがありましたか", "type": "score_radio",
             "options": ["まったくなかった", "5回に1回未満", "2回に1回未満", "2回に1回くらい", "2回に1回以上", "ほとんどいつも"]},
            {"key": "ipss_urgency", "label": "尿意切迫感:尿を我慢するのが難しいことがありましたか", "type": "score_radio",
             "options": ["まったくなかった", "5回に1回未満", "2回に1回未満", "2回に1回くらい", "2回に1回以上", "ほとんどいつも"]},
            {"key": "ipss_weak_stream", "label": "尿線細少:尿の勢いが弱いことがありましたか", "type": "score_radio",
             "options": ["まったくなかった", "5回に1回未満", "2回に1回未満", "2回に1回くらい", "2回に1回以上", "ほとんどいつも"]},
            {"key": "ipss_straining", "label": "腹圧排尿:尿を出し始めるためにお腹に力を入れることがありましたか", "type": "score_radio",
             "options": ["まったくなかった", "5回に1回未満", "2回に1回未満", "2回に1回くらい", "2回に1回以上", "ほとんどいつも"]},
            {"key": "ipss_nocturia", "label": "夜間頻尿:夜寝てから朝起きるまでに、何回くらい排尿に起きましたか", "type": "score_radio",
             "options": ["0回", "1回", "2回", "3回", "4回", "5回以上"]},
            {"key": "ipss_qol", "label": "QOL:現在の尿の状態がこのまま変わらずに続くとしたら、あなたはどう思いますか", "type": "score_radio",
             "options": ["とても満足", "満足", "まあ満足", "どちらともいえない", "やや不満", "いやだ", "とても悪い"]},
            {"key": "ipss_total", "label": "IPSS合計点(35点満点)", "type": "computed"},
            {"key": "ipss_qol_score", "label": "IPSS QOLスコア(6点満点)", "type": "computed"},

            {"key": "oabss_daytime", "label": "朝起きた時から夜寝るまでに、何回くらい尿をしましたか(頻度尿)", "type": "score_radio",
             "options": ["7回以下", "8〜14回", "15回以上"]},
            {"key": "oabss_nighttime", "label": "夜寝てから朝起きるまでに、何回くらい尿をするために起きましたか(夜間排尿)", "type": "score_radio",
             "options": ["0回", "1回", "2回", "3回以上"]},
            {"key": "oabss_urgency", "label": "急に尿がしたくなり、我慢が難しいことがありましたか(尿意切迫感)", "type": "score_radio",
             "options": ["なし", "週に1回より少ない", "週に1回以上", "1日1回くらい", "1日2〜4回", "1日5回以上"]},
            {"key": "oabss_incontinence", "label": "急に尿意を感じ、我慢できずに尿が漏れることがありましたか(切迫性尿失禁)", "type": "score_radio",
             "options": ["なし", "週に1回より少ない", "週に1回以上", "1日1回くらい", "1日2〜4回", "1日5回以上"]},
            {"key": "oabss_total", "label": "OABSS合計点(15点満点)", "type": "computed"},

            {"key": "freeNote", "label": "その他、気になる症状や相談したい事柄", "type": "textarea"},
        ],
    },
}


def get_form_fields(form_type: str):
    return FORM_TYPES.get(form_type, FORM_TYPES["general"])["fields"]

# ---------------------------------------------------------------------------
# 管理画面(ログイン保護)
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


LOGIN_PAGE = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>スタッフログイン</title>
<style>
  body{font-family:sans-serif;background:#f4f6f8;display:flex;height:100vh;
       align-items:center;justify-content:center;margin:0;}
  .box{background:#fff;padding:28px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.1);width:280px;}
  input{width:100%;padding:10px;font-size:15px;margin:10px 0;box-sizing:border-box;
        border:1px solid #ccc;border-radius:6px;}
  button{width:100%;padding:10px;font-size:15px;background:#1565c0;color:#fff;
         border:none;border-radius:6px;cursor:pointer;}
  .error{color:#c62828;font-size:13px;}
</style></head><body>
<div class="box">
  <h3>スタッフログイン</h3>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post">
    <input type="password" name="password" placeholder="パスワード" autofocus>
    <button type="submit">ログイン</button>
  </form>
</div>
</body></html>
"""


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)
        error = "パスワードが違います。"
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


DASHBOARD_PAGE = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>問診リンク発行</title>
<style>
  body{font-family:sans-serif;padding:20px;max-width:520px;margin:0 auto;color:#222;}
  input{padding:9px;font-size:15px;border:1px solid #ccc;border-radius:6px;}
  button{padding:9px 16px;font-size:15px;background:#1565c0;color:#fff;border:none;
         border-radius:6px;cursor:pointer;margin-left:6px;}
  .result{margin-top:18px;background:#f4f6f8;border-radius:8px;padding:14px;}
  .result a{word-break:break-all;}
  .top a{font-size:13px;}
  img{margin-top:10px;}
</style></head><body>
<div class="top"><a href="{{ url_for('admin_logout') }}">ログアウト</a></div>
<h2>問診リンクの発行</h2>
<form method="post">
  <input type="text" name="patient_id" placeholder="患者ID (例: 2)" required>
  <button type="submit">発行</button>
</form>
{% if link %}
  <div class="result">
    <p>患者ID <b>{{ patient_id }}</b> 用のリンクを発行しました。</p>
    <p><a href="{{ link }}" target="_blank">{{ link }}</a></p>
    <img src="{{ qr_url }}" width="160" height="160" alt="QRコード">
    <p style="font-size:13px;color:#666;">↑ QRコードを印刷・SMS等で患者さんにお渡しください。</p>
  </div>
{% endif %}
<hr>
<h3>回答結果を見る</h3>
<form method="get" action="{{ url_for('admin_view', patient_id='__ID__') }}"
      onsubmit="this.action=this.action.replace('__ID__', document.getElementById('vid').value); return true;">
  <input id="vid" type="text" placeholder="患者ID (例: 2)" required>
  <button type="submit">表示</button>
</form>
</body></html>
"""


@app.route("/admin/", methods=["GET", "POST"])
@login_required
def admin_dashboard():
    link = None
    qr_url = None
    patient_id = None
    if request.method == "POST":
        patient_id = request.form.get("patient_id", "").strip()
        if patient_id:
            token = create_token(patient_id)
            link = request.host_url.rstrip("/") + f"/form/{token}"
            qr_url = url_for("qr_image", token=token)
    return render_template_string(
        DASHBOARD_PAGE, link=link, qr_url=qr_url, patient_id=patient_id
    )


@app.route("/admin/qr/<token>.png")
@login_required
def qr_image(token):
    link = request.host_url.rstrip("/") + f"/form/{token}"
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


VIEW_PAGE = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>問診結果</title>
<style>
  body{font-family:sans-serif;margin:0;padding:16px;background:#fffff2;color:#111;font-size:14px;}
  h2{margin:0 0 4px;font-size:17px;}
  .meta{color:#666;font-size:12px;margin-bottom:12px;}
  table{width:100%;border-collapse:collapse;}
  td,th{border:1px solid #ddd;padding:7px 9px;vertical-align:top;text-align:left;}
  th{background:#f3f3f3;width:32%;white-space:nowrap;}
  .empty{color:#999;text-align:center;padding:40px 0;}
  .top a{font-size:12px;margin-right:10px;}
</style></head><body>
<div class="top">
  <a href="{{ url_for('admin_dashboard') }}">← リンク発行に戻る</a>
  <a href="{{ url_for('admin_history', patient_id=patient_id) }}">過去問診一覧</a>
</div>
{% if not records %}
  <div class="empty">この患者(ID: {{ patient_id }})の問診回答はまだありません。</div>
{% else %}
  <h2>問診結果(患者ID: {{ patient_id }})</h2>
  <div class="meta">回答日時: {{ latest.submitted_at }}{% if is_latest %}（全{{ records|length }}件中 最新）{% else %}（過去の回答を表示中）{% endif %}</div>
  <table>
    {% for f in fields %}
      <tr><th>{{ f.label }}</th><td>{{ format_value(f, latest.get(f.key, "")) }}</td></tr>
    {% endfor %}
  </table>
{% endif %}
</body></html>
"""


HISTORY_PAGE = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>過去問診一覧</title>
<style>
  body{font-family:sans-serif;margin:0;padding:16px;background:#fffff2;color:#111;font-size:14px;}
  h2{margin:0 0 12px;font-size:17px;}
  .top a{font-size:12px;}
  ul{list-style:none;padding:0;margin:12px 0 0;}
  li{border-bottom:1px solid #ddd;padding:10px 4px;}
  li a{color:#1565c0;text-decoration:none;font-weight:bold;}
  .meta{color:#666;font-size:12px;}
  .empty{color:#999;text-align:center;padding:40px 0;}
</style></head><body>
<div class="top"><a href="{{ url_for('admin_view', patient_id=patient_id) }}">← 最新の問診に戻る</a></div>
<h2>過去問診一覧(患者ID: {{ patient_id }})</h2>
{% if not records %}
  <div class="empty">問診回答はまだありません。</div>
{% else %}
<ul>
  {% for r in records %}
    <li>
      <a href="{{ r.url }}">{{ r.submitted_at }}</a>
      <div class="meta">{{ form_labels.get(r.form_type, r.form_type) }}</div>
    </li>
  {% endfor %}
</ul>
{% endif %}
</body></html>
"""


def format_value(field, value):
    if not value:
        return "-"
    if field.get("type") == "score_radio":
        options = field.get("options") or []
        try:
            idx = int(value)
            if 0 <= idx < len(options):
                return f"{options[idx]}({idx}点)"
        except (ValueError, TypeError):
            pass
    return value


@app.route("/admin/view/<patient_id>")
@login_required
def admin_view(patient_id):
    records = load_records(patient_id)
    at = request.args.get("at")
    if at:
        record = next((r for r in records if r.get("submitted_at") == at), None)
        is_latest = False
    else:
        record = records[-1] if records else None
        is_latest = True
    fields = get_form_fields(record.get("form_type", "general")) if record else []
    return render_template_string(
        VIEW_PAGE,
        patient_id=patient_id,
        records=records,
        latest=record,
        fields=fields,
        is_latest=is_latest,
        format_value=format_value,
    )


@app.route("/admin/history/<patient_id>")
@login_required
def admin_history(patient_id):
    from urllib.parse import quote

    raw_records = list(reversed(load_records(patient_id)))
    form_labels = {key: val["label"] for key, val in FORM_TYPES.items()}
    records = [
        {
            "submitted_at": r.get("submitted_at", ""),
            "form_type": r.get("form_type", "general"),
            "url": url_for("admin_view", patient_id=patient_id)
            + "?at="
            + quote(r.get("submitted_at", "")),
        }
        for r in raw_records
    ]
    return render_template_string(
        HISTORY_PAGE, patient_id=patient_id, records=records, form_labels=form_labels
    )


# ---------------------------------------------------------------------------
# 受付フロー: ①QRスキャン → ②N2017.cgiへ遷移 → ③ブックマークレットで登録
#            → ④確認画面(氏名+問診QR)
# ---------------------------------------------------------------------------

CHECKIN_SCAN_PAGE = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>受付QRスキャン</title>
<style>
  body{font-family:sans-serif;margin:0;background:#111;color:#fff;
       display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;}
  video{width:100%;max-width:420px;border-radius:8px;}
  canvas{display:none;}
  p{padding:0 16px;text-align:center;font-size:14px;color:#ccc;}
  #status{margin-top:10px;font-size:14px;color:#8f8;}
</style></head><body>
<h3>診察券のQRを読み込んでください</h3>
<video id="video" playsinline autoplay muted></video>
<canvas id="canvas"></canvas>
<p id="status">カメラを起動しています…</p>
<script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js"></script>
<script>
const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
const statusEl = document.getElementById('status');
const RS_BASE_URL_TEMPLATE = {{ rs_base_url_template|tojson }};

let handled = false;

navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } })
  .then((stream) => {
    video.srcObject = stream;
    statusEl.textContent = "QRコードをカメラに映してください";
    requestAnimationFrame(tick);
  })
  .catch((err) => {
    statusEl.textContent = "カメラを起動できませんでした: " + err.message;
  });

function tick() {
  if (handled) return;
  if (video.readyState === video.HAVE_ENOUGH_DATA) {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const code = jsQR(imageData.data, imageData.width, imageData.height);
    if (code && code.data) {
      const m = code.data.match(/\\d+/);
      if (m) {
        handled = true;
        statusEl.textContent = "患者ID " + m[0] + " を検出。移動します…";
        const url = RS_BASE_URL_TEMPLATE.replace("{id}", m[0]);
        setTimeout(() => { location.href = url; }, 400);
        return;
      }
    }
  }
  requestAnimationFrame(tick);
}
</script>
</body></html>
"""


@app.route("/checkin")
def checkin_scan():
    return render_template_string(
        CHECKIN_SCAN_PAGE, rs_base_url_template=RS_BASE_URL_TEMPLATE
    )


def _cors(resp):
    if isinstance(resp, tuple):
        resp = app.make_response(resp)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Key"
    return resp


@app.route("/checkin/register", methods=["POST", "OPTIONS"])
def checkin_register():
    if request.method == "OPTIONS":
        return _cors(app.make_default_options_response())

    if request.headers.get("X-Api-Key") != REGISTER_API_KEY:
        return _cors(("unauthorized", 401))

    payload = request.get_json(silent=True) or request.form
    patient_id = str(payload.get("id", "")).strip()
    name = str(payload.get("name", "")).strip()
    dob = str(payload.get("dob", "")).strip()
    gender = str(payload.get("gender", "")).strip()
    form_type = str(payload.get("type", "general")).strip() or "general"

    if not patient_id:
        return _cors(("patient id required", 400))

    upsert_directory(patient_id, name, dob, gender)
    token = create_token(patient_id, form_type)

    resp = jsonify(
        {
            "token": token,
            "confirm_url": request.host_url.rstrip("/") + f"/checkin/confirm?token={token}",
        }
    )
    return _cors(resp)


@app.route("/checkin/types")
def checkin_types():
    types = [{"id": key, "label": val["label"]} for key, val in FORM_TYPES.items()]
    return _cors(jsonify({"types": types}))


# ---------------------------------------------------------------------------
# 問診管理アプリ(スタッフ用)向けAPI
# ---------------------------------------------------------------------------

def _staff_authorized() -> bool:
    return request.headers.get("X-Staff-Key") == STAFF_API_KEY


@app.route("/admin/api/form_types")
def api_form_types():
    if not _staff_authorized():
        return _cors(("unauthorized", 401))
    return _cors(jsonify({"types": FORM_TYPES}))


@app.route("/admin/api/submissions")
def api_submissions():
    if not _staff_authorized():
        return _cors(("unauthorized", 401))

    date_str = request.args.get("date") or now_jst().strftime("%Y-%m-%d")
    directory = _load_directory()
    results = []

    for fname in os.listdir(DATA_DIR):
        if fname.startswith("_") or not fname.endswith(".json"):
            continue
        patient_id = fname[:-5]
        path = os.path.join(DATA_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception:
            continue

        for record in records:
            submitted_at = record.get("submitted_at", "")
            if not submitted_at.startswith(date_str):
                continue
            form_type = record.get("form_type", "general")
            fields_only = {
                k: v for k, v in record.items()
                if k not in ("submitted_at", "form_type", "confirmed")
            }
            dir_entry = directory.get(patient_id, {})
            results.append(
                {
                    "patient_id": patient_id,
                    "name": dir_entry.get("name", ""),
                    "dob": dir_entry.get("dob", ""),
                    "submitted_at": submitted_at,
                    "form_type": form_type,
                    "confirmed": bool(record.get("confirmed", False)),
                    "fields": fields_only,
                }
            )

    results.sort(key=lambda r: r.get("submitted_at", ""), reverse=True)
    return _cors(jsonify({"date": date_str, "submissions": results}))


@app.route("/admin/api/confirm", methods=["POST", "OPTIONS"])
def api_confirm():
    if request.method == "OPTIONS":
        return _cors(app.make_default_options_response())
    if not _staff_authorized():
        return _cors(("unauthorized", 401))

    payload = request.get_json(silent=True) or {}
    patient_id = str(payload.get("patient_id", "")).strip()
    submitted_at = str(payload.get("submitted_at", "")).strip()
    if not patient_id or not submitted_at:
        return _cors(("patient_id and submitted_at required", 400))

    records = load_records(patient_id)
    changed = False
    for r in records:
        if r.get("submitted_at") == submitted_at:
            r["confirmed"] = True
            changed = True

    if changed:
        with open(data_path(patient_id), "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=2)

    return _cors(jsonify({"ok": changed}))


CHECKIN_CONFIRM_PAGE = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>受付確認</title>
<style>
  body{font-family:sans-serif;margin:0;padding:24px;text-align:center;background:#f4f6f8;}
  .name{font-size:26px;font-weight:bold;margin:14px 0 4px;}
  .dob{color:#555;font-size:15px;margin-bottom:20px;}
  img{border:8px solid #fff;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.15);}
  .hint{color:#666;font-size:13px;margin-top:16px;}
  .error{color:#c62828;}
</style></head><body>
{% if not entry %}
  <p class="error">情報を取得できませんでした。もう一度お試しください。</p>
{% else %}
  <p>お名前をご確認ください</p>
  <div class="name">{{ entry.name or "(氏名未取得)" }}</div>
  <div class="dob">{{ entry.dob or "" }}</div>
  <img src="{{ qr_url }}" width="220" height="220" alt="問診QRコード">
  <p class="hint">お名前に間違いがなければ、ご自身のスマートフォンで<br>上のQRコードを読み取って問診にお進みください。</p>
{% endif %}
</body></html>
"""


@app.route("/checkin/confirm")
def checkin_confirm():
    token = request.args.get("token", "").strip()
    patient_id = resolve_token(token)
    entry = lookup_directory(patient_id) if patient_id else None
    qr_url = url_for("checkin_qr_image", token=token)
    return render_template_string(CHECKIN_CONFIRM_PAGE, entry=entry, qr_url=qr_url)


@app.route("/checkin/qr/<token>.png")
def checkin_qr_image(token):
    link = request.host_url.rstrip("/") + f"/form/{token}"
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ---------------------------------------------------------------------------
# 患者用フォーム(ログイン不要・トークンで特定)
# ---------------------------------------------------------------------------

FORM_PAGE = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>問診票</title>
<style>
  *{box-sizing:border-box;}
  body{font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif;background:#f4f6f8;
       margin:0;padding:16px;color:#222;}
  h1{font-size:20px;text-align:center;margin:8px 0 20px;}
  form{max-width:640px;margin:0 auto;}
  .field{background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:12px;
         box-shadow:0 1px 3px rgba(0,0,0,0.08);}
  label.field-label{display:block;font-weight:bold;margin-bottom:8px;font-size:15px;}
  textarea,input[type=text]{width:100%;font-size:16px;padding:10px;border:1px solid #ccc;
       border-radius:6px;font-family:inherit;}
  textarea{min-height:70px;resize:vertical;}
  .radio-group{display:flex;flex-wrap:wrap;gap:8px;}
  .radio-group label{flex:1 1 auto;text-align:center;padding:10px 12px;border:1px solid #bbb;
       border-radius:20px;background:#fafafa;cursor:pointer;font-size:14px;user-select:none;}
  .radio-group input{display:none;}
  .radio-group label:has(input:checked){background:#2e7d32;color:#fff;border-color:#2e7d32;}
  .checkbox-group{display:flex;flex-direction:column;gap:8px;}
  .checkbox-group label{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid #bbb;
       border-radius:10px;background:#fafafa;cursor:pointer;font-size:14px;}
  .checkbox-group input{width:18px;height:18px;}
  .checkbox-group label:has(input:checked){background:#e8f5e9;border-color:#2e7d32;}
  .score-question{margin-bottom:14px;}
  .score-question .q-text{font-weight:bold;margin-bottom:6px;display:block;}
  button.submit-btn{display:block;width:100%;padding:16px;font-size:18px;font-weight:bold;
       color:#fff;background:#1565c0;border:none;border-radius:10px;margin-top:20px;cursor:pointer;}
  .done{max-width:640px;margin:60px auto;text-align:center;font-size:18px;}
</style></head><body>
{% if saved %}
  <div class="done"><p>✅ ご回答ありがとうございました。</p><p>受付にお声がけください。</p></div>
{% elif error %}
  <div class="done">{{ error }}</div>
{% else %}
  <h1>問診票</h1>
  <form method="post" action="{{ url_for('submit') }}">
    <input type="hidden" name="token" value="{{ token }}">
    {% for f in fields %}
      {% if f.type != "computed" %}
      <div class="field">
        {% if f.type != "score_radio" %}
        <label class="field-label">{{ f.label }}</label>
        {% endif %}
        {% if f.type == "textarea" %}
          <textarea name="{{ f.key }}"></textarea>
        {% elif f.type == "text" %}
          <input type="text" name="{{ f.key }}">
        {% elif f.type == "radio" %}
          <div class="radio-group">
            {% for opt in f.options %}
              <label><input type="radio" name="{{ f.key }}" value="{{ opt }}"><span>{{ opt }}</span></label>
            {% endfor %}
          </div>
        {% elif f.type == "checkbox" %}
          <div class="checkbox-group">
            {% for opt in f.options %}
              <label><input type="checkbox" name="{{ f.key }}" value="{{ opt }}"><span>{{ opt }}</span></label>
            {% endfor %}
          </div>
        {% elif f.type == "score_radio" %}
          <div class="score-question">
            <span class="q-text">{{ f.label }}</span>
            <div class="radio-group">
              {% for opt in f.options %}
                <label><input type="radio" name="{{ f.key }}" value="{{ loop.index0 }}"><span>{{ opt }}</span></label>
              {% endfor %}
            </div>
          </div>
        {% endif %}
      </div>
      {% endif %}
    {% endfor %}
    <button type="submit" class="submit-btn">回答を送信する</button>
  </form>
{% endif %}
</body></html>
"""


UROLOGY_FORM_PAGE = """
<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>泌尿器科 一般問診</title>
<style>
  *{box-sizing:border-box;}
  body{font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif;background:#f4f6f8;
       margin:0;padding:16px;color:#222;}
  h1{font-size:19px;text-align:center;margin:8px 0 20px;}
  form{max-width:640px;margin:0 auto;}
  .section{background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:12px;
         box-shadow:0 1px 3px rgba(0,0,0,0.08);}
  .section.hidden{display:none;}
  label.field-label{display:block;font-weight:bold;margin-bottom:8px;font-size:14px;}
  .sub-label{display:block;font-weight:normal;margin:10px 0 6px;font-size:13px;color:#555;}
  textarea,input[type=text],input[type=number],input[type=date]{width:100%;font-size:15px;padding:9px;
       border:1px solid #ccc;border-radius:6px;font-family:inherit;}
  textarea{min-height:60px;resize:vertical;}
  .radio-group{display:flex;flex-wrap:wrap;gap:8px;}
  .radio-group label{flex:1 1 auto;text-align:center;padding:9px 10px;border:1px solid #bbb;
       border-radius:20px;background:#fafafa;cursor:pointer;font-size:13px;user-select:none;}
  .radio-group input{display:none;}
  .radio-group label.checked{background:#2e7d32;color:#fff;border-color:#2e7d32;}
  .checkbox-group{display:flex;flex-direction:column;gap:6px;}
  .checkbox-group label{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid #bbb;
       border-radius:8px;background:#fafafa;cursor:pointer;font-size:13px;}
  .checkbox-group input{width:16px;height:16px;flex-shrink:0;}
  .checkbox-group label.checked{background:#e8f5e9;border-color:#2e7d32;}
  .sub-fields{margin-top:10px;padding:10px;background:#f7f7f2;border-radius:8px;display:none;}
  .sub-fields.show{display:block;}
  .row2{display:flex;gap:8px;}
  .row2 .field-mini{flex:1;}
  .field-mini label{display:block;font-size:12px;color:#666;margin-bottom:4px;}
  .hint{font-size:12px;color:#777;margin-bottom:8px;}
  .score-q{margin-bottom:12px;}
  .score-q .q-text{font-weight:bold;font-size:13px;display:block;margin-bottom:6px;}
  .score-total{background:#eef4ff;border-radius:8px;padding:10px;margin-top:10px;font-size:13px;
       display:flex;justify-content:space-between;font-weight:bold;color:#0c447c;}
  button.submit-btn{display:block;width:100%;padding:14px;font-size:16px;font-weight:bold;
       color:#fff;background:#1565c0;border:none;border-radius:10px;margin-top:16px;cursor:pointer;}
  .req{color:#c62828;font-size:12px;margin-left:4px;}
  .invalid{outline:2px solid #c62828;outline-offset:2px;}
  .error-msg{color:#c62828;font-size:12px;margin-top:6px;display:none;}
  .error-msg.show{display:block;}
  .done{max-width:640px;margin:60px auto;text-align:center;font-size:18px;}
</style></head><body>
{% if saved %}
  <div class="done"><p>✅ ご回答ありがとうございました。</p><p>受付にお声がけください。</p></div>
{% elif error %}
  <div class="done">{{ error }}</div>
{% else %}
<h1>泌尿器科 一般問診</h1>
<form method="post" action="{{ url_for('submit_urology') }}" id="uroForm">
  <input type="hidden" name="token" value="{{ token }}">

  <div class="section">
    <label class="field-label">お手元の番号札の番号を入力してください<span class="req">必須</span></label>
    <input type="number" name="ticketNumber" id="ticketNumber" inputmode="numeric" placeholder="例: 12">
    <div class="error-msg" id="err_ticketNumber">番号札の番号を入力してください</div>
  </div>

  <div class="section">
    <label class="field-label">治療中もしくは過去に治療をした病気はありますか<span class="req">必須</span></label>
    <div class="radio-group" data-group="pastIllnessStatus">
      <label><input type="radio" name="pastIllnessStatus" value="前回受診時と同様"><span>前回受診時と同様</span></label>
      <label><input type="radio" name="pastIllnessStatus" value="ない"><span>ない</span></label>
      <label><input type="radio" name="pastIllnessStatus" value="ある"><span>ある</span></label>
    </div>
    <div class="error-msg" id="err_pastIllnessStatus">回答を選択してください</div>
    <div class="sub-fields" id="pastIllnessSub">
      <div class="checkbox-group" id="pastIllnessList">
        <label><input type="checkbox" name="pastIllnessItems" value="尿管結石"><span>尿管結石</span></label>
        <label><input type="checkbox" name="pastIllnessItems" value="緑内障"><span>緑内障</span></label>
        <label><input type="checkbox" name="pastIllnessItems" value="糖尿病"><span>糖尿病</span></label>
        <label><input type="checkbox" name="pastIllnessItems" value="高血圧"><span>高血圧</span></label>
        <label><input type="checkbox" name="pastIllnessItems" value="狭心症"><span>狭心症</span></label>
        <label><input type="checkbox" name="pastIllnessItems" value="不整脈"><span>不整脈</span></label>
        <label><input type="checkbox" name="pastIllnessItems" value="脳卒中"><span>脳卒中</span></label>
        <label><input type="checkbox" name="pastIllnessItems" value="透析"><span>透析</span></label>
        <label class="other-toggle"><input type="checkbox" name="pastIllnessItems" value="その他"><span>その他</span></label>
      </div>
      <input type="text" name="pastIllnessOtherDetail" id="pastIllnessOtherDetail" placeholder="その他の病名を入力" style="margin-top:8px;display:none;">
    </div>
  </div>

  <div class="section">
    <label class="field-label">現在飲んでいるお薬はありますか<span class="req">必須</span></label>
    <div class="radio-group" data-group="medicationStatus">
      <label><input type="radio" name="medicationStatus" value="前回受診時と同様"><span>前回受診時と同様</span></label>
      <label><input type="radio" name="medicationStatus" value="ない"><span>ない</span></label>
      <label><input type="radio" name="medicationStatus" value="ある"><span>ある</span></label>
    </div>
    <div class="error-msg" id="err_medicationStatus">回答を選択してください</div>
    <div class="sub-fields" id="medicationSub">
      <span class="sub-label">お薬手帳を提出しましたか</span>
      <div class="radio-group" data-group="medicationBook">
        <label><input type="radio" name="medicationBook" value="はい"><span>はい</span></label>
        <label><input type="radio" name="medicationBook" value="いいえ"><span>いいえ</span></label>
      </div>
      <div class="sub-fields" id="medicationDetailSub">
        <input type="text" name="medicationDetail" id="medicationDetail" placeholder="薬品名をご記入ください">
      </div>
    </div>
  </div>

  <div class="section hidden" id="agaSection">
    <label class="field-label">AGA(男性型脱毛症)の治療薬を服用していますか<span class="req">必須</span></label>
    <div class="radio-group" data-group="agaStatus">
      <label><input type="radio" name="agaStatus" value="いいえ"><span>いいえ</span></label>
      <label><input type="radio" name="agaStatus" value="はい"><span>はい</span></label>
    </div>
    <div class="error-msg" id="err_agaStatus">回答を選択してください</div>
    <div class="sub-fields" id="agaSub">
      <input type="text" name="agaDetail" id="agaDetail" placeholder="薬品名をご記入ください">
    </div>
  </div>

  <div class="section">
    <label class="field-label">ご家族(血縁者)にがんの方はいますか<span class="req">必須</span></label>
    <div class="radio-group" data-group="familyCancerStatus">
      <label><input type="radio" name="familyCancerStatus" value="前回受診時と同様"><span>前回受診時と同様</span></label>
      <label><input type="radio" name="familyCancerStatus" value="いない"><span>いない</span></label>
      <label><input type="radio" name="familyCancerStatus" value="はい"><span>はい</span></label>
    </div>
    <div class="error-msg" id="err_familyCancerStatus">回答を選択してください</div>
    <div class="sub-fields" id="familyCancerSub">
      <div class="checkbox-group" id="familyCancerList">
        <label><input type="checkbox" name="familyCancerItems" value="前立腺がん"><span>前立腺がん</span></label>
        <label><input type="checkbox" name="familyCancerItems" value="膵がん"><span>膵がん</span></label>
        <label><input type="checkbox" name="familyCancerItems" value="乳がん"><span>乳がん</span></label>
        <label><input type="checkbox" name="familyCancerItems" value="卵巣がん"><span>卵巣がん</span></label>
        <label class="other-toggle"><input type="checkbox" name="familyCancerItems" value="その他"><span>その他</span></label>
      </div>
      <input type="text" name="familyCancerOtherDetail" id="familyCancerOtherDetail" placeholder="その他の詳細" style="margin-top:8px;display:none;">
    </div>
  </div>

  <div class="section">
    <label class="field-label">薬のアレルギーはありますか<span class="req">必須</span></label>
    <div class="radio-group" data-group="allergyStatus">
      <label><input type="radio" name="allergyStatus" value="前回受診時と同様"><span>前回受診時と同様</span></label>
      <label><input type="radio" name="allergyStatus" value="ない"><span>ない</span></label>
      <label><input type="radio" name="allergyStatus" value="ある"><span>ある</span></label>
    </div>
    <div class="error-msg" id="err_allergyStatus">回答を選択してください</div>
    <div class="sub-fields" id="allergySub">
      <input type="text" name="allergyDetail" id="allergyDetail" placeholder="薬品名をご記入ください">
    </div>
  </div>

  <div class="section hidden" id="lifestyleSection">
    <label class="field-label">飲酒・喫煙について<span class="req">必須</span></label>
    <span class="sub-label">飲酒はされますか</span>
    <div class="radio-group" data-group="alcohol">
      <label><input type="radio" name="alcohol" value="飲まない"><span>飲まない</span></label>
      <label><input type="radio" name="alcohol" value="たまに飲む"><span>たまに飲む</span></label>
      <label><input type="radio" name="alcohol" value="ほぼ毎日飲む"><span>ほぼ毎日飲む</span></label>
    </div>
    <div class="error-msg" id="err_alcohol">回答を選択してください</div>
    <span class="sub-label">喫煙はされますか</span>
    <div class="radio-group" data-group="smoking">
      <label><input type="radio" name="smoking" value="吸わない"><span>吸わない</span></label>
      <label><input type="radio" name="smoking" value="吸う"><span>吸う</span></label>
      <label><input type="radio" name="smoking" value="禁煙中"><span>禁煙中</span></label>
    </div>
    <div class="error-msg" id="err_smoking">回答を選択してください</div>
    <div class="sub-fields" id="smokeActiveSub">
      <div class="row2">
        <div class="field-mini"><label>1日平均(本)</label><input type="number" name="smokeActivePerDay" id="smokeActivePerDay"></div>
        <div class="field-mini"><label>開始年齢(歳)</label><input type="number" name="smokeActiveStartAge" id="smokeActiveStartAge"></div>
        <div class="field-mini"><label>喫煙年数(自動計算)</label><input type="text" name="smokeActiveYears" id="smokeActiveYears" readonly></div>
      </div>
    </div>
    <div class="sub-fields" id="smokeQuitSub">
      <div class="row2">
        <div class="field-mini"><label>1日平均(本)</label><input type="number" name="smokeQuitPerDay" id="smokeQuitPerDay"></div>
        <div class="field-mini"><label>開始年齢(歳)</label><input type="number" name="smokeQuitStartAge" id="smokeQuitStartAge"></div>
        <div class="field-mini"><label>終了年齢(歳)</label><input type="number" name="smokeQuitEndAge" id="smokeQuitEndAge"></div>
      </div>
      <div class="field-mini" style="margin-top:8px;max-width:160px;"><label>喫煙年数(自動計算)</label><input type="text" name="smokeQuitYears" id="smokeQuitYears" readonly></div>
    </div>
  </div>

  <div class="section hidden" id="femaleSection">
    <label class="field-label">女性の方にお聞きします<span class="req">必須</span></label>
    <span class="sub-label">妊娠中ですか</span>
    <div class="radio-group" data-group="pregnant">
      <label><input type="radio" name="pregnant" value="いいえ"><span>いいえ</span></label>
      <label><input type="radio" name="pregnant" value="可能性あり"><span>可能性あり</span></label>
      <label><input type="radio" name="pregnant" value="はい"><span>はい</span></label>
    </div>
    <div class="error-msg" id="err_pregnant">回答を選択してください</div>
    <div class="sub-fields" id="pregnantWeekSub">
      <div class="field-mini" style="max-width:160px;"><label>妊娠週数(週目)</label><input type="number" name="pregnantWeek" id="pregnantWeek"></div>
    </div>
    <span class="sub-label">授乳中ですか</span>
    <div class="radio-group" data-group="breastfeeding">
      <label><input type="radio" name="breastfeeding" value="いいえ"><span>いいえ</span></label>
      <label><input type="radio" name="breastfeeding" value="はい"><span>はい</span></label>
    </div>
    <div class="error-msg" id="err_breastfeeding">回答を選択してください</div>
    <span class="sub-label">生理中ですか</span>
    <div class="radio-group" data-group="menstruating">
      <label><input type="radio" name="menstruating" value="いいえ"><span>いいえ</span></label>
      <label><input type="radio" name="menstruating" value="はい"><span>はい</span></label>
    </div>
    <div class="error-msg" id="err_menstruating">回答を選択してください</div>
  </div>

  <div class="section">
    <label class="field-label">症状はいつからですか<span class="req">必須</span></label>
    <input type="text" name="symptomOnset" id="symptomOnset" placeholder="例: 3日前から、1週間前から">
    <div class="error-msg" id="err_symptomOnset">症状はいつからか入力してください</div>
  </div>

  <div class="section">
    <label class="field-label">熱はありますか<span class="req">必須</span></label>
    <div class="radio-group" data-group="feverStatus">
      <label><input type="radio" name="feverStatus" value="ない"><span>ない</span></label>
      <label><input type="radio" name="feverStatus" value="ある"><span>ある</span></label>
    </div>
    <div class="error-msg" id="err_feverStatus">回答を選択してください</div>
    <div class="sub-fields" id="feverSub">
      <div class="row2">
        <div class="field-mini"><label>いつから</label><input type="date" name="feverFrom" id="feverFrom"></div>
        <div class="field-mini"><label>いつまで</label><input type="date" name="feverTo" id="feverTo"></div>
      </div>
      <div class="field-mini" style="margin-top:8px;max-width:160px;"><label>最高体温(℃)</label><input type="number" step="0.1" name="feverMaxTemp" id="feverMaxTemp"></div>
    </div>
  </div>

  <div class="section">
    <label class="field-label">今日はどうされましたか(当てはまるものをすべて選択)<span class="req">必須</span></label>

    <span class="sub-label">尿のトラブル</span>
    <div class="checkbox-group">
      <label class="symptom-item" data-key="urinaryIncontinence"><input type="checkbox" name="symptoms" value="尿が漏れる"><span>尿が漏れる</span></label>
      <label class="symptom-item" data-key="residualUrine"><input type="checkbox" name="symptoms" value="残尿感がある"><span>残尿感がある</span></label>
      <label class="symptom-item" data-key="difficultyUrinating"><input type="checkbox" name="symptoms" value="尿が出にくい"><span>尿が出にくい</span></label>
      <label class="symptom-item" data-key="frequentUrination"><input type="checkbox" name="symptoms" value="頻尿"><span>頻尿</span></label>
      <label class="symptom-item" data-key="painfulUrination"><input type="checkbox" name="symptoms" value="排尿時の痛み"><span>排尿時の痛み</span></label>
      <label class="symptom-item" data-key="urethralDischarge"><input type="checkbox" name="symptoms" value="尿道の違和感・膿が出る"><span>尿道の違和感・膿が出る</span></label>
    </div>
    <div class="error-msg" id="err_symptoms">少なくとも1つ選択してください</div>

    <span class="sub-label">その他の症状</span>
    <div class="checkbox-group">
      <label class="symptom-item" data-key="backPain"><input type="checkbox" name="symptoms" value="背中の痛み"><span>背中の痛み</span></label>
    </div>
    <div class="sub-fields" id="backPainSub">
      <div class="radio-group" data-group="backPainSide">
        <label><input type="radio" name="backPainSide" value="左"><span>左</span></label>
        <label><input type="radio" name="backPainSide" value="右"><span>右</span></label>
        <label><input type="radio" name="backPainSide" value="全体"><span>全体</span></label>
      </div>
    </div>

    <div class="checkbox-group" style="margin-top:6px;">
      <label class="symptom-item" data-key="groinPain"><input type="checkbox" name="symptoms" value="鼠径部の痛み"><span>鼠径部の痛み</span></label>
      <label class="symptom-item" data-key="bloodUrine"><input type="checkbox" name="symptoms" value="血尿"><span>血尿</span></label>
      <label class="symptom-item" data-key="testicleDiscomfort"><input type="checkbox" name="symptoms" value="睾丸の違和感"><span>睾丸の違和感</span></label>
    </div>
    <div class="sub-fields" id="testicleDiscomfortSub">
      <div class="radio-group" data-group="testicleDiscomfortType">
        <label><input type="radio" name="testicleDiscomfortType" value="腫れ"><span>腫れ</span></label>
        <label><input type="radio" name="testicleDiscomfortType" value="痛み"><span>痛み</span></label>
      </div>
    </div>

    <div class="checkbox-group" style="margin-top:6px;">
      <label class="symptom-item" data-key="semenBlood"><input type="checkbox" name="symptoms" value="精液に血が混じる"><span>精液に血が混じる</span></label>
      <label class="symptom-item" data-key="stdConcern"><input type="checkbox" name="symptoms" value="性感染症が気になる"><span>性感染症が気になる</span></label>
    </div>
    <div class="sub-fields" id="stdConcernSub">
      <div class="checkbox-group">
        <label class="std-detail"><input type="checkbox" name="stdConcernDetail" value="気になる症状がある"><span>気になる症状がある</span></label>
        <label class="std-detail"><input type="checkbox" name="stdConcernDetail" value="症状はない"><span>症状はない</span></label>
        <label class="std-detail std-partner"><input type="checkbox" name="stdConcernDetail" value="パートナーが性感染症に罹った"><span>パートナーが性感染症に罹った</span></label>
      </div>
      <div class="sub-fields" id="stdDiseaseDetailSub">
        <input type="text" name="stdDiseaseDetail" id="stdDiseaseDetail" placeholder="病名がわかる場合は入力してください">
      </div>
    </div>

    <div class="checkbox-group" style="margin-top:6px;">
      <label class="symptom-item" data-key="dischargeAbnormality"><input type="checkbox" name="symptoms" value="おりものの異常"><span>おりものの異常</span></label>
      <label class="symptom-item" data-key="genitalItch"><input type="checkbox" name="symptoms" value="陰部のかゆみ"><span>陰部のかゆみ</span></label>
      <label class="symptom-item" data-key="maleMenopause"><input type="checkbox" name="symptoms" value="男性更年期が気になる"><span>男性更年期が気になる</span></label>
      <label class="symptom-item" data-key="checkupAbnormality"><input type="checkbox" name="symptoms" value="健康診断で異常を指摘された"><span>健康診断で異常を指摘された</span></label>
    </div>
    <div class="sub-fields" id="checkupAbnormalitySub">
      <input type="text" name="checkupAbnormalityDetail" id="checkupAbnormalityDetail" placeholder="指摘された項目がわかる場合は入力してください">
    </div>

    <span class="sub-label">自由診療</span>
    <div class="checkbox-group">
      <label class="symptom-item" data-key="bridalCheck"><input type="checkbox" name="symptoms" value="ブライダルチェック(自費の性感染症検査)"><span>ブライダルチェック(自費の性感染症検査)</span></label>
      <label class="symptom-item" data-key="semenTest"><input type="checkbox" name="symptoms" value="精液検査"><span>精液検査</span></label>
      <label class="symptom-item" data-key="ed"><input type="checkbox" name="symptoms" value="ED(勃起不全)"><span>ED(勃起不全)</span></label>
      <label class="symptom-item" data-key="agaSymptom"><input type="checkbox" name="symptoms" value="AGA(男性型脱毛症)"><span>AGA(男性型脱毛症)</span></label>
      <label class="symptom-item" data-key="hairLoss"><input type="checkbox" name="symptoms" value="脱毛"><span>脱毛</span></label>
      <label class="symptom-item" data-key="freeInjection"><input type="checkbox" name="symptoms" value="自由注射"><span>自由注射</span></label>
    </div>
    <div class="sub-fields" id="freeInjectionSub">
      <div class="checkbox-group">
        <label><input type="checkbox" name="freeInjectionItems" value="にんにく注射"><span>にんにく注射</span></label>
        <label><input type="checkbox" name="freeInjectionItems" value="プラセンタ注射"><span>プラセンタ注射</span></label>
        <label><input type="checkbox" name="freeInjectionItems" value="白玉注射"><span>白玉注射</span></label>
      </div>
    </div>
  </div>

  <div class="section">
    <label class="field-label">その他、気になる症状や相談したい事柄がありましたら入力してください</label>
    <textarea name="freeNote" id="freeNote"></textarea>
  </div>

  <div class="section hidden" id="voidingTriggerFinalSection">
    <label class="field-label" id="voidingTriggerFinalLabel">の症状は以前から気になりますか<span class="req">必須</span></label>
    <div class="radio-group" data-group="voidingOneWeekPlus">
      <label><input type="radio" name="voidingOneWeekPlus" value="いいえ"><span>いいえ</span></label>
      <label><input type="radio" name="voidingOneWeekPlus" value="はい"><span>はい</span></label>
    </div>
    <div class="error-msg" id="err_voidingOneWeekPlus">回答を選択してください</div>
  </div>

  <div class="section hidden" id="voidingSection">
    <label class="field-label">排尿チェックシート(IPSS / OABSS)</label>
    <div class="hint">この1週間の状態について、最も近いものを選んでください。</div>
    <div id="ipssContainer"></div>
    <div class="score-total"><span>IPSS合計点</span><span id="ipssTotal">0 / 35点</span></div>
    <div class="score-total"><span>QOLスコア</span><span id="ipssQol">0 / 6点</span></div>
    <div id="oabssContainer" style="margin-top:16px;"></div>
    <div class="score-total"><span>OABSS合計点</span><span id="oabssTotal">0 / 15点</span></div>
  </div>

  <button type="submit" class="submit-btn">回答を送信する</button>
</form>
{% endif %}

<script>
const REAL_GENDER = {{ gender|tojson }};
const REAL_AGE = {{ age }};

function $(id){ return document.getElementById(id); }
function radioValue(name){
  const el = document.querySelector(`input[name="${name}"]:checked`);
  return el ? el.value : null;
}
function toggle(el, show){ if (el) el.classList.toggle('show', show); }
function toggleSection(el, show){ if (el) el.classList.toggle('hidden', !show); }
function checkedOf(key){
  const item = document.querySelector(`.symptom-item[data-key="${key}"]`);
  return item ? item.querySelector('input').checked : false;
}
function showError(key, show){
  const err = $('err_' + key);
  if (err) err.classList.toggle('show', show);
}

document.querySelectorAll('.radio-group').forEach(group => {
  group.addEventListener('click', e => {
    const label = e.target.closest('label');
    if (!label) return;
    group.querySelectorAll('label').forEach(l => l.classList.remove('checked'));
    label.classList.add('checked');
    const key = group.dataset.group;
    if (key) showError(key, false);
    handleConditionals();
  });
});
document.querySelectorAll('.checkbox-group label').forEach(label => {
  label.addEventListener('click', () => {
    setTimeout(() => {
      const input = label.querySelector('input');
      label.classList.toggle('checked', input.checked);
      if (label.classList.contains('symptom-item')) showError('symptoms', false);
      handleConditionals();
    }, 0);
  });
});
$('ticketNumber') && $('ticketNumber').addEventListener('input', () => showError('ticketNumber', false));
$('symptomOnset') && $('symptomOnset').addEventListener('input', () => showError('symptomOnset', false));

document.querySelectorAll('#pastIllnessList .other-toggle input').forEach(el => {
  el.addEventListener('change', () => {
    $('pastIllnessOtherDetail').style.display = el.checked ? 'block' : 'none';
  });
});
document.querySelectorAll('#familyCancerList .other-toggle input').forEach(el => {
  el.addEventListener('change', () => {
    $('familyCancerOtherDetail').style.display = el.checked ? 'block' : 'none';
  });
});

const freqOptions5 = ['まったくなかった','5回に1回未満','2回に1回未満','2回に1回くらい','2回に1回以上','ほとんどいつも'];
const nightOptions = ['0回','1回','2回','3回','4回','5回以上'];
const ipssQuestions = [
  { key:'ipss_residual', label:'残尿感:排尿後に尿が残っている感じがありましたか', options: freqOptions5 },
  { key:'ipss_frequency', label:'頻尿:排尿後2時間以内にもう一度、排尿しなければならないことがありましたか', options: freqOptions5 },
  { key:'ipss_intermittency', label:'尿線途絶:排尿の途中で尿が途切れることがありましたか', options: freqOptions5 },
  { key:'ipss_urgency', label:'尿意切迫感:尿を我慢するのが難しいことがありましたか', options: freqOptions5 },
  { key:'ipss_weak_stream', label:'尿線細少:尿の勢いが弱いことがありましたか', options: freqOptions5 },
  { key:'ipss_straining', label:'腹圧排尿:尿を出し始めるためにお腹に力を入れることがありましたか', options: freqOptions5 },
  { key:'ipss_nocturia', label:'夜間頻尿:夜寝てから朝起きるまでに、何回くらい排尿に起きましたか', options: nightOptions }
];
const ipssQolQ = { key:'ipss_qol', label:'QOL:現在の尿の状態がこのまま変わらずに続くとしたら、あなたはどう思いますか',
  options:['とても満足','満足','まあ満足','どちらともいえない','やや不満','いやだ','とても悪い'] };
const oabssQuestions = [
  { key:'oabss_daytime', label:'朝起きた時から夜寝るまでに、何回くらい尿をしましたか(頻度尿)', options:['7回以下','8〜14回','15回以上'] },
  { key:'oabss_nighttime', label:'夜寝てから朝起きるまでに、何回くらい尿をするために起きましたか(夜間排尿)', options:['0回','1回','2回','3回以上'] },
  { key:'oabss_urgency', label:'急に尿がしたくなり、我慢が難しいことがありましたか(尿意切迫感)', options:['なし','週に1回より少ない','週に1回以上','1日1回くらい','1日2〜4回','1日5回以上'] },
  { key:'oabss_incontinence', label:'急に尿意を感じ、我慢できずに尿が漏れることがありましたか(切迫性尿失禁)', options:['なし','週に1回より少ない','週に1回以上','1日1回くらい','1日2〜4回','1日5回以上'] }
];

function buildScoreQuestions(container, questions){
  if (!container) return;
  container.innerHTML = '';
  questions.forEach(q => {
    const div = document.createElement('div');
    div.className = 'score-q';
    let html = `<span class="q-text">${q.label}</span><div class="radio-group" data-group="${q.key}">`;
    q.options.forEach((opt, i) => {
      html += `<label><input type="radio" name="${q.key}" value="${i}"><span>${opt}</span></label>`;
    });
    html += '</div>';
    div.innerHTML = html;
    container.appendChild(div);
  });
  container.querySelectorAll('.radio-group').forEach(group => {
    group.addEventListener('click', e => {
      const label = e.target.closest('label');
      if (!label) return;
      group.querySelectorAll('label').forEach(l => l.classList.remove('checked'));
      label.classList.add('checked');
      updateScores();
    });
  });
}
if ($('ipssContainer')) {
  buildScoreQuestions($('ipssContainer'), ipssQuestions.concat([ipssQolQ]));
  buildScoreQuestions($('oabssContainer'), oabssQuestions);
}

function sumScore(questions){
  let total = 0;
  questions.forEach(q => {
    const v = radioValue(q.key);
    if (v !== null) total += parseInt(v, 10);
  });
  return total;
}
function updateScores(){
  $('ipssTotal').textContent = sumScore(ipssQuestions) + ' / 35点';
  const qol = radioValue('ipss_qol');
  $('ipssQol').textContent = (qol === null ? 0 : qol) + ' / 6点';
  $('oabssTotal').textContent = sumScore(oabssQuestions) + ' / 15点';
}

function calcYears(start, end){
  const s = parseInt(start, 10), e = parseInt(end, 10);
  if (isNaN(s) || isNaN(e) || e < s) return '';
  return String(e - s);
}
function updateSmokingYears(){
  $('smokeActiveYears').value = calcYears($('smokeActiveStartAge').value, REAL_AGE);
  $('smokeQuitYears').value = calcYears($('smokeQuitStartAge').value, $('smokeQuitEndAge').value);
}
['smokeActiveStartAge','smokeQuitStartAge','smokeQuitEndAge'].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener('input', updateSmokingYears);
});

function handleConditionals(){
  toggle($('pastIllnessSub'), radioValue('pastIllnessStatus') === 'ある');
  toggle($('medicationSub'), radioValue('medicationStatus') === 'ある');
  toggle($('medicationDetailSub'), radioValue('medicationBook') === 'いいえ');

  toggleSection($('agaSection'), REAL_GENDER === '男');
  toggle($('agaSub'), radioValue('agaStatus') === 'はい');

  toggle($('familyCancerSub'), radioValue('familyCancerStatus') === 'はい');
  toggle($('allergySub'), radioValue('allergyStatus') === 'ある');

  toggleSection($('lifestyleSection'), REAL_AGE >= 20);
  toggle($('smokeActiveSub'), radioValue('smoking') === '吸う');
  toggle($('smokeQuitSub'), radioValue('smoking') === '禁煙中');
  updateSmokingYears();

  toggleSection($('femaleSection'), REAL_GENDER === '女');
  toggle($('pregnantWeekSub'), radioValue('pregnant') === 'はい');

  toggle($('feverSub'), radioValue('feverStatus') === 'ある');

  const voidingTriggerKeys = ['urinaryIncontinence','residualUrine','difficultyUrinating','frequentUrination'];
  const triggerLabels = { urinaryIncontinence:'尿が漏れる', residualUrine:'残尿感がある', difficultyUrinating:'尿が出にくい', frequentUrination:'頻尿' };
  const checkedTriggers = voidingTriggerKeys.filter(checkedOf);
  const voidingTriggerChecked = checkedTriggers.length > 0;
  toggleSection($('voidingTriggerFinalSection'), voidingTriggerChecked);
  if (voidingTriggerChecked) {
    $('voidingTriggerFinalLabel').innerHTML =
      `「${checkedTriggers.map(k => triggerLabels[k]).join('、')}」の症状は以前から気になりますか<span class="req">必須</span>`;
  }

  const maleMenopauseChecked = checkedOf('maleMenopause');
  const showVoiding = (voidingTriggerChecked && radioValue('voidingOneWeekPlus') === 'はい') || maleMenopauseChecked;
  toggleSection($('voidingSection'), showVoiding);

  toggle($('backPainSub'), checkedOf('backPain'));
  toggle($('testicleDiscomfortSub'), checkedOf('testicleDiscomfort'));
  toggle($('stdConcernSub'), checkedOf('stdConcern'));
  const partnerCb = document.querySelector('.std-partner input');
  toggle($('stdDiseaseDetailSub'), partnerCb ? partnerCb.checked : false);
  toggle($('checkupAbnormalitySub'), checkedOf('checkupAbnormality'));
  toggle($('freeInjectionSub'), checkedOf('freeInjection'));
}
if ($('uroForm')) handleConditionals();

function validateForm(){
  let firstInvalidEl = null;
  let ok = true;
  function check(key, passed, scrollEl){
    showError(key, !passed);
    if (!passed){
      ok = false;
      if (!firstInvalidEl) firstInvalidEl = scrollEl;
    }
  }

  check('ticketNumber', $('ticketNumber').value.trim() !== '', $('ticketNumber').closest('.section'));
  check('pastIllnessStatus', radioValue('pastIllnessStatus') !== null, document.querySelector('[data-group="pastIllnessStatus"]').closest('.section'));
  check('medicationStatus', radioValue('medicationStatus') !== null, document.querySelector('[data-group="medicationStatus"]').closest('.section'));

  if (REAL_GENDER === '男') {
    check('agaStatus', radioValue('agaStatus') !== null, $('agaSection'));
  } else {
    showError('agaStatus', false);
  }

  check('familyCancerStatus', radioValue('familyCancerStatus') !== null, document.querySelector('[data-group="familyCancerStatus"]').closest('.section'));
  check('allergyStatus', radioValue('allergyStatus') !== null, document.querySelector('[data-group="allergyStatus"]').closest('.section'));

  if (REAL_AGE >= 20) {
    check('alcohol', radioValue('alcohol') !== null, $('lifestyleSection'));
    check('smoking', radioValue('smoking') !== null, $('lifestyleSection'));
  } else {
    showError('alcohol', false);
    showError('smoking', false);
  }

  if (REAL_GENDER === '女') {
    check('pregnant', radioValue('pregnant') !== null, $('femaleSection'));
    check('breastfeeding', radioValue('breastfeeding') !== null, $('femaleSection'));
    check('menstruating', radioValue('menstruating') !== null, $('femaleSection'));
  } else {
    showError('pregnant', false);
    showError('breastfeeding', false);
    showError('menstruating', false);
  }

  check('symptomOnset', $('symptomOnset').value.trim() !== '', $('symptomOnset').closest('.section'));
  check('feverStatus', radioValue('feverStatus') !== null, document.querySelector('[data-group="feverStatus"]').closest('.section'));
  check('symptoms', document.querySelectorAll('.symptom-item input:checked').length > 0, document.querySelector('.symptom-item').closest('.section'));

  if (!$('voidingTriggerFinalSection').classList.contains('hidden')) {
    check('voidingOneWeekPlus', radioValue('voidingOneWeekPlus') !== null, $('voidingTriggerFinalSection'));
  } else {
    showError('voidingOneWeekPlus', false);
  }

  if (!ok && firstInvalidEl) {
    firstInvalidEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  return ok;
}

if ($('uroForm')) {
  $('uroForm').addEventListener('submit', e => {
    if (!validateForm()) e.preventDefault();
  });
}
</script>
</body></html>
"""


@app.route("/form/<token>")
def form(token):
    patient_id = resolve_token(token)
    form_type = resolve_token_type(token)

    if not patient_id:
        fields = get_form_fields(form_type)
        return render_template_string(
            FORM_PAGE, error="このリンクは無効です。受付にお問い合わせください。",
            saved=False, token=token, fields=fields,
        )

    if form_type == "urology_general":
        entry = lookup_directory(patient_id) or {}
        gender = entry.get("gender", "") or "男"
        age = compute_age(entry.get("dob", ""))
        return render_template_string(
            UROLOGY_FORM_PAGE, token=token, gender=gender, age=age, saved=False, error=None
        )

    fields = get_form_fields(form_type)
    return render_template_string(FORM_PAGE, token=token, fields=fields, saved=False, error=None)


@app.route("/submit/urology", methods=["POST"])
def submit_urology():
    token = request.form.get("token", "").strip()
    patient_id = resolve_token(token)
    if not patient_id:
        return render_template_string(
            UROLOGY_FORM_PAGE, token=token, gender="男", age=0,
            saved=False, error="このリンクは無効です。受付にお問い合わせください。",
        )

    f = request.form
    record = {
        "submitted_at": now_jst_str(),
        "form_type": "urology_general",
        "confirmed": False,
        "ticketNumber": f.get("ticketNumber", "").strip(),
        "pastIllnessStatus": f.get("pastIllnessStatus", ""),
        "pastIllnessItems": "、".join(f.getlist("pastIllnessItems")),
        "pastIllnessOtherDetail": f.get("pastIllnessOtherDetail", "").strip(),
        "medicationStatus": f.get("medicationStatus", ""),
        "medicationBook": f.get("medicationBook", ""),
        "medicationDetail": f.get("medicationDetail", "").strip(),
        "agaStatus": f.get("agaStatus", ""),
        "agaDetail": f.get("agaDetail", "").strip(),
        "familyCancerStatus": f.get("familyCancerStatus", ""),
        "familyCancerItems": "、".join(f.getlist("familyCancerItems")),
        "familyCancerOtherDetail": f.get("familyCancerOtherDetail", "").strip(),
        "allergyStatus": f.get("allergyStatus", ""),
        "allergyDetail": f.get("allergyDetail", "").strip(),
        "alcohol": f.get("alcohol", ""),
        "smoking": f.get("smoking", ""),
        "smokeActivePerDay": f.get("smokeActivePerDay", "").strip(),
        "smokeActiveStartAge": f.get("smokeActiveStartAge", "").strip(),
        "smokeActiveYears": f.get("smokeActiveYears", "").strip(),
        "smokeQuitPerDay": f.get("smokeQuitPerDay", "").strip(),
        "smokeQuitStartAge": f.get("smokeQuitStartAge", "").strip(),
        "smokeQuitEndAge": f.get("smokeQuitEndAge", "").strip(),
        "smokeQuitYears": f.get("smokeQuitYears", "").strip(),
        "pregnant": f.get("pregnant", ""),
        "pregnantWeek": f.get("pregnantWeek", "").strip(),
        "breastfeeding": f.get("breastfeeding", ""),
        "menstruating": f.get("menstruating", ""),
        "symptomOnset": f.get("symptomOnset", "").strip(),
        "feverStatus": f.get("feverStatus", ""),
        "feverFrom": f.get("feverFrom", "").strip(),
        "feverTo": f.get("feverTo", "").strip(),
        "feverMaxTemp": f.get("feverMaxTemp", "").strip(),
        "symptoms": "、".join(f.getlist("symptoms")),
        "backPainSide": f.get("backPainSide", ""),
        "testicleDiscomfortType": f.get("testicleDiscomfortType", ""),
        "stdConcernDetail": "、".join(f.getlist("stdConcernDetail")),
        "stdDiseaseDetail": f.get("stdDiseaseDetail", "").strip(),
        "checkupAbnormalityDetail": f.get("checkupAbnormalityDetail", "").strip(),
        "freeInjectionItems": "、".join(f.getlist("freeInjectionItems")),
        "voidingOneWeekPlus": f.get("voidingOneWeekPlus", ""),
        "freeNote": f.get("freeNote", "").strip(),
    }

    for k in IPSS_KEYS + ["ipss_qol"] + OABSS_KEYS:
        record[k] = f.get(k, "")

    record = compute_scores(record, get_form_fields("urology_general"))

    save_record(patient_id, record)
    return render_template_string(
        UROLOGY_FORM_PAGE, token=token, gender="男", age=0, saved=True, error=None
    )


def compute_age(dob: str) -> int:
    try:
        parts = dob.replace("　", " ").strip().split("/")
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        today = now_jst()
        age = today.year - y
        if (today.month, today.day) < (m, d):
            age -= 1
        return age
    except Exception:
        return 0


IPSS_KEYS = [
    "ipss_residual", "ipss_frequency", "ipss_intermittency", "ipss_urgency",
    "ipss_weak_stream", "ipss_straining", "ipss_nocturia",
]
OABSS_KEYS = ["oabss_daytime", "oabss_nighttime", "oabss_urgency", "oabss_incontinence"]


def compute_scores(record, fields):
    field_keys = {f["key"] for f in fields}

    if set(IPSS_KEYS).issubset(field_keys):
        total = 0
        for k in IPSS_KEYS:
            try:
                total += int(record.get(k, ""))
            except (ValueError, TypeError):
                pass
        record["ipss_total"] = str(total)

    if "ipss_qol" in field_keys:
        record["ipss_qol_score"] = record.get("ipss_qol", "")

    if set(OABSS_KEYS).issubset(field_keys):
        total = 0
        for k in OABSS_KEYS:
            try:
                total += int(record.get(k, ""))
            except (ValueError, TypeError):
                pass
        record["oabss_total"] = str(total)

    return record


@app.route("/submit", methods=["POST"])
def submit():
    token = request.form.get("token", "").strip()
    patient_id = resolve_token(token)
    form_type = resolve_token_type(token)
    fields = get_form_fields(form_type)
    if not patient_id:
        return render_template_string(
            FORM_PAGE, error="このリンクは無効です。受付にお問い合わせください。",
            saved=False, token=token, fields=fields,
        )

    record = {"submitted_at": now_jst_str(), "form_type": form_type, "confirmed": False}
    for f in fields:
        if f["type"] == "computed":
            continue
        if f["type"] == "checkbox":
            record[f["key"]] = "、".join(request.form.getlist(f["key"]))
        else:
            record[f["key"]] = request.form.get(f["key"], "").strip()

    record = compute_scores(record, fields)

    save_record(patient_id, record)
    return render_template_string(FORM_PAGE, token=token, fields=fields, saved=True, error=None)


@app.route("/")
def index():
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
