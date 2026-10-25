from pathlib import Path

from src.services import docling_model_services


def test_docling_models_available_requires_layout_table_and_ocr(tmp_path: Path):
    root = tmp_path
    required_dirs = (
        root / "docling-project--docling-layout-heron",
        root
        / "docling-project--docling-models"
        / "model_artifacts"
        / "tableformer"
        / "accurate",
        root / "RapidOcr",
    )
    for directory in required_dirs:
        directory.mkdir(parents=True)
        (directory / "model.bin").write_bytes(b"artifact")
    (required_dirs[1] / "tm_config.json").write_text("{}", encoding="utf-8")

    assert docling_model_services.docling_models_available(root)
    (required_dirs[-1] / "model.bin").unlink()
    assert not docling_model_services.docling_models_available(root)


def test_docling_artifacts_path_uses_configured_path(monkeypatch, tmp_path: Path):
    configured_path = tmp_path / "docling-artifacts"
    monkeypatch.setenv("DOCLING_ARTIFACTS_PATH", str(configured_path))

    assert docling_model_services.docling_artifacts_path() == configured_path


def test_worker_startup_logs_model_availability(monkeypatch, caplog):
    from src.worker import celery_app

    monkeypatch.setattr(celery_app, "docling_models_available", lambda: False)
    with caplog.at_level("INFO"):
        celery_app.log_docling_model_availability()

    assert "Docling OCR/layout models available: no" in caplog.text
