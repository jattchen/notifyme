# -*- coding: utf-8 -*-
"""Stable, secret-free failures. Callers publish error.code and not the text."""


class NotifyMeError(Exception):
    """Expected failure. The code is the only machine-readable field."""

    def __init__(self, code):
        super(NotifyMeError, self).__init__(code)
        self.code = code
