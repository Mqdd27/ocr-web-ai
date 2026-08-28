import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx
from starlette.background import BackgroundTask
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
TEMP_DIR = BASE_DIR / "temp"
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://ai.mqdd.my.id/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "ollama-local/qwen3.5:2b")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "10"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "600"))
VISION_CONCURRENCY = int(os.getenv("VISION_CONCURRENCY", "1"))

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
ALLOWED_MIME = {"image/jpeg", "image/png", "application/pdf"}

app = FastAPI(title="Local Document AI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)
vision_semaphore = asyncio.Semaphore(VISION_CONCURRENCY)
unavailable_models: dict[str, str] = {}
TEMP_DIR.mkdir(parents=True, exist_ok=True)


OCR_PROMPT = """Extract all visible text from this document exactly and completely.
Do not summarize or translate.
Preserve reading order, line breaks, headings, numbers, dates, currency,
tables, and punctuation as accurately as possible.
Return only the extracted document text."""


def validate_upload(file: UploadFile) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file extension.")
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=400, detail="Unsupported MIME type.")
    return suffix


async def save_upload(file: UploadFile, suffix: str, work_dir: Path) -> Path:
    target = work_dir / f"upload-{next(tempfile._get_candidate_names())}{suffix}"
    total = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024
    with target.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HTTPException(status_code=413, detail=f"Upload exceeds {MAX_UPLOAD_MB} MB limit.")
            handle.write(chunk)
    return target


def verify_image(path: Path) -> None:
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid or unreadable image file.") from exc


def convert_pdf_to_images(pdf_path: Path, work_dir: Path) -> list[Path]:
    prefix = work_dir / "page"
    cmd = [
        "pdftoppm",
        "-r",
        "160",
        "-png",
        "-f",
        "1",
        "-l",
        str(MAX_PDF_PAGES),
        str(pdf_path),
        str(prefix),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="PDF support requires poppler-utils/pdftoppm.") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=400, detail=f"Could not render PDF: {exc.stderr.strip()}") from exc
    pages = sorted(work_dir.glob("page-*.png"))
    if not pages:
        raise HTTPException(status_code=400, detail="PDF did not render any pages.")
    return pages


def image_to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


async def get_vision_models() -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{AI_BASE_URL}/models", headers={"Authorization": f"Bearer {AI_API_KEY}"})
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"9router models error: {response.text[:500]}")
    return sorted(item["id"] for item in response.json().get("data", []) if item.get("capabilities", {}).get("vision"))

@app.get("/models")
async def models():
    return [{"id": model, "available": model not in unavailable_models, "error": unavailable_models.get(model)} for model in await get_vision_models()]

async def ai_chat(messages: list[dict], model: str, timeout: Optional[int] = None) -> str:
    models = await get_vision_models()
    if model not in models:
        raise HTTPException(status_code=400, detail="Unsupported vision model.")
    payload = {"model": model, "messages": messages, "temperature": 0.0}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout or REQUEST_TIMEOUT_SECONDS)) as client:
        response = await client.post(f"{AI_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {AI_API_KEY}"}, json=payload)
    if response.status_code >= 400:
        unavailable_models[model] = response.text[:200]
        raise HTTPException(status_code=502, detail=f"9router error: {response.text[:500]}")
    unavailable_models.pop(model, None)
    return response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()

async def ocr_image(path: Path, model: str) -> str:
    encoded = image_to_base64(path)
    messages = [{"role": "user", "content": [{"type": "text", "text": OCR_PROMPT}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]}]
    async with vision_semaphore:
        return await ai_chat(messages, model)


async def ocr_file(path: Path, suffix: str, work_dir: Path, model: str) -> str:
    if suffix == ".pdf":
        pages = convert_pdf_to_images(path, work_dir)
        results = []
        for idx, page in enumerate(pages, start=1):
            text = await ocr_image(page, model)
            results.append(f"--- Page {idx} ---\n{text}")
        return "\n\n".join(results)
    verify_image(path)
    return await ocr_image(path, model)


async def operate_on_text(action: str, text: str, model: str, target_language: str = "Indonesian", detail: str = "short", fields: str = "", question: str = "") -> str:
    if not text.strip():
        raise HTTPException(status_code=400, detail="Extracted text is required for this action.")
    prompts = {
        "translate": f"Translate the following document text to {target_language}. Preserve meaning, names, dates, numbers, and formatting where practical. Return only the translation.\n\n{text}",
        "summarize": f"Summarize the following document text. Summary style: {detail}. Return only the summary.\n\n{text}",
        "extract": f"Extract the requested fields from the document text and return valid JSON only. Use null for missing values. Requested fields: {fields or 'infer important fields'}.\n\nDocument text:\n{text}",
        "ask": f"Answer the user's question using only the document text below. If the answer is not in the text, say so clearly.\n\nQuestion: {question}\n\nDocument text:\n{text}",
    }
    if action not in prompts:
        raise HTTPException(status_code=400, detail="Unsupported action.")
    return await ai_chat([{"role": "user", "content": prompts[action]}], model)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    models = await get_vision_models()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "model": VISION_MODEL,
            "models": models,
            "max_upload_mb": MAX_UPLOAD_MB,
            "max_pdf_pages": MAX_PDF_PAGES,
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "model": VISION_MODEL, "ai_base_url": AI_BASE_URL}


@app.post("/process")
async def process(
    file: Optional[UploadFile] = File(None),
    action: str = Form("ocr"),
    ocr_text: str = Form(""),
    target_language: str = Form("Indonesian"),
    detail: str = Form("short"),
    fields: str = Form(""),
    question: str = Form(""),
    model: str = Form(VISION_MODEL),
):
    started = time.monotonic()
    work_dir = Path(tempfile.mkdtemp(prefix="docai-", dir=TEMP_DIR))
    try:
        extracted_text = ocr_text.strip()
        if action == "ocr" or not extracted_text:
            if file is None:
                raise HTTPException(status_code=400, detail="Upload a document or provide previously extracted text.")
            suffix = validate_upload(file)
            upload_path = await save_upload(file, suffix, work_dir)
            extracted_text = await ocr_file(upload_path, suffix, work_dir, model)
            result = extracted_text if action == "ocr" else await operate_on_text(action, extracted_text, model, target_language, detail, fields, question)
        else:
            result = await operate_on_text(action, extracted_text, model, target_language, detail, fields, question)
        elapsed = round(time.monotonic() - started, 2)
        structured = None
        if action == "extract":
            try:
                structured = json.loads(result)
            except json.JSONDecodeError:
                structured = None
        return JSONResponse(
            {
                "action": action,
                "model": model,
                "elapsed_seconds": elapsed,
                "extracted_text": extracted_text,
                "result": result,
                "structured": structured,
            }
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.post("/download/txt")
async def download_txt(content: str = Form(...)):
    path = TEMP_DIR / f"result-{next(tempfile._get_candidate_names())}.txt"
    path.write_text(content, encoding="utf-8")
    return FileResponse(
        path,
        filename="document-ai-result.txt",
        media_type="text/plain",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.post("/download/json")
async def download_json(content: str = Form(...)):
    path = TEMP_DIR / f"result-{next(tempfile._get_candidate_names())}.json"
    try:
        parsed = json.loads(content)
        path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    except json.JSONDecodeError:
        path.write_text(content, encoding="utf-8")
    return FileResponse(
        path,
        filename="document-ai-result.json",
        media_type="application/json",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def general_exception_handler(_: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})
