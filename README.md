# ブックマークレットの使い方

## 1. 下記の1行コードをコピーする

`API_BASE` と `API_KEY` の部分を、実際にデプロイしたサーバーの値に書き換えてから使ってください。

```
javascript:(function(){var CONFIG={API_BASE:"https://your-app.up.railway.app",API_KEY:"change-this-api-key"};function extractPatientId(){var html=document.documentElement.innerHTML;var m=html.match(/RS_VOICE_PATIENT_ID\s*=\s*'(\d+)'/);if(m)return m[1];m=html.match(/kanja_id=(\d+)/);if(m)return m[1];return null;}function extractName(){var html=document.documentElement.innerHTML;var m=html.match(/font-family:\s*'?メイリオ'?">([^<]+)<\/span>/);return m?m[1].trim():"";}function extractDob(){var html=document.documentElement.innerHTML;var m=html.match(/(\d{4}\/\d{1,2}\/\d{1,2})生/);return m?m[1]:"";}var id=extractPatientId();var name=extractName();var dob=extractDob();if(!id){alert("患者IDを取得できませんでした。");return;}fetch(CONFIG.API_BASE+"/checkin/register",{method:"POST",headers:{"Content-Type":"application/json","X-Api-Key":CONFIG.API_KEY},body:JSON.stringify({id:id,name:name,dob:dob})}).then(function(res){return res.json();}).then(function(data){if(data&&data.confirm_url){location.href=data.confirm_url;}else{alert("登録に失敗しました: "+JSON.stringify(data));}}).catch(function(err){alert("通信エラー: "+err.message);});})();
```

## 2. iPhone Chromeへの登録手順

1. Chromeで適当なページを開き、共有ボタン→「ブックマークに追加」でひとまずブックマークを1つ作る
2. Chromeの「ブックマーク」一覧を開き、①で作ったブックマークを長押し→「編集」
3. 名前を「問診登録」などに変更、URL欄の中身を全部消して上記の1行コードを貼り付け、保存
4. N2017.cgiの患者画面を開いた状態で、ブックマーク一覧からこの「問診登録」をタップすると実行されます

**Chromeでうまく保存できない場合(iOSやChromeのバージョンによってはjavascript:を
弾くことがあります)**は、同じ手順をSafariで試してください。SafariはiOSの中で
最もブックマークレットの動作が安定しています。
