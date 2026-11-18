"""Apply POSIX limits before exec, without using a threaded-parent preexec_fn.

Invoked by absolute filename with the worker's Python; intentionally stdlib-only
and independent of Django settings. Children inherit these limits across exec.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    if sys.platform == "win32":
        raise RuntimeError("POSIX converter limits are unavailable on Windows")
    import resource

    file_bytes, address_bytes, cpu_seconds = map(int, sys.argv[1:4])
    limits = [
        (resource.RLIMIT_FSIZE, file_bytes),
        (resource.RLIMIT_CPU, cpu_seconds),
    ]
    if address_bytes:
        limits.append((resource.RLIMIT_AS, address_bytes))
    for kind, value in limits:
        _, inherited_hard = resource.getrlimit(kind)
        if inherited_hard != resource.RLIM_INFINITY:
            value = min(value, inherited_hard)
        resource.setrlimit(kind, (value, value))
    os.execvpe(sys.argv[4], sys.argv[4:], os.environ)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"converter resource setup/exec failed ({type(exc).__name__})\n")
        sys.exit(126)
