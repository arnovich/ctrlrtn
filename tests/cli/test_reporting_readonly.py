"""Read-only commands must never create a database.

Running ``ctrlrtn usecases`` in the wrong directory used to leave an empty
``ctrlrtn.db`` behind and print an empty table, which reads as "no traffic"
rather than "wrong database".
"""

from __future__ import annotations

import pytest

from ctrlrtn.cli.commands import main


@pytest.mark.parametrize(
    "argv",
    [["usecases"], ["calls"], ["tasks"], ["sessions"], ["show", "1"]],
)
def test_reports_refuse_a_missing_database(tmp_path, monkeypatch, capsys, argv):
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("CTRLRTN_DB", str(missing))

    with pytest.raises(SystemExit) as excinfo:
        main(argv)

    assert excinfo.value.code == 2
    assert not missing.exists()
    err = capsys.readouterr().err
    assert f"no database at {missing}" in err
    assert "ctrlrtn serve" in err


def test_serve_announces_where_it_records(tmp_path, monkeypatch, capsys):
    """The one line an operator needs when the CLI and the gateway disagree
    about which file they are looking at."""
    import ctrlrtn.cli.runtime as runtime

    db = tmp_path / "gateway.db"
    monkeypatch.setenv("CTRLRTN_DB", str(db))
    monkeypatch.setenv("CTRLRTN_PORT", "4123")
    monkeypatch.setattr(runtime.uvicorn, "run", lambda *a, **k: None)

    main(["serve"])

    err = capsys.readouterr().err
    assert "listening on 127.0.0.1:4123" in err
    assert f"recording to {db}" in err
