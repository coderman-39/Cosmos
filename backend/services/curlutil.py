"""curl argument builder that keeps secrets OFF the process argv.

Anything passed on argv is visible to every process on the machine
(`ps aux`, Activity Monitor). Auth headers and credential-bearing bodies are
written to chmod-0600 files inside a chmod-0700 temp dir instead, and curl
reads them via its @file forms:  -H @file  /  --data-binary @file
(both supported since curl 7.55; this machine ships 8.x).

Usage:
    sf = SecretFiles()
    try:
        args = ["-X", "POST", url, *sf.header(f"Authorization: Bearer {tok}"),
                *sf.data(payload)]
        ... run curl ...
    finally:
        sf.cleanup()
"""

import os
import shutil
import tempfile


class SecretFiles:
    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="cosmos_curl_")
        os.chmod(self._dir, 0o700)
        self._n = 0

    def _write(self, content: str) -> str:
        self._n += 1
        path = os.path.join(self._dir, f"s{self._n}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def header(self, header_line: str) -> list[str]:
        """A sensitive -H header, e.g. 'Authorization: Bearer …'."""
        return ["-H", f"@{self._write(header_line.rstrip() + chr(10))}"]

    def data(self, payload: str) -> list[str]:
        """A sensitive/large request body. --data-binary preserves newlines
        (plain --data would strip them from JSON payloads)."""
        return ["--data-binary", f"@{self._write(payload)}"]

    def cleanup(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
