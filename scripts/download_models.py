"""Download Docling OCR/layout models into the persistent artifacts volume."""

import logging
import os
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("docling_model_download")


def main() -> None:
    artifacts_path = Path(
        os.getenv(
            "DOCLING_ARTIFACTS_PATH",
            Path.home() / ".cache" / "docling" / "models",
        )
    )
    artifacts_path.mkdir(parents=True, exist_ok=True)

    # Hugging Face Hub honors these settings and resumes cached partial files.
    os.environ.setdefault("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "120")
    os.environ.setdefault("HF_HUB_MAX_RETRIES", "10")
    logger.info("Downloading Docling models to %s", artifacts_path)

    from docling.utils.model_downloader import download_models
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel

    delays = (5, 15, 45, 90)
    for attempt in range(len(delays) + 1):
        try:
            download_models(output_dir=artifacts_path, progress=True)
            RapidOcrModel.download_models(
                backend="onnxruntime",
                local_dir=artifacts_path / "RapidOcr",
                progress=True,
            )
            logger.info("Docling model download finished")
            return
        except Exception as exc:
            if attempt == len(delays):
                logger.error(
                    "Docling model download failed after retries (%s)",
                    type(exc).__name__,
                )
                raise SystemExit(
                    "Docling model download failed after retries; inspect network "
                    "configuration and rerun the command."
                ) from None
            delay = delays[attempt]
            logger.warning(
                "Docling model download interrupted (%s); retrying in %s seconds. "
                "Existing cache files are retained.",
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)


if __name__ == "__main__":
    main()
