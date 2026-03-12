from loguru import logger

from nano_alice.cli import commands


def test_setup_logging_replaces_default_console_sink(monkeypatch, tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("nano_alice.config.loader.get_data_dir", lambda: data_dir)

    commands._setup_logging(enable_console=True, console_level="INFO")
    try:
        logger.patch(lambda record: record.update(name="nano_alice.test")).info("hello logging")
        captured = capsys.readouterr()
    finally:
        logger.remove()

    assert "hello logging" in captured.out
    assert " | INFO  | hello logging" in captured.out
    assert captured.err == ""
    assert (data_dir / "logs" / "nano-alice.log").exists()
