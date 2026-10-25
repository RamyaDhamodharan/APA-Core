"""Availability checks for Docling's locally cached OCR/layout artifacts."""

import os
from pathlib import Path

OCR_MODELS_UNAVAILABLE_MESSAGE = (
    "OCR models are not installed. Ask an admin to run the model download."
)


def docling_artifacts_path() -> Path:
    configured_path = os.getenv("DOCLING_ARTIFACTS_PATH")
    if configured_path:
        return Path(configured_path)
    return Path.home() / ".cache" / "docling" / "models"


_MODEL_WEIGHT_SUFFIXES = {".bin", ".onnx", ".pt", ".pth", ".safetensors"}


def _contains_model_weights(directory: Path) -> bool:
    return directory.is_dir() and any(
        path.is_file() and path.suffix.casefold() in _MODEL_WEIGHT_SUFFIXES
        for path in directory.rglob("*")
    )


def docling_models_available(artifacts_path: Path | None = None) -> bool:
    """Return whether offline layout, table structure, and OCR assets are present."""
    root = artifacts_path or docling_artifacts_path()
    layout_path = root / "docling-project--docling-layout-heron"
    tableformer_path = (
        root
        / "docling-project--docling-models"
        / "model_artifacts"
        / "tableformer"
        / "accurate"
    )
    ocr_path = root / "RapidOcr"
    return (
        _contains_model_weights(layout_path)
        and (tableformer_path / "tm_config.json").is_file()
        and _contains_model_weights(tableformer_path)
        and _contains_model_weights(ocr_path)
    )
