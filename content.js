(function () {
  "use strict";

  const BUTTON_ID = "monshinViewerButton";
  const KENSA_BUTTON_ID = "kensaLineButton";

  function extractPatientId() {
    // 既存の「問診表示」ボタンと同じロジック。変更していません。
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

  function readHiddenValue(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : "";
  }

  // ============================================================
  // 半角カタカナ(ｱｲｳ...、濁点ﾞ・半濁点ﾟは分離した状態)を
  // 全角カタカナに変換する。
  // 実機コンソールで確認: yomi_search = "ｲｼｲ ｼｮｳｺﾞ" -> "イシイ ショウゴ"
  // ============================================================
  function hankakuKanaToZenkaku(str) {
    const kanaMap = {
      ｦ: "ヲ", ｧ: "ァ", ｨ: "ィ", ｩ: "ゥ", ｪ: "ェ", ｫ: "ォ",
      ｬ: "ャ", ｭ: "ュ", ｮ: "ョ", ｯ: "ッ", ｰ: "ー",
      ｱ: "ア", ｲ: "イ", ｳ: "ウ", ｴ: "エ", ｵ: "オ",
      ｶ: "カ", ｷ: "キ", ｸ: "ク", ｹ: "ケ", ｺ: "コ",
      ｻ: "サ", ｼ: "シ", ｽ: "ス", ｾ: "セ", ｿ: "ソ",
      ﾀ: "タ", ﾁ: "チ", ﾂ: "ツ", ﾃ: "テ", ﾄ: "ト",
      ﾅ: "ナ", ﾆ: "ニ", ﾇ: "ヌ", ﾈ: "ネ", ﾉ: "ノ",
      ﾊ: "ハ", ﾋ: "ヒ", ﾌ: "フ", ﾍ: "ヘ", ﾎ: "ホ",
      ﾏ: "マ", ﾐ: "ミ", ﾑ: "ム", ﾒ: "メ", ﾓ: "モ",
      ﾔ: "ヤ", ﾕ: "ユ", ﾖ: "ヨ",
      ﾗ: "ラ", ﾘ: "リ", ﾙ: "ル", ﾚ: "レ", ﾛ: "ロ",
      ﾜ: "ワ", ﾝ: "ン",
      "｡": "。", "､": "、", "｢": "「", "｣": "」", "･": "・",
    };
    const dakutenMap = {
      カ: "ガ", キ: "ギ", ク: "グ", ケ: "ゲ", コ: "ゴ",
      サ: "ザ", シ: "ジ", ス: "ズ", セ: "ゼ", ソ: "ゾ",
      タ: "ダ", チ: "ヂ", ツ: "ヅ", テ: "デ", ト: "ド",
      ハ: "バ", ヒ: "ビ", フ: "ブ", ヘ: "ベ", ホ: "ボ", ウ: "ヴ",
    };
    const handakutenMap = { ハ: "パ", ヒ: "ピ", フ: "プ", ヘ: "ペ", ホ: "ポ" };

    let result = "";
    for (let i = 0; i < str.length; i++) {
      const ch = str[i];
      const next = str[i + 1];
      if (kanaMap[ch]) {
        const base = kanaMap[ch];
        if (next === "ﾞ" && dakutenMap[base]) {
          result += dakutenMap[base];
          i++;
        } else if (next === "ﾟ" && handakutenMap[base]) {
          result += handakutenMap[base];
          i++;
        } else {
          result += base;
        }
      } else if (ch === "　") {
        result += " ";
      } else {
        result += ch; // 半角カナ以外(スペース等)はそのまま
      }
    }
    return result;
  }

  // ============================================================
  // LINE検査登録ボタン用の患者情報抽出。
  // 実機コンソールで確認済みの隠しフィールドから直接値を読む
  // (正規表現でHTMLから拾うより確実):
  //   id_search   : 患者ID (例 "2" ※ゼロ埋めされていない)
  //   yomi_search : 氏名の読み、半角カタカナ (例 "ｲｼｲ ｼｮｳｺﾞ")
  //   y_pt/m_pt/d_pt : 生年月日(西暦)を年・月・日に分けて保持
  // ============================================================
  function extractKensaPatientInfo() {
    const rawId = readHiddenValue("id_search");
    const yomi = readHiddenValue("yomi_search");
    const y = readHiddenValue("y_pt");
    const mo = readHiddenValue("m_pt");
    const d = readHiddenValue("d_pt");

    // line-kensa-system側は「患者ID(6桁の数字)」なのでゼロ埋めする。
    // id_search が取れない場合は、既存の問診表示用ロジックにフォールバック。
    const idSource = rawId || extractPatientId() || "";
    const patientId = idSource ? idSource.padStart(6, "0") : "";

    const kana = yomi ? hankakuKanaToZenkaku(yomi).trim() : "";

    let dobIso = "";
    if (y && mo && d) {
      dobIso = `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    }

    return { patientId, kana, dobIso };
  }

  function getSettings() {
    return new Promise((resolve) => {
      chrome.storage.sync.get(["serverBase", "kensaServerBase"], (res) => {
        resolve({
          serverBase: (res.serverBase || "").trim().replace(/\/$/, ""),
          kensaServerBase: (res.kensaServerBase || "").trim().replace(/\/$/, ""),
        });
      });
    });
  }

  function ensureButton(serverBase, patientId) {
    if (document.getElementById(BUTTON_ID)) return;

    const btn = document.createElement("div");
    btn.id = BUTTON_ID;
    btn.textContent = "問診表示";
    btn.title = "この患者の問診結果を新しいタブで開く";
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

  // ============================================================
  // 新規: 「LINE検査登録」タブ。
  // line-kensa-system の管理画面(/admin/)を、患者ID・氏名(カタカナ)・
  // 生年月日(西暦, YYYY-MM-DD)をクエリパラメータで渡した状態で新しいタブに開く。
  // 和暦への変換はline-kensa-system側(kensa-admin-autofill.js)で行う。
  // ============================================================
  function ensureKensaButton(kensaServerBase, patientId, nameKana, dobIso) {
    if (document.getElementById(KENSA_BUTTON_ID)) return;

    const btn = document.createElement("div");
    btn.id = KENSA_BUTTON_ID;
    btn.textContent = "LINE検査登録";
    btn.title = "この患者の情報を引き継いでLINE検査結果送信システムの登録画面を開く";
    btn.style.cssText = [
      "position:fixed",
      "right:0",
      "top:545px",
      "z-index:2147482998",
      "background:#d9f7e6",
      "border:1px solid #06C755",
      "border-right:none",
      "border-radius:8px 0 0 8px",
      "padding:10px 6px",
      "cursor:pointer",
      "font-size:13px",
      "font-weight:bold",
      "color:#0b6b3a",
      "font-family:sans-serif",
      "writing-mode:vertical-rl",
      "box-shadow:0 2px 8px rgba(0,0,0,0.25)",
    ].join(";");

    btn.onclick = () => {
      if (!kensaServerBase) {
        alert(
          "検査結果送信システムのアドレスが未設定です。\n" +
            "拡張機能を右クリック→「オプション」から設定してください。"
        );
        return;
      }
      if (!patientId) {
        alert("この画面から患者IDを取得できませんでした。");
        return;
      }
      if (!nameKana) {
        // カナが取れなくても患者ID等は渡した状態で開き、氏名だけ手入力してもらう
        alert(
          "カタカナ氏名を自動取得できませんでした。氏名欄は手入力してください。\n" +
            "(yomi_search フィールドの値を確認してください)"
        );
      }

      const params = new URLSearchParams();
      params.set("pid", patientId);
      if (nameKana) params.set("kana", nameKana);
      if (dobIso) params.set("dob", dobIso);

      window.open(`${kensaServerBase}/admin/?${params.toString()}`, "_blank");
    };

    document.body.appendChild(btn);
  }

  async function init() {
    const patientId = extractPatientId();
    const { patientId: kensaPatientId, kana: nameKana, dobIso } = extractKensaPatientInfo();
    const { serverBase, kensaServerBase } = await getSettings();

    ensureButton(serverBase, patientId);
    ensureKensaButton(kensaServerBase, kensaPatientId, nameKana, dobIso);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  window.addEventListener("load", init);
})();
