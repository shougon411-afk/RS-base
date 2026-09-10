/**
 * RS-base連携: 患者ID・氏名(カタカナ)・生年月日の自動入力
 * ------------------------------------------------------------
 * 【組み込み方法】
 * public/admin/index.html を開き、既存の <script> ... </script>(管理画面の
 * ロジックが書かれているタグ)の閉じタグ直前に、このファイルの中身を
 * まるごと貼り付けてください。もしくは、この内容を
 * public/admin/autofill.js として保存し、既存の <script> タグの直前に
 *   <script src="autofill.js"></script>
 * を追加しても構いません。
 *
 * 【前提にしている画面のID】(実際にログインして確認済み)
 *   患者ID           : #p-id
 *   氏名(カタカナ)     : #p-name
 *   生年月日 元号セレクト : #p-era      (明治/大正/昭和/平成/令和)
 *   生年月日 年        : #p-era-year
 *   生年月日 月        : #p-month
 *   生年月日 日        : #p-day
 *
 * 既存の登録処理(送信ボタン #p-submit まわりのコード)には一切手を
 * 加えていません。このスクリプトは上記の入力欄に値をセットするだけです。
 *
 * 【URLパラメータの仕様】(RS-base拡張機能側と合わせる)
 *   ?pid=000123&kana=ヤマダ%20タロウ&dob=2002-04-11
 *   - pid : 患者ID(6桁)
 *   - kana: 氏名のカタカナ(空白区切り可)
 *   - dob : 生年月日、西暦のISO形式 YYYY-MM-DD
 */
(function () {
  "use strict";

  // ===== 西暦 <-> 和暦 変換 =====
  // 明治以降の元号切り替え日(その元号が始まる日)
  const ERA_TABLE = [
    { name: "令和", start: new Date(2019, 4, 1) }, // 2019-05-01
    { name: "平成", start: new Date(1989, 0, 8) }, // 1989-01-08
    { name: "昭和", start: new Date(1926, 11, 25) }, // 1926-12-25
    { name: "大正", start: new Date(1912, 6, 30) }, // 1912-07-30
    { name: "明治", start: new Date(1868, 0, 25) }, // 1868-01-25
  ];

  function seirekiToWareki(year, month, day) {
    const d = new Date(year, month - 1, day);
    for (const era of ERA_TABLE) {
      if (d >= era.start) {
        return { era: era.name, eraYear: year - era.start.getFullYear() + 1, month, day };
      }
    }
    return null; // 明治より前の生年月日は非対応
  }

  function warekiToSeireki(eraName, eraYear, month, day) {
    const era = ERA_TABLE.find((e) => e.name === eraName);
    if (!era || !eraYear) return null;
    return { year: era.start.getFullYear() + Number(eraYear) - 1, month, day };
  }

  // グローバルに公開しておく(管理画面の他の場所からも使えるように)
  window.seirekiToWareki = seirekiToWareki;
  window.warekiToSeireki = warekiToSeireki;

  // ===== RS-baseから渡された情報での自動入力 =====
  function autofillFromQuery() {
    const params = new URLSearchParams(location.search);
    const pid = params.get("pid");
    const kana = params.get("kana");
    const dob = params.get("dob"); // YYYY-MM-DD (西暦)

    if (!pid && !kana && !dob) return;

    const idEl = document.getElementById("p-id");
    const nameEl = document.getElementById("p-name");
    const eraEl = document.getElementById("p-era");
    const eraYearEl = document.getElementById("p-era-year");
    const monthEl = document.getElementById("p-month");
    const dayEl = document.getElementById("p-day");

    if (pid && idEl) idEl.value = pid;
    if (kana && nameEl) nameEl.value = kana;

    if (dob) {
      const m = dob.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
      if (m) {
        const y = Number(m[1]);
        const mo = Number(m[2]);
        const da = Number(m[3]);
        const w = seirekiToWareki(y, mo, da);
        if (w && eraEl && eraYearEl && monthEl && dayEl) {
          eraEl.value = w.era;
          eraYearEl.value = w.eraYear;
          monthEl.value = w.month;
          dayEl.value = w.day;
        }
      }
    }

    // 自動入力された項目がひと目でわかるように色をつける(見た目だけの変更)
    [idEl, nameEl, eraEl, eraYearEl, monthEl, dayEl].forEach((el) => {
      if (!el) return;
      el.style.backgroundColor = "#eefaf3";
      el.style.borderColor = "#a7e6c1";
    });

    if (idEl && idEl.parentElement) {
      const badge = document.createElement("div");
      badge.textContent = "RS-baseから自動入力";
      badge.style.cssText =
        "display:inline-block;font-size:11px;font-weight:700;color:#0b6b3a;" +
        "background:#d9f7e6;border:1px solid #a7e6c1;border-radius:999px;" +
        "padding:2px 10px;margin-bottom:8px;";
      idEl.parentElement.insertBefore(badge, idEl.parentElement.firstChild);
    }
  }

  // ===== 西暦での入力を補助する小さなツール =====
  // 既存の和暦(元号/年/月/日)欄はそのまま残し、その下に
  // 「西暦から自動計算」の補助入力を追加する。西暦欄に入力すると
  // 既存の和暦欄(#p-era 等、登録時に実際に使われる項目)へ反映される。
  function addSeirekiHelper() {
    const eraEl = document.getElementById("p-era");
    const eraYearEl = document.getElementById("p-era-year");
    const monthEl = document.getElementById("p-month");
    const dayEl = document.getElementById("p-day");
    if (!eraEl || !eraYearEl || !monthEl || !dayEl) return;
    if (document.getElementById("p-seireki-helper")) return;

    const wrap = document.createElement("div");
    wrap.id = "p-seireki-helper";
    wrap.style.cssText =
      "display:flex;align-items:center;gap:6px;margin-top:6px;font-size:12px;color:#555;";
    wrap.innerHTML =
      '<span>西暦から自動計算:</span>' +
      '<input type="number" id="p-seireki-year" placeholder="例:2002" style="width:70px;padding:6px;border:1px solid #ccc;border-radius:6px;font-size:13px;">年' +
      '<input type="number" id="p-seireki-month" placeholder="月" style="width:44px;padding:6px;border:1px solid #ccc;border-radius:6px;font-size:13px;">月' +
      '<input type="number" id="p-seireki-day" placeholder="日" style="width:44px;padding:6px;border:1px solid #ccc;border-radius:6px;font-size:13px;">日';

    // 和暦欄(#p-era)の後ろに挿入する。実際のレイアウトによっては
    // 挿入位置を調整してください。
    eraEl.parentElement.insertAdjacentElement("afterend", wrap);

    const y = wrap.querySelector("#p-seireki-year");
    const mo = wrap.querySelector("#p-seireki-month");
    const da = wrap.querySelector("#p-seireki-day");

    function sync() {
      const yy = parseInt(y.value, 10);
      const mm = parseInt(mo.value, 10);
      const dd = parseInt(da.value, 10);
      if (!yy || !mm || !dd) return;
      const w = seirekiToWareki(yy, mm, dd);
      if (w) {
        eraEl.value = w.era;
        eraYearEl.value = w.eraYear;
        monthEl.value = w.month;
        dayEl.value = w.day;
      }
    }
    [y, mo, da].forEach((el) => el.addEventListener("input", sync));
  }

  function init() {
    addSeirekiHelper();
    autofillFromQuery();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
