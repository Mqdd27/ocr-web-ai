const form = document.getElementById("docForm");
const action = document.getElementById("action");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const ocrText = document.getElementById("ocrText");
const submitBtn = document.getElementById("submitBtn");
let lastResult = "";

function updateFields() {
  document.querySelectorAll(".conditional").forEach((el) => el.classList.remove("show"));
  document.querySelectorAll(`.${action.value}`).forEach((el) => el.classList.add("show"));
}

async function download(endpoint, content) {
  const body = new FormData();
  body.append("content", content || lastResult || "");
  const response = await fetch(endpoint, { method: "POST", body });
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = endpoint.includes("json") ? "document-ai-result.json" : "document-ai-result.txt";
  link.click();
  URL.revokeObjectURL(url);
}

action.addEventListener("change", updateFields);
document.getElementById("clearBtn").addEventListener("click", () => {
  form.reset();
  resultEl.textContent = "";
  ocrText.value = "";
  lastResult = "";
  statusEl.textContent = "Idle";
  updateFields();
});
document.getElementById("copyBtn").addEventListener("click", async () => {
  await navigator.clipboard.writeText(lastResult || resultEl.textContent);
  statusEl.textContent = "Copied result";
});
document.getElementById("txtBtn").addEventListener("click", () => download("/download/txt", lastResult));
document.getElementById("jsonBtn").addEventListener("click", () => download("/download/json", lastResult));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const started = performance.now();
  submitBtn.disabled = true;
  statusEl.textContent = "Processing...";
  resultEl.textContent = "";
  const timer = window.setInterval(() => {
    const elapsed = ((performance.now() - started) / 1000).toFixed(1);
    statusEl.textContent = `Processing for ${elapsed}s...`;
  }, 1000);
  try {
    const response = await fetch("/process", { method: "POST", body: new FormData(form) });
    const raw = await response.text();
    let data = null;
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch {
      const preview = raw.trim().replace(/\s+/g, " ").slice(0, 500);
      throw new Error(preview || `Server returned non-JSON response with HTTP ${response.status}`);
    }
    if (!response.ok) throw new Error(data.detail || `Request failed with HTTP ${response.status}`);
    lastResult = data.result || "";
    resultEl.textContent = lastResult;
    if (data.extracted_text) ocrText.value = data.extracted_text;
    statusEl.textContent = `Done in ${data.elapsed_seconds}s · ${data.model}`;
  } catch (error) {
    statusEl.textContent = "Error";
    resultEl.textContent = error.message;
  } finally {
    window.clearInterval(timer);
    submitBtn.disabled = false;
    const elapsed = ((performance.now() - started) / 1000).toFixed(1);
    if (statusEl.textContent === "Processing...") statusEl.textContent = `Still processing after ${elapsed}s`;
  }
});

updateFields();
