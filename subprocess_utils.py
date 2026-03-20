"""Subprocess helper — hides console windows on Windows + cancellation support."""

import subprocess
import sys
import threading

# On Windows, prevent ffmpeg/ffprobe from flashing a console window
_CREATION_FLAGS = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)

# ── Global cancel infrastructure ────────────────────────────────────────────
_cancel_flag = threading.Event()
_active_processes: list[subprocess.Popen] = []
_lock = threading.Lock()


def request_cancel():
    """Signal all running subprocesses to stop."""
    _cancel_flag.set()
    with _lock:
        for proc in _active_processes:
            try:
                proc.terminate()
            except OSError:
                pass


def reset_cancel():
    """Clear the cancel flag (call before starting a new pipeline)."""
    _cancel_flag.clear()
    with _lock:
        _active_processes.clear()


def is_cancelled() -> bool:
    """Check if cancellation has been requested."""
    return _cancel_flag.is_set()


class CancelledError(Exception):
    """Raised when a subprocess is interrupted by cancellation."""
    pass


def run(*args, **kwargs):
    """subprocess.run() wrapper with cancellation support.

    Polls the process every 0.5s. If cancel is requested, terminates the
    process and raises CancelledError. Also hides console windows on Windows.
    """
    if _cancel_flag.is_set():
        raise CancelledError("Pipeline cancelled")

    kwargs.setdefault("creationflags", _CREATION_FLAGS)

    # Translate capture_output into Popen-compatible args
    capture_output = kwargs.pop("capture_output", False)
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)

    timeout = kwargs.pop("timeout", None)
    check = kwargs.pop("check", False)

    proc = subprocess.Popen(*args, **kwargs)
    with _lock:
        _active_processes.append(proc)

    try:
        # Drain stdout/stderr in background threads to prevent pipe deadlock.
        # FFmpeg writes heavily to stderr (progress, stats). If the pipe buffer
        # fills (~64KB) and nobody reads it, FFmpeg blocks → deadlock.
        stdout_chunks = []
        stderr_chunks = []

        def _drain(pipe, buf):
            try:
                while True:
                    chunk = pipe.read(8192)
                    if not chunk:
                        break
                    buf.append(chunk)
            except Exception:
                pass

        drain_threads = []
        if proc.stdout:
            t = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks), daemon=True)
            t.start()
            drain_threads.append(t)
        if proc.stderr:
            t = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
            t.start()
            drain_threads.append(t)

        # Poll the process, checking cancel flag every 0.5s
        elapsed = 0.0
        poll_interval = 0.5
        while proc.poll() is None:
            if _cancel_flag.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise CancelledError("Pipeline cancelled")
            if timeout is not None and elapsed >= timeout:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise subprocess.TimeoutExpired(proc.args, timeout)
            # Wait a bit before next poll
            try:
                proc.wait(timeout=poll_interval)
            except subprocess.TimeoutExpired:
                pass
            elapsed += poll_interval

        # Check cancel one more time after process exits (process may have been
        # killed externally by request_cancel via _active_processes)
        if _cancel_flag.is_set():
            raise CancelledError("Pipeline cancelled")

        # Wait for drain threads to finish reading
        for t in drain_threads:
            t.join(timeout=5)

        # Combine captured output
        if stdout_chunks:
            joiner = b'' if isinstance(stdout_chunks[0], bytes) else ''
            stdout = joiner.join(stdout_chunks)
        else:
            stdout = None
        if stderr_chunks:
            joiner = b'' if isinstance(stderr_chunks[0], bytes) else ''
            stderr = joiner.join(stderr_chunks)
        else:
            stderr = None

        result = subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )
        return result
    finally:
        # Clean up pipes
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if proc.stderr:
            try:
                proc.stderr.close()
            except Exception:
                pass
        with _lock:
            try:
                _active_processes.remove(proc)
            except ValueError:
                pass
