import os
import time

from prn import watch

from .conftest import test_config_text


class FakeCode:
    def __init__(self):
        self.fp = ("v1",)
        self.import_error = None
        self.import_checks = 0

    def fingerprint(self):
        return self.fp

    def check_import(self):
        self.import_checks += 1
        return self.import_error


def make(tmp_path, code):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(test_config_text())
    return cfg, watch.ChangeWatcher(config_path=lambda: cfg, fingerprint=code.fingerprint,
                                    import_check=code.check_import)


def touch(path, text):
    path.write_text(text)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))  # guarantee an mtime change


def test_no_changes_no_actions(tmp_path):
    _, w = make(tmp_path, FakeCode())
    assert w.check() == watch.WatchResult()


def test_valid_config_save_triggers_repoll_once(tmp_path):
    cfg, w = make(tmp_path, FakeCode())
    touch(cfg, cfg.read_text().replace("poll_interval_seconds: 300", "poll_interval_seconds: 120"))
    assert w.check().repoll is True
    assert w.check().repoll is False


def test_invalid_config_reported_once_then_recovers(tmp_path):
    cfg, w = make(tmp_path, FakeCode())
    good = cfg.read_text()
    touch(cfg, good.replace("draft: false", "draft: nope", 1))
    r = w.check()
    assert r.repoll is False and "draft" in r.config_error
    assert w.check().config_error is None          # not re-reported until the next save
    touch(cfg, good)
    assert w.check().repoll is True


def test_code_change_restarts_only_after_settling(tmp_path):
    code = FakeCode()
    _, w = make(tmp_path, code)
    code.fp = ("v2-partial",)
    assert w.check().restart is False                # first sighting: wait
    code.fp = ("v2",)
    assert w.check().restart is False                # still changing: wait again
    r = w.check()                                    # stable for one tick
    assert r.restart is True and code.import_checks == 1


def test_broken_code_is_not_restarted_and_reported_once(tmp_path):
    code = FakeCode()
    _, w = make(tmp_path, code)
    code.fp, code.import_error = ("v2",), "SyntaxError: invalid syntax"
    w.check()
    r = w.check()
    assert r.restart is False and "SyntaxError" in r.code_error
    assert w.check() == watch.WatchResult()          # no retry/spam while unchanged
    code.fp, code.import_error = ("v3",), None       # fixed
    w.check()
    assert w.check().restart is True


def test_real_fingerprint_sees_source_edits(tmp_path):
    pkg = tmp_path / "prn"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    before = watch.source_fingerprint(pkg)
    time.sleep(0.01)
    (pkg / "a.py").write_text("x = 22\n")
    assert watch.source_fingerprint(pkg) != before


def test_import_check_passes_for_current_code():
    assert watch.new_code_imports() is None
