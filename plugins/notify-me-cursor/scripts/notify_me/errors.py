class NotifyMeError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self):
        error = {"code": self.code, "message": self.message}
        error.update(self.extra)
        return {"ok": False, "error": error}
