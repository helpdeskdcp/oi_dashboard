"""
test_no_committed_secrets.py -- regression test for the committed NSE
cookie jar.

nse_cookies.txt (Akamai bot-detection cookies: bm_sz / _abck / AKA_A2)
was tracked in git. Those are IP- and session-bound credentials with real
expiries; nse_fetcher.py regenerates them at runtime via its own
_bootstrap_session(), so nothing needs them on disk in the repo. This
test fails if any cookie jar is committed again, and pins the .gitignore
patterns that prevent it.
"""
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Substrings that only appear in a real browser/Akamai cookie jar, never in
# this repo's source. Kept as fragments so this file never contains a
# complete, greppable cookie value of its own.
_COOKIE_JAR_MARKERS = ("Netscape HTTP Cookie File", "_abck", "bm_sz")


def _tracked_files():
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:   # not a git checkout (e.g. an exported tarball)
        return None
    return result.stdout.split()


class TestNoCookieJarIsCommitted:
    def test_nse_cookies_file_is_not_tracked(self):
        tracked = _tracked_files()
        if tracked is None:
            return
        assert "nse_cookies.txt" not in tracked

    def test_no_tracked_file_looks_like_a_cookie_jar(self):
        tracked = _tracked_files()
        if tracked is None:
            return
        offenders = []
        for rel in tracked:
            path = os.path.join(REPO_ROOT, rel)
            if rel == os.path.basename(__file__) or not os.path.isfile(path):
                continue
            if os.path.getsize(path) > 2_000_000:
                continue
            with open(path, "r", errors="ignore") as fh:
                head = fh.read(4096)
            if any(marker in head for marker in _COOKIE_JAR_MARKERS):
                offenders.append(rel)
        assert offenders == [], f"cookie-jar-looking files are committed: {offenders}"

    def test_gitignore_covers_cookie_jars(self):
        with open(os.path.join(REPO_ROOT, ".gitignore")) as fh:
            patterns = {line.strip() for line in fh}
        assert "nse_cookies.txt" in patterns
        assert "*cookies.txt" in patterns
