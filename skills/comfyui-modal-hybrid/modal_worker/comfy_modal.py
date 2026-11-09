"""
ComfyUI Hybrid Worker on Modal (serverless GPU).

Local laptop runs Comfy Desktop for editing only. When you press
"Run on Modal" in the UI, the bridge node sends the API-format workflow here,
it runs on a cloud GPU, and the output files are sent back to the laptop.

Deploy:
    modal secret create comfy-bridge BRIDGE_TOKEN=<random-long-string> [HF_TOKEN=...] [CIVITAI_TOKEN=...]
    modal deploy comfy_modal.py

Download models into the cloud volume (no local disk used):
    modal run comfy_modal.py::download_models --file models.txt
    modal run comfy_modal.py::list_models

Change the GPU with an env var at deploy time, e.g.:
    COMFY_GPU=A100-80GB modal deploy comfy_modal.py
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import time
import uuid

import modal

# ---------------------------------------------------------------- config ---
APP_NAME = "comfy-hybrid"
GPU = os.environ.get("COMFY_GPU", "L40S")  # L40S = 48GB VRAM
SCALEDOWN_SECONDS = int(os.environ.get("COMFY_SCALEDOWN", "120"))  # idle time before GPU shuts off
JOB_TIMEOUT_SECONDS = int(os.environ.get("COMFY_JOB_TIMEOUT", str(60 * 60)))  # max per job

HERE = pathlib.Path(__file__).parent
COMFY_DIR = "/root/comfy/ComfyUI"
MODELS_DIR = "/models"
COMFY_PORT = 8188
COMFY_URL = f"http://127.0.0.1:{COMFY_PORT}"

MODEL_FOLDERS = [
    "checkpoints", "loras", "vae", "text_encoders", "clip", "clip_vision",
    "diffusion_models", "unet", "controlnet", "upscale_models", "embeddings",
    "ipadapter", "style_models", "gligen", "hypernetworks", "photomaker",
    "audio_encoders", "model_patches", "vae_approx", "LLM",
]

app = modal.App(APP_NAME)
models_vol = modal.Volume.from_name("comfy-models", create_if_missing=True)
bridge_secret = modal.Secret.from_name("comfy-bridge", required_keys=["BRIDGE_TOKEN"])


def _custom_node_commands() -> list[str]:
    """Turn custom_nodes.txt into image build commands.

    Line formats:
        https://github.com/user/Some-Node        -> git clone + pip install -r requirements.txt
        comfyui-kjnodes                          -> comfy node install (ComfyUI registry id)
    """
    path = HERE / "custom_nodes.txt"
    if not path.exists():
        return []
    cmds = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("http://", "https://", "git@")):
            name = line.rstrip("/").split("/")[-1].removesuffix(".git")
            dest = f"{COMFY_DIR}/custom_nodes/{name}"
            cmds.append(
                f"git clone --depth 1 {line} {dest} && "
                f"(if [ -f {dest}/requirements.txt ]; then pip install -r {dest}/requirements.txt; fi)"
            )
        else:
            cmds.append(f"comfy --skip-prompt node install {line}")
    return cmds


def _extra_model_paths_yaml() -> str:
    lines = ["modal_volume:", f"  base_path: {MODELS_DIR}", "  is_default: true"]
    lines += [f"  {f}: {f}/" for f in MODEL_FOLDERS]
    return "\n".join(lines) + "\n"


comfy_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "ffmpeg", "libgl1", "libglib2.0-0", "wget")
    .pip_install("comfy-cli", "requests", "huggingface_hub[hf_transfer]")
    .run_commands("comfy --skip-prompt install --fast-deps --nvidia")
    .run_commands(  # base64 keeps the multi-line YAML safe inside a Dockerfile RUN
        f"echo {base64.b64encode(_extra_model_paths_yaml().encode()).decode()} | base64 -d "
        f"> {COMFY_DIR}/extra_model_paths.yaml"
    )
)
_node_cmds = _custom_node_commands()
if _node_cmds:
    comfy_image = comfy_image.run_commands(*_node_cmds)
comfy_image = comfy_image.env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})

light_image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "fastapi[standard]", "requests", "huggingface_hub[hf_transfer]"
).env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})


# ------------------------------------------------------- output handling ---
def collect_output_files(history_entry: dict, comfy_dir: str = COMFY_DIR) -> list[dict]:
    """Read every file a finished prompt produced and return them base64-encoded.

    ComfyUI lists results per node under keys like "images", "gifs", "videos",
    "audio" - each item is {"filename", "subfolder", "type"}.
    """
    files, seen = [], set()
    for node_id, node_out in (history_entry.get("outputs") or {}).items():
        for key, items in node_out.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or "filename" not in item:
                    continue
                kind = item.get("type", "output")
                if kind not in ("output", "temp"):
                    continue
                path = os.path.join(comfy_dir, kind, item.get("subfolder", ""), item["filename"])
                if path in seen or not os.path.isfile(path):
                    continue
                seen.add(path)
                with open(path, "rb") as fh:
                    data = base64.b64encode(fh.read()).decode()
                files.append({
                    "node_id": node_id,
                    "kind": key,
                    "filename": item["filename"],
                    "subfolder": item.get("subfolder", ""),
                    "type": kind,
                    "data": data,
                })
    return files


def history_error(history_entry: dict) -> str | None:
    status = history_entry.get("status") or {}
    if status.get("status_str") != "error":
        return None
    for name, payload in status.get("messages", []):
        if name == "execution_error":
            return (
                f"{payload.get('node_type', '?')} (node {payload.get('node_id', '?')}): "
                f"{payload.get('exception_message', '').strip()}"
            )
    return "ComfyUI reported an execution error"


# ------------------------------------------------------------ GPU worker ---
@app.cls(
    image=comfy_image,
    gpu=GPU,
    volumes={MODELS_DIR: models_vol},
    timeout=JOB_TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_SECONDS,
    max_containers=1,
)
class ComfyWorker:
    @modal.enter()
    def start_comfy(self):
        import requests

        subprocess.run(
            f"comfy launch --background -- --listen 127.0.0.1 --port {COMFY_PORT}",
            shell=True, check=True,
        )
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                if requests.get(f"{COMFY_URL}/system_stats", timeout=5).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(1)
        raise RuntimeError("ComfyUI did not start within 5 minutes")

    @modal.method()
    def run(self, prompt: dict, inputs: dict | None = None) -> dict:
        import requests

        # Pick up models downloaded since this container started.
        try:
            models_vol.reload()
        except Exception as exc:  # volume busy (models memory-mapped) - keep going
            print(f"volume reload skipped: {exc}")

        # Input files (e.g. LoadImage) sent from the laptop.
        input_dir = os.path.join(COMFY_DIR, "input")
        for rel_name, b64 in (inputs or {}).items():
            dest = os.path.normpath(os.path.join(input_dir, rel_name))
            if not dest.startswith(input_dir + os.sep):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(base64.b64decode(b64))

        started = time.time()
        resp = requests.post(
            f"{COMFY_URL}/prompt",
            json={"prompt": prompt, "client_id": str(uuid.uuid4())},
            timeout=60,
        )
        if not resp.ok:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            return {"error": "Workflow rejected by ComfyUI", "detail": detail}
        prompt_id = resp.json()["prompt_id"]

        while True:
            hist = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=30).json()
            entry = hist.get(prompt_id)
            if entry and (entry.get("status") or {}).get("completed") is not None:
                if entry["status"].get("completed") or entry["status"].get("status_str") == "error":
                    break
            time.sleep(1)

        err = history_error(entry)
        if err:
            return {"error": err, "prompt_id": prompt_id}
        files = collect_output_files(entry)
        return {
            "prompt_id": prompt_id,
            "elapsed": round(time.time() - started, 1),
            "gpu": GPU,
            "files": files,
        }


# ------------------------------------------------------- model download ---
def _download_one(url: str, folder: str, filename: str | None = None) -> str:
    """Download a model straight into the cloud volume. Supports:
    - https://huggingface.co/<repo>/resolve/<rev>/<path>   (uses HF_TOKEN if set)
    - hf://<user>/<repo>/<path/inside/repo>
    - any direct URL (civitai: CIVITAI_TOKEN appended automatically)
    """
    import shutil
    import urllib.parse

    import requests

    if folder.strip("/") not in MODEL_FOLDERS and not folder.startswith("custom/"):
        raise ValueError(f"Unknown model folder '{folder}'. Use one of: {', '.join(MODEL_FOLDERS)}")
    dest_dir = os.path.join(MODELS_DIR, folder.strip("/"))
    os.makedirs(dest_dir, exist_ok=True)

    hf_repo = hf_path = None
    revision = "main"
    if url.startswith("hf://"):
        parts = url[5:].split("/")
        hf_repo, hf_path = "/".join(parts[:2]), "/".join(parts[2:])
    elif "huggingface.co/" in url and "/resolve/" in url:
        tail = url.split("huggingface.co/", 1)[1].split("?", 1)[0]
        repo_part, rest = tail.split("/resolve/", 1)
        revision, hf_path = rest.split("/", 1)
        hf_repo = repo_part

    if hf_repo:
        from huggingface_hub import hf_hub_download

        name = filename or os.path.basename(hf_path)
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            return f"skip (exists): {folder}/{name}"
        tmp = hf_hub_download(
            repo_id=hf_repo, filename=hf_path, revision=revision,
            token=os.environ.get("HF_TOKEN") or None, cache_dir="/tmp/hf",
        )
        shutil.move(os.path.realpath(tmp), dest)
        return f"ok: {folder}/{name}"

    headers = {}
    if "civitai.com" in url and os.environ.get("CIVITAI_TOKEN"):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={os.environ['CIVITAI_TOKEN']}"
    with requests.get(url, stream=True, timeout=60, headers=headers, allow_redirects=True) as r:
        r.raise_for_status()
        name = filename
        if not name:
            cd = r.headers.get("content-disposition", "")
            if "filename=" in cd:
                name = cd.split("filename=", 1)[1].strip('"; ')
            else:
                name = os.path.basename(urllib.parse.urlparse(r.url).path)
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            return f"skip (exists): {folder}/{name}"
        part = dest + ".part"
        with open(part, "wb") as fh:
            for chunk in r.iter_content(chunk_size=16 * 1024 * 1024):
                fh.write(chunk)
        os.replace(part, dest)
    return f"ok: {folder}/{name}"


@app.function(image=light_image, volumes={MODELS_DIR: models_vol}, secrets=[bridge_secret], timeout=3 * 60 * 60)
def download_model(url: str, folder: str, filename: str | None = None) -> str:
    try:
        msg = _download_one(url, folder, filename)
    except Exception as exc:
        msg = f"FAILED {url}: {exc}"
    models_vol.commit()
    print(msg)
    return msg


def _scan_models() -> dict[str, list[str]]:
    result = {}
    for folder in MODEL_FOLDERS:
        root = os.path.join(MODELS_DIR, folder)
        if not os.path.isdir(root):
            continue
        names = []
        for dirpath, _, filenames in os.walk(root):
            for f in filenames:
                if f.endswith(".part"):
                    continue
                names.append(os.path.relpath(os.path.join(dirpath, f), root))
        if names:
            result[folder] = sorted(names)
    return result


@app.function(image=light_image, volumes={MODELS_DIR: models_vol})
def scan_models() -> dict[str, list[str]]:
    models_vol.reload()
    return _scan_models()


@app.local_entrypoint()
def download_models(file: str = "models.txt"):
    """Bulk download. Each line: <folder> <url> [filename]"""
    jobs = []
    for raw in pathlib.Path(file).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        jobs.append((parts[1], parts[0], parts[2] if len(parts) > 2 else None))
    print(f"Downloading {len(jobs)} model(s) into Modal volume 'comfy-models'...")
    for msg in download_model.starmap(jobs):
        print(" ", msg)


@app.local_entrypoint()
def list_models():
    for folder, names in scan_models.remote().items():
        print(f"[{folder}]")
        for n in names:
            print("   ", n)


# ----------------------------------------------------------- web bridge ---
@app.function(image=light_image, secrets=[bridge_secret], volumes={MODELS_DIR: models_vol})
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def api():
    import secrets as pysecrets

    from fastapi import Depends, FastAPI, Header, HTTPException
    from pydantic import BaseModel

    web = FastAPI(title="ComfyUI Modal bridge")
    token = os.environ["BRIDGE_TOKEN"]

    def auth(authorization: str = Header(default="")):
        supplied = authorization.removeprefix("Bearer ").strip()
        if not pysecrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Bad or missing bridge token")

    class Job(BaseModel):
        prompt: dict
        inputs: dict[str, str] = {}

    class Download(BaseModel):
        url: str
        folder: str
        filename: str | None = None

    @web.get("/health")
    def health():
        return {"ok": True, "app": APP_NAME, "gpu": GPU}

    @web.post("/submit", dependencies=[Depends(auth)])
    def submit(job: Job):
        call = ComfyWorker().run.spawn(job.prompt, job.inputs)
        return {"call_id": call.object_id}

    @web.get("/result/{call_id}", dependencies=[Depends(auth)])
    def result(call_id: str):
        fc = modal.FunctionCall.from_id(call_id)
        try:
            out = fc.get(timeout=0)
        except TimeoutError:
            return {"status": "running"}
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        if out.get("error"):
            return {"status": "failed", **out}
        return {"status": "done", **out}

    @web.post("/cancel/{call_id}", dependencies=[Depends(auth)])
    def cancel(call_id: str):
        modal.FunctionCall.from_id(call_id).cancel()
        return {"cancelled": call_id}

    @web.get("/models", dependencies=[Depends(auth)])
    def models():
        try:
            models_vol.reload()
        except Exception:
            pass
        return _scan_models()

    @web.post("/download", dependencies=[Depends(auth)])
    def download(req: Download):
        call = download_model.spawn(req.url, req.folder, req.filename)
        return {"call_id": call.object_id}

    return web
