"""`tastydb dashboard --host`: bare flag binds every interface (issue #3)."""
import pytest
import uvicorn
from click.testing import CliRunner

from tastydb import cli


@pytest.fixture
def run_dashboard(tmp_path, monkeypatch):
    calls = {}

    def fake_run(app, host, port, **kwargs):
        calls.update(host=host, port=port)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(cli, "_lan_ip", lambda: "192.168.1.50")

    def invoke(*args):
        result = CliRunner().invoke(cli.main, [
            "--db", f"sqlite:///{tmp_path}/t.sqlite3",
            "--env-file", str(tmp_path / "missing.env"),
            "dashboard", "--no-browser", *args,
        ])
        assert result.exit_code == 0, result.output
        return calls, result.output

    return invoke


def test_default_binds_loopback(run_dashboard):
    calls, output = run_dashboard()
    assert calls == {"host": "127.0.0.1", "port": 8787}
    assert "WARNING" not in output
    assert "192.168.1.50" not in output


def test_bare_host_binds_all_interfaces(run_dashboard):
    calls, output = run_dashboard("--host")
    assert calls == {"host": "0.0.0.0", "port": 8787}
    assert "http://192.168.1.50:8787/" in output
    assert "no authentication" in output


def test_explicit_host_passes_through(run_dashboard):
    calls, output = run_dashboard("--host", "192.168.1.5")
    assert calls["host"] == "192.168.1.5"
    assert "no authentication" in output


def test_bare_host_does_not_swallow_next_option(run_dashboard):
    calls, _ = run_dashboard("--host", "--port", "9000")
    assert calls == {"host": "0.0.0.0", "port": 9000}


def test_browser_url():
    assert cli._browser_url("0.0.0.0", 8787) == "http://127.0.0.1:8787/"
    assert cli._browser_url("::", 8787) == "http://127.0.0.1:8787/"
    assert cli._browser_url("127.0.0.1", 9000) == "http://127.0.0.1:9000/"
    assert cli._browser_url("::1", 8787) == "http://[::1]:8787/"
