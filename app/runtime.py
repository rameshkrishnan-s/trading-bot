"""Linux process locks. Acquire before initializing any trading state."""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another process owns this bot/account lock; no state was changed") from None
        yield
    finally:
        os.close(fd)
    # Never unlink a lock file: waiting processes may still hold its inode.


@contextmanager
def engine_locks(config):
    with ExitStack() as stack:
        stack.enter_context(file_lock(str(config.db_path.resolve()) + '.lock'))
        if config.mode != 'demo':
            # Cross-checkout / cross-port protection for the same local account.
            # This is not a distributed lock; only one VM may run the account.
            identity = config.angel_client_id if config.data_provider == 'angel' else 'upstox'
            key = hashlib.sha256((config.data_provider + ':' + identity).encode()).hexdigest()
            directory = Path(tempfile.gettempdir()) / f'options-desk-{os.getuid()}'
            stack.enter_context(file_lock(directory / (key + '.lock')))
        yield
