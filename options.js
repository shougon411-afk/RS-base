const input = document.getElementById("serverBase");
const status = document.getElementById("status");

chrome.storage.sync.get(["serverBase"], (res) => {
  if (res.serverBase) input.value = res.serverBase;
});

document.getElementById("saveBtn").addEventListener("click", () => {
  const value = input.value.trim().replace(/\/$/, "");
  chrome.storage.sync.set({ serverBase: value }, () => {
    status.textContent = "保存しました。N2017.cgiのページを再読み込みしてください。";
    setTimeout(() => (status.textContent = ""), 3000);
  });
});
