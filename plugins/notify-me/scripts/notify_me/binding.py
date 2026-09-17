import errno
import json
import os
import stat
import tempfile

from .bark import BarkEndpoint
from .errors import NotifyMeError
from .paths import chmod_private_file, ensure_private_dir, state_home


def _refuse_insecure_binding_path(path):
    if path.is_symlink():
        raise NotifyMeError("insecure_binding", "binding.json 不能是符号链接")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise NotifyMeError("insecure_binding", "binding.json 不是普通文件")


class Binding:
    def __init__(self, home=None):
        self.home = home if home is not None else state_home()
        self.path = self.home / "binding.json"

    def save(self, endpoint):
        if not isinstance(endpoint, BarkEndpoint):
            raise NotifyMeError("invalid_bark_url", "Bark 地址未完成校验")
        _refuse_insecure_binding_path(self.path)
        if self.home.exists() and stat.S_IMODE(self.home.stat().st_mode) & 0o077:
            raise NotifyMeError("insecure_binding", "Bark 状态目录权限过宽")
        ensure_private_dir(self.home)
        payload = json.dumps(endpoint.to_stored(), ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(self.home), prefix=".binding.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            _refuse_insecure_binding_path(self.path)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        chmod_private_file(self.path)
        return endpoint.public_view()

    def load(self):
        _refuse_insecure_binding_path(self.path)
        if not self.path.exists():
            raise NotifyMeError("activation_required", "尚未绑定 Bark，请先在终端运行 setup")
        if self.home.exists() and stat.S_IMODE(self.home.stat().st_mode) & 0o077:
            raise NotifyMeError("insecure_binding", "Bark 状态目录权限过宽")
        flags = os.O_RDONLY | os.O_NOFOLLOW
        try:
            fd = os.open(str(self.path), flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP or self.path.is_symlink():
                raise NotifyMeError("insecure_binding", "binding.json 不能是符号链接")
            raise NotifyMeError("invalid_binding", "无法读取 Bark 绑定")
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise NotifyMeError("insecure_binding", "binding.json 不是普通文件")
            if stat.S_IMODE(opened.st_mode) & 0o077:
                raise NotifyMeError("insecure_binding", "Bark 绑定文件权限过宽")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = None
                try:
                    data = json.loads(handle.read())
                except (OSError, ValueError, UnicodeError):
                    raise NotifyMeError("invalid_binding", "无法读取 Bark 绑定")
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return BarkEndpoint.from_stored(data)

    def public_view(self):
        if not self.path.exists():
            return {"bound": False, "host": None}
        try:
            return self.load().public_view()
        except NotifyMeError:
            return {"bound": False, "host": None}
