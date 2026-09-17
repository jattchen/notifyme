import json
import os
import stat
import tempfile

from .bark import BarkEndpoint
from .errors import NotifyMeError
from .paths import chmod_private_file, ensure_private_dir, state_home


class Binding:
    def __init__(self, home=None):
        self.home = home if home is not None else state_home()
        self.path = self.home / "binding.json"

    def save(self, endpoint):
        if not isinstance(endpoint, BarkEndpoint):
            raise NotifyMeError("invalid_bark_url", "Bark 地址未完成校验")
        ensure_private_dir(self.home)
        payload = json.dumps(endpoint.to_stored(), ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(self.home), prefix=".binding.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
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
        if not self.path.exists():
            raise NotifyMeError("activation_required", "尚未绑定 Bark，请先在终端运行 setup")
        if self.home.exists() and stat.S_IMODE(self.home.stat().st_mode) & 0o077:
            raise NotifyMeError("insecure_binding", "Bark 状态目录权限过宽")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise NotifyMeError("insecure_binding", "Bark 绑定文件权限过宽")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise NotifyMeError("invalid_binding", "无法读取 Bark 绑定")
        return BarkEndpoint.from_stored(data)

    def public_view(self):
        if not self.path.exists():
            return {"bound": False, "host": None}
        try:
            return self.load().public_view()
        except NotifyMeError:
            return {"bound": False, "host": None}
