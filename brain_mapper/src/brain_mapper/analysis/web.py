"""The server side of the results screen.

``start_after_zip(record)`` starts the analysis of the session once its zip is saved (``Job``, a
thread of its own; nothing it does can reach the recording, which is closed, or the zip, which is
made). The routes answer the participant page only, with the key that page got for the zip
download (``SessionRecord.allows``); no address holds a label.

|Route|Answer|
|-|-|
|``GET /api/results?key=``|``{"state": "none"|"running"|"done"|"failed", "progress", "stage", "elapsed", "message", "results"}``; ``results`` (the content of ``analysis/results.json``) only when done|
|``GET /api/results/windows?key=``|The content of ``analysis/windows.json`` once done, else 404|
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .run import RESULTS, WINDOWS, Job, finished_dir


def start_after_zip(record) -> Job | None:
    """Starts the analysis of ``record``'s session (its zip is saved); ``None`` without EEG."""
    hub = record.hub
    folder = hub.folder
    if folder is None or not (Path(folder) / "eeg" / "raw.bin").is_file():
        return None
    who = hub.participant
    return Job(Path(folder), label=who[0] if who else None).start()


def _status(record) -> dict:
    job = getattr(record, "analysis", None)
    if job is None:
        folder = record.hub.folder
        done = finished_dir(folder) if folder is not None else None
        if done is not None:
            return {"state": "done", "progress": 1.0, "stage": "Done", "elapsed": None, "message": None}
        return {"state": "none", "progress": 0.0, "stage": None, "elapsed": None, "message": None}
    return job.status()


def routes(hub) -> list[Route]:
    def refusal(status: int, code: str, text: str) -> JSONResponse:
        return JSONResponse({"error": text, "code": code}, status_code=status)

    def allowed(request: Request) -> bool:
        return hub.record.allows(request.query_params.get("key", ""))

    def _file(name: str) -> bytes | None:
        folder = hub.folder
        done = finished_dir(folder) if folder is not None else None
        if done is None:
            return None
        return (done / name).read_bytes()

    async def results(request: Request) -> Response:
        if not allowed(request):
            return refusal(403, "not_allowed", "Only the participant page can see the results.")
        status = _status(hub.record)
        status["results"] = None
        if status["state"] == "done":
            body = _file(RESULTS)
            status["results"] = json.loads(body) if body is not None else None
        return JSONResponse(status)

    async def windows(request: Request) -> Response:
        if not allowed(request):
            return refusal(403, "not_allowed", "Only the participant page can see the results.")
        body = _file(WINDOWS) if _status(hub.record)["state"] == "done" else None
        if body is None:
            return refusal(404, "not_ready", "The results are not ready.")
        return Response(body, media_type="application/json")

    return [Route("/api/results", results, methods=["GET"]), Route("/api/results/windows", windows, methods=["GET"])]
