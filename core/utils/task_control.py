"""OS-held locks for tasks sharing a game or its translation cache."""
from contextlib import contextmanager, ExitStack
import hashlib
import errno
import os
import tempfile

from core.utils.text_processing import sanitize_filename


class WorkBusyError(RuntimeError):
    pass


@contextmanager
def exclusive_work(game_path, works_dir):
    # Also lock the cache: different game paths can share the same Works name.
    folder = sanitize_filename(os.path.basename(game_path)) or "UntitledGame"
    resources = (game_path, os.path.join(works_dir, folder))
    lock_dir = os.path.join(tempfile.gettempdir(), "WindyTranslator-task-locks")
    os.makedirs(lock_dir, exist_ok=True)
    keys = sorted({hashlib.sha256(os.path.normcase(os.path.realpath(path)).encode("utf-8")).hexdigest()
                   for path in resources})
    with ExitStack() as handles:
        for key in keys:
            handle = handles.enter_context(open(os.path.join(lock_dir, key + ".lock"), "a+b"))
            if not os.fstat(handle.fileno()).st_size:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                raise WorkBusyError("该作品或翻译目录已有任务正在运行，请等待其结束后再试。") from error
        # Closing the handles releases the locks, including after a crash/kill.
        # Leave the files in place: unlinking an active lock permits a second inode.
        yield
