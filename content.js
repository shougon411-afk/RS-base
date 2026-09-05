(function () {
  "use strict";

  const BUTTON_ID = "monshinViewerButton";

  function extractPatientId() {
    const html = document.documentElement.innerHTML;

    // このクリニックのN2017.cgiでは、音声入力用に
    // window.RS_VOICE_PATIENT_ID='2'; のようなグローバル変数が
    // ページ内スクリプトに埋め込まれているため、これを利用する。
    let m = html.match(/RS_VOICE_PATIENT_ID\s*=\s*'(\d+)'/);
    if (m) return m[1];

    m = html.match(/kanja_id=(\d+)/);
    if (m) return m[1];

    m = html.match(/RS_GROWTH_CHART_URL\s*=\s*'[^']*[?&]id=(\d+)/);
    if (m) return m[1];

    return null;
  }

  function getServerBase() {
    return new Promise((resolve) => {
      chrome.storage.sync.get(["serverBase"], (res) => {
        resolve((res.serverBase || "").trim().replace(/\/$/, ""));
      });
    });
  }

  function ensureButton(serverBase, patientId) {
    if (document.getElementById(BUTTON_ID)) return;

    const btn = document.createElement("div");
    btn.id = BUTTON_ID;
    btn.textContent = "問診";
    btn.title = "問診結果を新しいタブで開く";
    btn.style.cssText = [
      "position:fixed",
      "right:0",
      "top:465px",
      "z-index:2147482998",
      "background:#efe6ff",
      "border:1px solid #666",
      "border-right:none",
      "border-radius:8px 0 0 8px",
      "padding:10px 6px",
      "cursor:pointer",
      "font-size:13px",
      "font-family:sans-serif",
      "writing-mode:vertical-rl",
      "box-shadow:0 2px 8px rgba(0,0,0,0.25)",
    ].join(";");

    btn.onclick = () => {
      if (!serverBase) {
        alert(
          "問診サーバーのアドレスが未設定です。\n" +
            "拡張機能を右クリック→「オプション」から設定してください。"
        );
        return;
      }
      if (!patientId) {
        alert("この画面から患者IDを取得できませんでした。");
        return;
      }
      // 別タブで開く(iframe埋め込みにすると、クロスオリジンで
      // ログインセッションのCookieがブロックされることがあるため)
      window.open(`${serverBase}/admin/view/${encodeURIComponent(patientId)}`, "_blank");
    };

    document.body.appendChild(btn);
  }

  async function init() {
    const patientId = extractPatientId();
    const serverBase = await getServerBase();
    ensureButton(serverBase, patientId);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  window.addEventListener("load", init);
})();
