# ComfyUI Hybrid: Laptop (Local) + Modal (Cloud GPU)

Edit workflow kat laptop (Comfy Desktop) → tekan **☁ Run on Modal** → GPU cloud generate → hasil (gambar/video) masuk balik dalam laptop, folder `output/modal/`.

Laptop tak perlu GPU dan tak perlu simpan model. Semua model duduk dalam **Modal Volume** (storage cloud).

```
 Laptop (Comfy Desktop)                      Modal (cloud)
 ┌──────────────────────────┐   workflow   ┌──────────────────────────────┐
 │ edit workflow            │ ───────────▶ │ api  (FastAPI, CPU, murah)   │
 │ [☁ Run on Modal] button  │              │   └─▶ ComfyWorker (GPU L40S) │
 │ output/modal/*.png|mp4   │ ◀─────────── │        ComfyUI + custom nodes│
 └──────────────────────────┘   results    │ Volume "comfy-models" (/models)│
                                           └──────────────────────────────┘
```

---

## Step 1: Install Comfy Desktop (laptop)

Download dari https://www.comfy.org/download dan install. Bila dia tanya GPU, pilih **CPU** pun okay (kita tak generate kat laptop).

## Step 2: Buat akaun Modal + install CLI

1. Daftar kat https://modal.com (kad virtual mungkin ditolak, guna kad debit/kredit biasa).
2. Kat laptop, buka Terminal / PowerShell:
   ```bash
   pip install modal
   modal setup          # Windows: python -m modal setup   (browser akan buka untuk login)
   ```

## Step 3: Deploy GPU worker

```bash
cd skills/comfyui-modal-hybrid/modal_worker

# 1) Buat token rahsia (password antara laptop ↔ Modal). Tukar string ni!
modal secret create comfy-bridge BRIDGE_TOKEN=tukar-ni-jadi-string-panjang-rawak
#    (optional) tambah HF_TOKEN=hf_xxx  CIVITAI_TOKEN=xxx  untuk model yang perlu login

# 2) Edit custom_nodes.txt: senaraikan custom nodes yang korang guna

# 3) Deploy
modal deploy comfy_modal.py
```

Lepas deploy, dia akan print URL macam ni. **Copy URL ni:**
```
https://<username>--comfy-hybrid-api.modal.run
```

Nak tukar GPU? (default **L40S = 48GB VRAM**)
```bash
COMFY_GPU=A100-80GB modal deploy comfy_modal.py     # Mac/Linux
$env:COMFY_GPU="H100"; modal deploy comfy_modal.py   # Windows PowerShell
```
Pilihan: `T4` (16GB), `L4` (24GB), `A10G` (24GB), `L40S` (48GB), `A100-40GB`, `A100-80GB`, `H100` (80GB).

## Step 4: Download model ke cloud (bukan ke laptop)

```bash
cp models.example.txt models.txt    # edit senarai model
modal run comfy_modal.py::download_models --file models.txt
modal run comfy_modal.py::list_models        # tengok apa yang dah ada
```
Format `models.txt`: `<folder> <url> [nama_fail]`, contoh:
```
checkpoints    https://huggingface.co/.../resolve/main/model.safetensors
loras          https://civitai.com/api/download/models/123456
diffusion_models hf://Comfy-Org/some-repo/path/model.safetensors
```

## Step 5: Install bridge node dalam Comfy Desktop

Copy folder **`comfyui_modal_bridge`** ke dalam folder `custom_nodes` Comfy Desktop:

- Windows: `Documents\ComfyUI\custom_nodes\`  (atau folder yang korang pilih masa install)
- Mac: `~/Documents/ComfyUI/custom_nodes/`

Restart Comfy Desktop. Akan nampak panel kecil kat bawah kanan:

```
[☁ Run on Modal] [✕]  Ready  [⟳] [⚙]
```

1. Klik **⚙**, paste URL dari Step 3 dan `BRIDGE_TOKEN`.
2. Klik **⟳** (Sync): nama model dari cloud akan muncul dalam dropdown (contoh: Load Checkpoint). Tekan `R` untuk refresh.
3. Bina workflow macam biasa → klik **☁ Run on Modal** (atau `Ctrl+Shift+Enter`).

Hasil keluar dalam popup + disimpan dalam `ComfyUI/output/modal/`.

> ⚠️ **Jangan tekan "Queue/Run" biasa** untuk workflow yang guna model cloud. Fail model kat laptop hanya *placeholder* 0-byte (untuk dropdown je). Selalu guna **Run on Modal**.
> Nak buang placeholder: menu **Modal** atau `POST /modal_bridge/remove_placeholders`.

---

## Kos (anggaran, semak https://modal.com/pricing)

- GPU hanya dicaj **masa sedang run** + ~2 minit idle (`COMFY_SCALEDOWN=120`) sebelum auto shut-off.
- Run pertama lambat sikit (cold start ~30-90s untuk load ComfyUI + model). Run seterusnya dalam 2 minit tu laju.
- Plan Starter Modal ada kredit percuma bulanan. Bila kredit tu habis, korang mula kena caj, jadi set **spending limit** kat Modal dashboard.

## Troubleshooting

| Masalah | Penyelesaian |
|---|---|
| `Bad or missing bridge token` | Token kat ⚙ tak sama dengan `BRIDGE_TOKEN` dalam `modal secret` |
| `Workflow rejected… value not in list` | Model tak ada dalam cloud. Download (Step 4), lepas tu ⟳ Sync |
| `node type not found` | Custom node tak ada kat cloud. Tambah dalam `custom_nodes.txt`, `modal deploy` semula |
| `CUDA out of memory` | Tukar GPU besar (`COMFY_GPU=A100-80GB`) atau kecilkan resolution/frames |
| Video panjang timeout | `COMFY_JOB_TIMEOUT=7200 modal deploy comfy_modal.py` |
| Nak tengok log | `modal app logs comfy-hybrid` atau dashboard modal.com |
