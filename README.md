# Local Document AI

Web application for extracting and processing text from document images and PDF files using vision-capable AI models served through a 9router OpenAI-compatible API.

## Features

- OCR for JPG, JPEG, PNG, and PDF files
- Dynamic selection of vision-capable models from 9router
- Marks models as unavailable after a failed request
- PDF rendering with configurable page limits
- Translation to Indonesian or English
- Short or detailed document summaries
- Structured field extraction as JSON
- Question answering based on extracted document text
- TXT and JSON result downloads
- Upload size and request timeout controls

## Architecture

```text
Browser
  |
  v
Nginx
  |
  v
FastAPI / Uvicorn
  |
  +-- pdftoppm (PDF pages to PNG)
  |
  +-- 9router OpenAI-compatible API
```

The application fetches `/v1/models` from 9router and only displays models whose capabilities report `vision: true`. Images are sent to `/v1/chat/completions` as base64 data URLs.

## Requirements

- Python 3.10 or newer
- `poppler-utils` for PDF support
- A 9router API endpoint and API key
- Nginx and systemd for the production setup shown below

On Ubuntu or Debian:

```bash
sudo apt update
sudo apt install python3 python3-venv poppler-utils nginx
```

## Installation

```bash
git clone git@github.com:Mqdd27/ocr-web-ai.git
cd ocr-web-ai
python3 -m venv venv
venv/bin/pip install -r requirements.txt
mkdir -p uploads temp
```

Create `.env` in the project root:

```dotenv
AI_BASE_URL=https://your-9router-host.example/v1
AI_API_KEY=replace-with-your-api-key
VISION_MODEL=ollama-local/qwen3.5:2b
MAX_UPLOAD_MB=25
MAX_PDF_PAGES=10
REQUEST_TIMEOUT_SECONDS=600
VISION_CONCURRENCY=1
```

Protect the environment file:

```bash
chmod 600 .env
```

Do not commit `.env`. It is already excluded by `.gitignore`.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `AI_BASE_URL` | `https://ai.mqdd.my.id/v1` | OpenAI-compatible 9router API base URL |
| `AI_API_KEY` | Empty | API key sent as a Bearer token |
| `VISION_MODEL` | `ollama-local/qwen3.5:2b` | Initially selected OCR model |
| `MAX_UPLOAD_MB` | `25` | Maximum accepted upload size in MB |
| `MAX_PDF_PAGES` | `10` | Maximum PDF pages rendered and processed |
| `REQUEST_TIMEOUT_SECONDS` | `600` | Timeout for AI requests |
| `VISION_CONCURRENCY` | `1` | Maximum simultaneous vision requests per process |

The configured default model must be returned by `/v1/models` with `capabilities.vision` set to `true`.

## Running Locally

Environment variables from `.env` must be loaded before starting Uvicorn:

```bash
set -a
. ./.env
set +a
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8500
```

Open `http://127.0.0.1:8500`.

Health check:

```bash
curl http://127.0.0.1:8500/health
```

## Production with systemd

Example `/etc/systemd/system/local-document-ai.service`:

```ini
[Unit]
Description=Local Document AI OCR Web App
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your-user
Group=your-user
WorkingDirectory=/opt/local-document-ai
EnvironmentFile=/opt/local-document-ai/.env
ExecStart=/opt/local-document-ai/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8500 --workers 1
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/local-document-ai/uploads /opt/local-document-ai/temp

[Install]
WantedBy=multi-user.target
```

The service user must be able to traverse the project directory and write to `uploads` and `temp`:

```bash
sudo chown -R your-user:your-user /opt/local-document-ai
sudo systemctl daemon-reload
sudo systemctl enable --now local-document-ai
sudo systemctl status local-document-ai
```

View service logs:

```bash
sudo journalctl -u local-document-ai -f
```

## Nginx Reverse Proxy

Example server block:

```nginx
server {
    listen 80;
    server_name ocr.example.com;

    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:8500;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
        proxy_connect_timeout 60s;
    }
}
```

Validate and reload Nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## Usage

1. Select an OCR model.
2. Choose an action.
3. Upload a JPG, JPEG, PNG, or PDF document.
4. Select action-specific options when needed.
5. Click **Process**.
6. Copy the result or download it as TXT or JSON.

After OCR completes, the extracted text is kept in the form. Translation, summarization, structured extraction, and question answering can reuse that text without uploading the document again.

## Model Availability

The model list comes from 9router at runtime. A model is labeled `(Unavailable)` and disabled after a request to that model returns an error. A later successful request removes its unavailable state.

Availability state is stored in application memory. It resets whenever the service restarts and is not shared between multiple Uvicorn workers.

## API Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Web interface |
| `GET` | `/health` | Application health and active configuration |
| `GET` | `/models` | Vision models and observed availability |
| `POST` | `/process` | OCR and document processing |
| `POST` | `/download/txt` | Download text output |
| `POST` | `/download/json` | Download JSON output |

## Troubleshooting

### Bad Gateway

Confirm that the service is running and port `8500` is listening:

```bash
sudo systemctl status local-document-ai
ss -ltn | grep ':8500'
curl http://127.0.0.1:8500/health
```

A `502 Bad Gateway` commonly means Nginx cannot connect to Uvicorn. Check the service logs:

```bash
sudo journalctl -u local-document-ai -n 100 --no-pager
```

### Permission denied at `CHDIR`

The user configured in the systemd unit must have execute permission on every parent directory and read access to the project:

```bash
namei -l /opt/local-document-ai
```

Adjust ownership or directory permissions, then restart the service.

### Models do not load

Verify the API URL and key directly:

```bash
curl -H "Authorization: Bearer $AI_API_KEY" "$AI_BASE_URL/models"
```

A `401` response means the API key is missing or invalid.

### PDF processing fails

Confirm that `pdftoppm` is installed:

```bash
pdftoppm -v
```

## Security Notes

- Keep `AI_API_KEY` only in `.env` or another protected secret store.
- Use HTTPS for public deployments.
- Run the service as an unprivileged user.
- Keep upload and PDF limits appropriate for available memory and API capacity.
- Do not expose Uvicorn directly when Nginx is the public entry point.
