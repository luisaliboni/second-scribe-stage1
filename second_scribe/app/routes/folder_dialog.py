"""Folder window routes (item 22): is there one, and open it.

``GET /api/project/folder-dialog`` answers ``{"available": bool}`` and opens nothing. ``POST
/api/project/choose-folder`` opens the system's folder window (``folder_dialog.choose``) and answers ``{"path": str}``
or ``{"path": null}`` when the person cancelled. The request body is not read: the command is fixed and its only text
is the app's own title string. The server has already checked the Host header, the session cookie and the CSRF
header. One window at a time (409 ``folder_dialog_open``); ``state.lock`` is not held while the window is open.
"""
from __future__ import annotations

import threading

from .. import folder_dialog, strings
from ..api import ApiError, route

_OPEN = threading.Lock()
# Tests replace this with a fake ``subprocess.run``; the real one opens a window on screen.
RUN = None


@route("GET", "/api/project/folder-dialog")
def folder_dialog_available(state, body, query):
    return {"available": folder_dialog.available(strings.t("setup.project.choose_prompt"))}


@route("POST", "/api/project/choose-folder")
def choose_folder(state, body, query):
    if not _OPEN.acquire(blocking=False):
        raise ApiError(409, "folder_dialog_open", strings.t("setup.project.choose_open"))
    try:
        kw = {"run": RUN} if RUN is not None else {}
        path = folder_dialog.choose(strings.t("setup.project.choose_prompt"), **kw)
    except folder_dialog.DialogError as e:
        if e.code == "timeout":
            raise ApiError(504, "folder_dialog_timeout", strings.t("setup.project.choose_timeout")) from None
        raise ApiError(502, "folder_dialog_failed", strings.t("setup.project.choose_failed")) from None
    finally:
        _OPEN.release()
    return {"path": path}
