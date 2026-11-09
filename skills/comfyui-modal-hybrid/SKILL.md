---
name: comfyui-modal-hybrid
description: Set up and operate a hybrid ComfyUI workflow where Comfy Desktop runs on a weak local laptop (editing only) and all generation runs on Modal serverless GPUs, with models stored in a Modal Volume. Use when the user wants to run ComfyUI on a cloud GPU without a local GPU, deploy/redeploy the Modal ComfyUI worker, download models or custom nodes to Modal, install the "Run on Modal" bridge button, or debug Modal ComfyUI jobs.
---

# ComfyUI Hybrid (Local Comfy Desktop + Modal GPU)

## Pieces

| Path | Runs on | Purpose |
|---|---|---|
| `modal_worker/comfy_modal.py` | Modal | GPU class `ComfyWorker` (headless ComfyUI), `api` FastAPI bridge, `download_model`, `scan_models` |
| `modal_worker/custom_nodes.txt` | build time | Custom nodes baked into the Modal image (git URL or registry id per line) |
| `modal_worker/models.example.txt` | local | Bulk model list: `<folder> <url> [filename]` |
| `comfyui_modal_bridge/` | laptop | ComfyUI custom node: "☁ Run on Modal" button, model-list sync, result download |
| `SETUP.md` | — | End-user guide (Bahasa Melayu) |

Flow: button → `app.graphToPrompt()` → local route `/modal_bridge/run` → uploads referenced `input/` files + API prompt to `POST {endpoint}/submit` → `ComfyWorker.run.spawn()` → bridge polls `GET /result/{call_id}` → base64 outputs saved to `output/modal/` → gallery popup.

## Setup checklist (do these for the user)

1. `pip install modal && modal setup` (the user must complete the browser login themselves).
2. `modal secret create comfy-bridge BRIDGE_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")`. Optionally add `HF_TOKEN`, `CIVITAI_TOKEN`. Show the token to the user once so they can paste it into the ⚙ dialog.
3. Edit `custom_nodes.txt` to mirror the custom nodes installed in their Comfy Desktop (`ls <ComfyUI>/custom_nodes`).
4. `cd modal_worker && modal deploy comfy_modal.py`, then note the `...--comfy-hybrid-api.modal.run` URL.
5. Build `models.txt` from the user's workflows, then `modal run comfy_modal.py::download_models --file models.txt`.
6. Copy `comfyui_modal_bridge/` into the Comfy Desktop `custom_nodes/` folder. Either write `config.json` (`{"endpoint": "...", "token": "..."}`) or let the user use ⚙.
7. Verify: `curl <endpoint>/health`, then `modal run comfy_modal.py::list_models`.

## Knobs (env vars at `modal deploy` time)

- `COMFY_GPU`: `L40S` (default, 48GB), `L4`, `A10G`, `A100-40GB`, `A100-80GB`, `H100`
- `COMFY_SCALEDOWN`: idle seconds before the GPU container stops (default 120)
- `COMFY_JOB_TIMEOUT`: max seconds per job (default 3600)

## Rules

- Custom nodes must exist both locally (so the UI can build the graph) and in `custom_nodes.txt` (to execute). If they don't match, ComfyUI rejects the workflow with "node type not found".
- Local model files created by Sync are 0-byte placeholders listed in `comfyui_modal_bridge/placeholders.json`. Never run those workflows with the normal local Queue.
- `config.json` holds the token. Never commit it.
- After changing `custom_nodes.txt` or the GPU, redeploy. New models do not need a redeploy, because the worker reloads the volume on each job.
- Debug with `modal app logs comfy-hybrid`. ComfyUI validation errors come back in the `detail` field and appear in the UI toast.
