# Docling OCR and layout models

Docling 2.140 reads the artifact directory from `DOCLING_ARTIFACTS_PATH`. The
Compose services use `/root/.cache/huggingface/docling-models`; `core`, `worker`,
and `worker-email` all mount the same named volume at `/root/.cache/huggingface`.
The downloader uses Docling's `docling.utils.model_downloader.download_models`
utility and Hugging Face Hub's resumable cache. An interrupted download can be
rerun safely; completed files remain in the volume.

Model downloads are deliberately not part of the image build. After starting the
Compose stack, run this once (or rerun it after an interrupted download):

```powershell
docker compose exec core uv run --no-sync python -m scripts.download_models
```

For a Hugging Face mirror, set `HF_ENDPOINT` in the shell or `.env` before
starting Compose. The script uses a 600 second download timeout, a 120 second
metadata timeout, and retries interrupted downloads. The defaults can be
overridden with `HF_HUB_DOWNLOAD_TIMEOUT` and `HF_HUB_ETAG_TIMEOUT`.

To verify OCR with a local PNG invoice from PowerShell, copy it into the running
core container and ask Docling to extract text:

```powershell
docker compose cp .\invoice.png core:/tmp/invoice.png
docker compose exec core uv run --no-sync python -c "from docling.document_converter import DocumentConverter; text = DocumentConverter().convert('/tmp/invoice.png').document.export_to_markdown(); print(text); assert text.strip(), 'No OCR text extracted'"
```

The core startup log reports `Docling OCR/layout models available: yes` only
when layout, table structure, and RapidOCR artifacts are all present. An invoice
that needs OCR while those artifacts are absent receives the actionable
`OCR models are not installed. Ask an admin to run the model download.` message.
