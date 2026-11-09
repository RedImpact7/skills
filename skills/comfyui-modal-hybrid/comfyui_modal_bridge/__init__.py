"""
ComfyUI Modal Bridge - local custom node for Comfy Desktop.

Adds a "Run on Modal" button. The current workflow is sent to your Modal GPU
worker; generated images/videos come back into  output/modal/  on this laptop.

Config (either way):
  * click the gear in the Modal panel inside ComfyUI, or
  * env vars MODAL_COMFY_ENDPOINT / MODAL_COMFY_TOKEN, or
  * config.json next to this file: {"endpoint": "...", "token": "..."}
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid

import aiohttp
from aiohttp import web

import folder_paths
from server import PromptServer

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}
WEB_DIRECTORY = "./js"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
PLACEHOLDER_MANIFEST = os.path.join(HERE, "placeholders.json")
POLL_SECONDS = 3
MAX_INPUT_BYTES = 200 * 1024 * 1024

routes = PromptServer.instance.routes
JOBS: dict[str, dict] = {}


# ---------------------------------------------------------------- config ---
def load_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            cfg = {}
    endpoint = os.environ.get("MODAL_COMFY_ENDPOINT") or cfg.get("endpoint", "")
    token = os.environ.get("MODAL_COMFY_TOKEN") or cfg.get("token", "")
    return {"endpoint": endpoint.rstrip("/"), "token": token}


def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['token']}"}


def _send(event: str, data: dict):
    PromptServer.instance.send_sync(f"modal_bridge.{event}", data)


# ---------------------------------------------------------------- inputs ---
def collect_inputs(prompt: dict) -> dict[str, str]:
    """Find files in the local input/ folder referenced by the workflow
    (LoadImage, LoadVideo, LoadAudio, ...) and base64 them for upload."""
    input_dir = os.path.abspath(folder_paths.get_input_directory())
    found: dict[str, str] = {}
    total = 0
    for node in prompt.values():
        for value in (node.get("inputs") or {}).values():
            if not isinstance(value, str) or not value or len(value) > 512:
                continue
            name = value
            for suffix in (" [input]", " [output]", " [temp]"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
            path = os.path.abspath(os.path.join(input_dir, name))
            if not path.startswith(input_dir + os.sep) or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, input_dir).replace(os.sep, "/")
            if rel in found:
                continue
            size = os.path.getsize(path)
            total += size
            if total > MAX_INPUT_BYTES:
                raise ValueError("Input files exceed 200MB - make them smaller first")
            with open(path, "rb") as fh:
                found[rel] = base64.b64encode(fh.read()).decode()
    return found


# --------------------------------------------------------------- outputs ---
def save_outputs(files: list[dict]) -> list[dict]:
    out_root = folder_paths.get_output_directory()
    saved = []
    for f in files:
        sub = os.path.join("modal", f.get("subfolder") or "")
        if f.get("type") == "temp":
            sub = os.path.join("modal", "preview")
        dest_dir = os.path.abspath(os.path.join(out_root, sub))
        if not dest_dir.startswith(os.path.abspath(out_root)):
            continue
        os.makedirs(dest_dir, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(f["filename"]))
        name, n = f"{base}{ext}", 1
        while os.path.exists(os.path.join(dest_dir, name)):
            name = f"{base}_{n}{ext}"
            n += 1
        with open(os.path.join(dest_dir, name), "wb") as fh:
            fh.write(base64.b64decode(f["data"]))
        saved.append({
            "filename": name,
            "subfolder": os.path.relpath(dest_dir, out_root).replace(os.sep, "/"),
            "type": "output",
            "node_id": f.get("node_id"),
            "kind": f.get("kind"),
        })
    return saved


# ------------------------------------------------------------------- job ---
async def _run_job(job_id: str, prompt: dict, cfg: dict):
    job = JOBS[job_id]
    started = time.time()
    try:
        inputs = await asyncio.to_thread(collect_inputs, prompt)
        timeout = aiohttp.ClientTimeout(total=None, sock_read=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            _send("status", {"job_id": job_id, "state": "submitting",
                             "message": f"Uploading workflow ({len(inputs)} input file(s))..."})
            async with session.post(f"{cfg['endpoint']}/submit", headers=_headers(cfg),
                                    json={"prompt": prompt, "inputs": inputs}) as r:
                if r.status != 200:
                    raise RuntimeError(f"Submit failed ({r.status}): {await r.text()}")
                call_id = (await r.json())["call_id"]
            job["call_id"] = call_id

            while True:
                if job.get("cancel"):
                    async with session.post(f"{cfg['endpoint']}/cancel/{call_id}", headers=_headers(cfg)):
                        pass
                    raise RuntimeError("Cancelled")
                elapsed = int(time.time() - started)
                _send("status", {"job_id": job_id, "state": "running",
                                 "message": f"Running on Modal GPU... {elapsed}s "
                                            "(first run includes GPU cold start)"})
                async with session.get(f"{cfg['endpoint']}/result/{call_id}", headers=_headers(cfg)) as r:
                    if r.status != 200:
                        raise RuntimeError(f"Result check failed ({r.status}): {await r.text()}")
                    res = await r.json()
                if res["status"] == "running":
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                if res["status"] == "failed":
                    detail = res.get("detail")
                    msg = res.get("error", "failed")
                    if detail:
                        msg += "\n" + json.dumps(detail, indent=1)[:3000]
                    raise RuntimeError(msg)
                break

        saved = await asyncio.to_thread(save_outputs, res.get("files", []))
        job.update(state="done", files=saved)
        _send("done", {"job_id": job_id, "files": saved, "gpu": res.get("gpu"),
                       "gpu_seconds": res.get("elapsed"),
                       "total_seconds": round(time.time() - started, 1)})
    except Exception as exc:  # report every failure to the UI
        job.update(state="error", error=str(exc))
        _send("error", {"job_id": job_id, "error": str(exc)})


# ---------------------------------------------------------------- routes ---
@routes.get("/modal_bridge/config")
async def get_config(request):
    cfg = load_config()
    return web.json_response({"endpoint": cfg["endpoint"], "has_token": bool(cfg["token"])})


@routes.post("/modal_bridge/config")
async def set_config(request):
    body = await request.json()
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            cfg = {}
    if body.get("endpoint"):
        cfg["endpoint"] = body["endpoint"].strip().rstrip("/")
    if body.get("token"):
        cfg["token"] = body["token"].strip()
    with open(CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)
    return web.json_response({"ok": True})


@routes.get("/modal_bridge/health")
async def health(request):
    cfg = load_config()
    if not cfg["endpoint"]:
        return web.json_response({"ok": False, "error": "No endpoint configured"})
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(f"{cfg['endpoint']}/health") as r:
                return web.json_response({"ok": r.status == 200, **(await r.json())})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)})


@routes.post("/modal_bridge/run")
async def run(request):
    cfg = load_config()
    if not cfg["endpoint"] or not cfg["token"]:
        return web.json_response({"error": "Modal endpoint/token not set. Click the gear button."}, status=400)
    body = await request.json()
    prompt = body.get("prompt")
    if not isinstance(prompt, dict) or not prompt:
        return web.json_response({"error": "Empty workflow"}, status=400)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"state": "queued"}
    asyncio.create_task(_run_job(job_id, prompt, cfg))
    return web.json_response({"job_id": job_id})


@routes.post("/modal_bridge/cancel/{job_id}")
async def cancel(request):
    job = JOBS.get(request.match_info["job_id"])
    if job:
        job["cancel"] = True
    return web.json_response({"ok": bool(job)})


@routes.post("/modal_bridge/sync_models")
async def sync_models(request):
    """Create empty placeholder files locally for every model in the Modal volume,
    so the model dropdowns in Comfy Desktop show them. Real local files are never touched."""
    cfg = load_config()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.get(f"{cfg['endpoint']}/models", headers=_headers(cfg)) as r:
                if r.status != 200:
                    raise RuntimeError(f"{r.status}: {await r.text()}")
                remote = await r.json()
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)

    manifest = []
    if os.path.exists(PLACEHOLDER_MANIFEST):
        with open(PLACEHOLDER_MANIFEST) as fh:
            manifest = json.load(fh)
    created = 0
    for folder, names in remote.items():
        try:
            paths = folder_paths.get_folder_paths(folder)
        except Exception:
            continue
        if not paths:
            continue
        for name in names:
            if any(os.path.exists(os.path.join(p, name)) for p in paths):
                continue
            dest = os.path.abspath(os.path.join(paths[0], name))
            if not dest.startswith(os.path.abspath(paths[0])):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "wb").close()
            manifest.append(dest)
            created += 1
    with open(PLACEHOLDER_MANIFEST, "w") as fh:
        json.dump(sorted(set(manifest)), fh, indent=1)
    try:
        folder_paths.filename_list_cache.clear()
    except Exception:
        pass
    total = sum(len(v) for v in remote.values())
    return web.json_response({"created": created, "remote_total": total, "folders": list(remote)})


@routes.post("/modal_bridge/remove_placeholders")
async def remove_placeholders(request):
    removed = 0
    if os.path.exists(PLACEHOLDER_MANIFEST):
        with open(PLACEHOLDER_MANIFEST) as fh:
            for p in json.load(fh):
                if os.path.isfile(p) and os.path.getsize(p) == 0:
                    os.remove(p)
                    removed += 1
        os.remove(PLACEHOLDER_MANIFEST)
    return web.json_response({"removed": removed})


@routes.post("/modal_bridge/download")
async def download(request):
    cfg = load_config()
    body = await request.json()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.post(f"{cfg['endpoint']}/download", headers=_headers(cfg), json=body) as r:
            return web.json_response(await r.json(), status=r.status)


print("[modal-bridge] loaded - look for the 'Run on Modal' button")
