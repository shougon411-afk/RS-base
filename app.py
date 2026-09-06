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
import secrets
from datetime import datetime
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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-before-deploying")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-this-password")

# ブックマークレットからの登録リクエストを認証するための簡易キー。
# 必ず環境変数で上書きしてください(既定値のままは危険です)。
REGISTER_API_KEY = os.environ.get("REGISTER_API_KEY", "change-this-api-key")

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
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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


def upsert_directory(patient_id: str, name: str, dob: str):
    directory = _load_directory()
    directory[patient_id] = {
        "name": name,
        "dob": dob,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_directory(directory)


def lookup_directory(patient_id: str):
    return _load_directory().get(patient_id)


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
  .top a{font-size:12px;}
</style></head><body>
<div class="top"><a href="{{ url_for('admin_dashboard') }}">← リンク発行に戻る</a></div>
{% if not records %}
  <div class="empty">この患者(ID: {{ patient_id }})の問診回答はまだありません。</div>
{% else %}
  <h2>問診結果(患者ID: {{ patient_id }})</h2>
  <div class="meta">回答日時: {{ latest.submitted_at }}（全{{ records|length }}件中 最新）</div>
  <table>
    {% for f in fields %}
      <tr><th>{{ f.label }}</th><td>{{ latest.get(f.key, "") or "-" }}</td></tr>
    {% endfor %}
  </table>
{% endif %}
</body></html>
"""


@app.route("/admin/view/<patient_id>")
@login_required
def admin_view(patient_id):
    records = load_records(patient_id)
    latest = records[-1] if records else None
    fields = get_form_fields(latest.get("form_type", "general")) if latest else []
    return render_template_string(
        VIEW_PAGE, patient_id=patient_id, records=records, latest=latest, fields=fields
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
    form_type = str(payload.get("type", "general")).strip() or "general"

    if not patient_id:
        return _cors(("patient id required", 400))

    upsert_directory(patient_id, name, dob)
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
      <div class="field">
        <label class="field-label">{{ f.label }}</label>
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
        {% endif %}
      </div>
    {% endfor %}
    <button type="submit" class="submit-btn">回答を送信する</button>
  </form>
{% endif %}
</body></html>
"""


@app.route("/form/<token>")
def form(token):
    patient_id = resolve_token(token)
    form_type = resolve_token_type(token)
    fields = get_form_fields(form_type)
    if not patient_id:
        return render_template_string(
            FORM_PAGE, error="このリンクは無効です。受付にお問い合わせください。",
            saved=False, token=token, fields=fields,
        )
    return render_template_string(FORM_PAGE, token=token, fields=fields, saved=False, error=None)


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

    record = {"submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "form_type": form_type}
    for f in fields:
        record[f["key"]] = request.form.get(f["key"], "").strip()

    save_record(patient_id, record)
    return render_template_string(FORM_PAGE, token=token, fields=fields, saved=True, error=None)


@app.route("/")
def index():
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

