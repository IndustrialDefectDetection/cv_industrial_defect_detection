"""Direct Streamlit commands must not expose MES tools to the local network."""

from pathlib import Path


def test_streamlit_defaults_bind_to_loopback():
    project_root = Path(__file__).resolve().parent.parent
    config = (project_root / ".streamlit" / "config.toml").read_text(
        encoding="utf-8"
    )

    assert 'address = "127.0.0.1"' in config
    assert "enableCORS = true" in config
    assert "enableXsrfProtection = true" in config
