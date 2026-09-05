/*
 * このファイルの中身をそのままブックマークレットとして登録してください。
 *
 * 【iPhone Chromeでの登録手順】
 * 1. Chromeで何かページ(例: このサーバーのトップページ)をブックマークに追加しておく
 * 2. Chromeの「ブックマーク」画面を開き、①で作ったブックマークを編集
 * 3. 名前を「問診登録」などにし、URL欄にこのファイルの内容(1行の javascript:... )を
 *    まるごと貼り付けて保存
 * 4. うまく保存できない/実行時に何も起こらない場合はSafariで同じ手順を試してください
 *    (iOSのバージョンやChromeの設定によりjavascript:ブックマークが保存できない場合があります)
 *
 * 【設定必須の値】
 * - API_BASE   : 問診システムのアドレス(例: https://your-app.up.railway.app)
 * - API_KEY    : サーバー側の REGISTER_API_KEY と同じ値
 * この2つは下の CONFIG 部分を書き換えてから、1行に圧縮してjavascript:を付けてください。
 */

(function () {
  var CONFIG = {
    API_BASE: "https://your-app.up.railway.app", // ← 必ず書き換える
    API_KEY: "change-this-api-key", // ← 必ずサーバー側と同じ値に書き換える
  };

  function extractPatientId() {
    var html = document.documentElement.innerHTML;
    var m = html.match(/RS_VOICE_PATIENT_ID\s*=\s*'(\d+)'/);
    if (m) return m[1];
    m = html.match(/kanja_id=(\d+)/);
    if (m) return m[1];
    return null;
  }

  function extractName() {
    var html = document.documentElement.innerHTML;
    var m = html.match(/font-family:\s*'?メイリオ'?">([^<]+)<\/span>/);
    return m ? m[1].trim() : "";
  }

  function extractDob() {
    var html = document.documentElement.innerHTML;
    var m = html.match(/(\d{4}\/\d{1,2}\/\d{1,2})生/);
    return m ? m[1] : "";
  }

  var id = extractPatientId();
  var name = extractName();
  var dob = extractDob();

  if (!id) {
    alert("患者IDを取得できませんでした。この画面はN2017.cgiの患者画面ですか？");
    return;
  }

  fetch(CONFIG.API_BASE + "/checkin/register", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Api-Key": CONFIG.API_KEY,
    },
    body: JSON.stringify({ id: id, name: name, dob: dob }),
  })
    .then(function (res) {
      return res.json();
    })
    .then(function (data) {
      if (data && data.confirm_url) {
        location.href = data.confirm_url;
      } else {
        alert("登録に失敗しました: " + JSON.stringify(data));
      }
    })
    .catch(function (err) {
      alert("通信エラー: " + err.message);
    });
})();
