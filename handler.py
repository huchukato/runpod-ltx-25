"""RunPod serverless handler for the LTX 2.5 ComfyUI stack.

Boots ComfyUI once per worker, then each job picks a workflow template by name,
patches its widgets (prompt / vl_preset / duration / size), uploads any input
media, queues the prompt, and returns the produced media as base64.

Job input:
  {
    "workflow": "LTX25-Loop-ABCBA.json",           # file in /opt/workflows
    "prompt":   "...",                             # enhancer text (wildcards ok)
    "seconds":  5,                                 # 5 | 10 | 20
    "images":   ["https://... or base64"],         # optional, fills LoadImage nodes in id order
    "vl_preset": "LTX › NSFW Loop",                # optional QwenVL preset override
    "enhance":  false,                             # optional, skips the QwenVL (passthrough)
    "image_size": "1344x768",                      # optional explicit size
    "upscale": true, "rife": true,                 # post chain toggles
    "upscale_model": "2xLexicaRRDBNet",            # optional overrides
    "upscale_target": "2K"
  }

The I2VA-T2VA template serves both modes: images present → QwenVL I2V path,
no images → the same QwenVL text-only with the image re-injections dropped
(T2V). The "Switch to Text to Video?" boolean carries the mode on canvas.
"""

import asyncio
import base64
import glob
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.parse
import uuid
from pathlib import Path

import runpod

COMFY_DIR = os.environ.get("COMFYUI_DIR", "/workspace/runpod-slim/ComfyUI")
COMFY_URL = "http://127.0.0.1:8188"
WORKFLOWS_DIR = Path(os.environ.get("WORKFLOWS_DIR", "/opt/workflows"))
JOB_TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "1800"))

# Workflows boot-synced from the ForgeHub repo override the baked copies, so a
# workflow edit lands at the next cold boot without rebuilding the image.
FH_REPO = os.environ.get("FORGEHUB_REPO", "huchukato/ForgeHub")
FH_BRANCH = os.environ.get("FORGEHUB_BRANCH", "main")
FH_ENDPOINT = os.environ.get("FORGEHUB_ENDPOINT_NAME", "LTX 2.5")
WF_SYNC_DIR = Path(os.environ.get("WF_SYNC_DIR", "/tmp/forgehub-workflows"))
WF_SYNC_MANIFEST = WF_SYNC_DIR / ".manifest"

# Output delivery: base64 inline under these limits, S3 offload above them.
# RunPod's job-done callback drops results over ~20MB, so keep headroom.
INLINE_MAX = int(os.environ.get("OUTPUT_INLINE_MAX_MB", "10")) * 1024 * 1024
INLINE_BUDGET = int(os.environ.get("OUTPUT_INLINE_BUDGET_MB", "15")) * 1024 * 1024
S3_PREFIX = os.environ.get("S3_OUTPUT_PREFIX", "outputs")


def _http(path, payload=None, timeout=30):
    url = COMFY_URL + path
    try:
        if payload is None:
            return urllib.request.urlopen(url, timeout=timeout).read()
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"ComfyUI {path} → HTTP {e.code}: {body}") from e


_BOOT_DONE = threading.Event()
_BOOT_ERR = None


def _boot_comfy():
    # Extra flags come from COMFYUI_EXTRA_ARGS (space-separated). Keep the
    # default empty: --fast/--use-sage-attention/--async-offload corrupt the
    # int8-convrot stacks (flat garbage output).
    cmd = [
        "python3", "main.py",
        "--listen", "127.0.0.1", "--port", "8188",
        "--disable-auto-launch",
    ] + os.environ.get("COMFYUI_EXTRA_ARGS", "").split()
    comfy_log = open("/workspace/comfy.log", "ab", buffering=0)
    proc = subprocess.Popen(cmd, cwd=COMFY_DIR, stdout=comfy_log, stderr=subprocess.STDOUT)
    deadline = time.time() + 600
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("ComfyUI exited during boot")
        try:
            _http("/system_stats", timeout=5)
            return proc
        except Exception:
            time.sleep(2)
    raise RuntimeError("ComfyUI did not become ready in 600s")


def _sync_workflows():
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{FH_REPO}/git/trees/{FH_BRANCH}?recursive=1",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "forgehub-worker"})
        tree = json.loads(urllib.request.urlopen(req, timeout=60).read())
        raw = lambda p: f"https://raw.githubusercontent.com/{FH_REPO}/{FH_BRANCH}/{p}"
        wanted = []
        for entry in sorted(tree.get("tree", []), key=lambda e: e["path"]):
            mp = entry["path"]
            if not (mp.startswith("workflows/") and mp.endswith(".meta.json")):
                continue
            meta = json.loads(urllib.request.urlopen(raw(mp), timeout=60).read())
            if meta.get("endpoint_name") == FH_ENDPOINT:
                wanted.append(mp[:-len(".meta.json")] + ".json")
        WF_SYNC_DIR.mkdir(parents=True, exist_ok=True)
        names = set()
        for wp in wanted:
            name = Path(wp).name
            tmp = WF_SYNC_DIR / f"{name}.tmp"
            tmp.write_bytes(urllib.request.urlopen(raw(wp), timeout=120).read())
            os.replace(tmp, WF_SYNC_DIR / name)
            names.add(name)
        if WF_SYNC_MANIFEST.is_file():
            for stale in set(WF_SYNC_MANIFEST.read_text().split()) - names:
                (WF_SYNC_DIR / stale).unlink(missing_ok=True)
        WF_SYNC_MANIFEST.write_text("\n".join(sorted(names)) + "\n")
        print(f"[handler] workflow sync: {sorted(names)}", flush=True)
    except Exception as exc:
        print(f"[handler] workflow sync failed: {exc} — using baked workflows", flush=True)


def _boot_in_bg():
    # ComfyUI boots on a daemon thread so the runpod handler is live (and the
    # `health` action answers) even while ComfyUI is still starting — or on
    # machines where it can't start at all (e.g. Hub test pods without the
    # network volume). Real jobs wait on _BOOT_DONE via _ensure_comfy().
    global _BOOT_ERR
    try:
        _sync_workflows()
        _boot_comfy()
    except Exception as exc:
        _BOOT_ERR = exc
        print(f"[handler] ComfyUI boot failed: {exc}", flush=True)
    finally:
        _BOOT_DONE.set()


async def _ensure_comfy():
    await asyncio.to_thread(_BOOT_DONE.wait)
    if _BOOT_ERR is not None:
        raise RuntimeError(f"ComfyUI failed to boot: {_BOOT_ERR} (see /workspace/comfy.log)")


def _media_bytes(value):
    if isinstance(value, str) and value.startswith("http"):
        return urllib.request.urlopen(value, timeout=120).read()
    return base64.b64decode(value)


def _upload(filename, data):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        COMFY_URL + "/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
    return resp.get("name", filename)


def _load_workflow(name):
    """Workflow templates must be saved in ComfyUI *API format* (Export API),
    where every node is keyed by id and inputs carry widget names — the UI
    format's positional widgets_values can't be patched reliably."""
    dirs = [WF_SYNC_DIR, WORKFLOWS_DIR] if WF_SYNC_MANIFEST.is_file() else [WORKFLOWS_DIR]
    for base in dirs:
        path = base / name
        if not path.is_file():
            path = base / f"{name}.json"
        if path.is_file():
            break
    else:
        raise RuntimeError(f"workflow template not found: {name} (available: {sorted(p.name for d in dirs for p in d.glob('*.json'))})")
    wf = json.loads(path.read_text())
    if "nodes" in wf:
        raise RuntimeError(f"{name} is a UI-format graph; re-export it with 'Export API' so widgets are named")
    return wf


def _patch(prompt, job):
    base = re.sub(r"[^\w.-]+", "-", str(job.get("job_name") or f"job_{uuid.uuid4().hex[:8]}")).strip("-")
    # I2VA-T2VA: the "Switch to Text to Video?" boolean chain bypasses the two
    # LTXVImgToVideoInplace re-injections. With images the graph runs as baked
    # (I2V); without, the image chain (loader → ImageInfo → resize → preprocess
    # → both re-injections) is dead weight with required inputs ComfyUI would
    # reject — drop it and rewire each re-injection's consumers to its latent
    # source, so T2V runs the QwenVL text-only on clean latents.
    bypass_srcs = set()
    for node in prompt.values():
        if node.get("class_type") == "LTXVImgToVideoInplace":
            bp = node.get("inputs", {}).get("bypass")
            if isinstance(bp, list) and bp:
                bypass_srcs.add(str(bp[0]))
    job_images = job.get("images") or []
    has_images = bool(job_images)
    if has_images:
        images = [_upload(f"{base}_{i}.png", _media_bytes(v)) for i, v in enumerate(job_images)]
    elif bypass_srcs:
        images = []
    else:
        images = []
    video_name = None
    if job.get("video"):
        video_name = _upload(f"{base}.mp4", _media_bytes(job["video"]))

    for nid, node in prompt.items():
        if nid not in bypass_srcs or node.get("class_type") != "PrimitiveBoolean":
            continue
        inp = node.setdefault("inputs", {})
        if isinstance(inp.get("value"), bool):
            inp["value"] = not has_images

    # Fill the loaders in TEMPORAL order: each PixaromaLoadImageMini chains
    # resize → LTXVPreprocess → LTXVAddGuide, and the guide's frame_idx is the
    # slot's position in the video (−1 sorts last). With exactly one image per
    # slot it fills 1:1; with the pose-loop shape (n slots, (n+1)/2 stills) it
    # mirrors the sequence — A,B,C into 5 slots → A,B,C,B,A; anything else
    # cycles, which also turns a single still into a closed A→A loop.
    def _slot_order():
        by_id = {str(k): v for k, v in prompt.items()}

        def _follow(src_id, want):
            for nid, n in by_id.items():
                if n.get("class_type") != want:
                    continue
                for v in n.get("inputs", {}).values():
                    if isinstance(v, list) and v and str(v[0]) == src_id:
                        return nid
            return None

        def _frame_idx(loader_id):
            nid = _follow(loader_id, "ResizeImageMaskNode") or _follow(loader_id, "LTXVPreprocess")
            if nid is None:
                return float("inf")
            nid = _follow(nid, "LTXVPreprocess") or nid
            nid = _follow(nid, "LTXVAddGuide")
            if nid is None:
                return float("inf")
            idx = by_id[nid].get("inputs", {}).get("frame_idx")
            return idx if isinstance(idx, int) and idx >= 0 else float("inf")

        return sorted((nid for nid, n in by_id.items() if "LoadImage" in n.get("class_type", "")), key=_frame_idx)

    slots = _slot_order()
    mapping = {}
    if len(images) == len(slots):
        mapping = dict(zip(slots, images))
    elif len(slots) == 2 * len(images) - 1 and len(images) > 1:
        seq = [images[i] if i < len(images) else images[2 * len(images) - 2 - i] for i in range(len(slots))]
        mapping = dict(zip(slots, seq))
    elif images:
        mapping = {lid: images[i % len(images)] for i, lid in enumerate(slots)}

    for nid, node in prompt.items():
        ct = node.get("class_type", "")
        inp = node.get("inputs", {})

        if "LoadImage" in ct and nid in mapping:
            inp["image"] = mapping[nid]
            if "LoadImageMiniState" in inp:
                inp["LoadImageMiniState"] = mapping[nid]
            if "LoadImagePixState" in inp:
                try:
                    state = json.loads(inp["LoadImagePixState"])
                    state["orig_name"] = mapping[nid]
                    inp["LoadImagePixState"] = json.dumps(state)
                except Exception:
                    inp.pop("LoadImagePixState", None)
        elif "LoadVideo" in ct and video_name:
            inp["video"] = video_name
        if job.get("prompt") is not None and "prompt" in inp and not isinstance(inp["prompt"], list):
            inp["prompt"] = job["prompt"]
        if job.get("prompt") is not None and "prompt_text" in inp and not isinstance(inp["prompt_text"], list):
            inp["prompt_text"] = job["prompt"]
        if job.get("vl_preset"):
            for wkey in ("preset_prompt", "enhancement_style"):
                if wkey in inp:
                    inp[wkey] = job["vl_preset"]
        if job.get("enhance") is False and "passthrough" in inp:
            inp["passthrough"] = True
        if job.get("seconds"):
            sec = int(job["seconds"])
            if "duration" in inp and not isinstance(inp["duration"], list):
                inp["duration"] = f"{sec}s"
    if job.get("seconds"):
        # Video length: the duration PrimitiveInt only — its value is ≤ 20
        # (fps 25 and the 352+ px dims stay clear of the range), so a blanket
        # PrimitiveInt rewrite cannot touch width/height/fps.
        sec = int(job["seconds"])
        for node in prompt.values():
            if node.get("class_type") != "PrimitiveInt":
                continue
            v = node.get("inputs", {}).get("value")
            if isinstance(v, int) and 0 < v <= 20:
                node["inputs"]["value"] = sec
    # Loaders left on their template value (fewer uploads than loaders, or a
    # T2V job on the I2V graph) would fail validation — drop them, then
    # iterate to a fixpoint: unlink references to the dead; image-chain nodes
    # (resize / preprocess / img-to-video) keep widget inputs so they never
    # reach zero-input and are declared dead once their image link is gone.
    # A dropped LTXVImgToVideoInplace forwards its own latent input to its
    # consumers — its bypass boolean survives as a harmless orphan.
    uploaded = set(images)
    CHAIN_NODES = ("ResizeImageMaskNode", "LTXVPreprocess", "LTXVImgToVideoInplace")
    while True:
        dead = [nid for nid, n in prompt.items()
                if ("LoadImage" in n.get("class_type", "")
                    and n.get("inputs", {}).get("image") not in uploaded)
                or ("LoadVideo" in n.get("class_type", "")
                    and n.get("inputs", {}).get("video") != video_name)
                or (n.get("class_type") in CHAIN_NODES
                    and not any(k in ("image", "input") for k in n.get("inputs", {})))
                or not n.get("inputs")]
        if not dead:
            break
        for nid in dead:
            n = prompt.pop(nid)
            src = n.get("inputs", {}).get("latent")
            for o in prompt.values():
                for k in [k for k, v in o.get("inputs", {}).items()
                          if isinstance(v, list) and v and v[0] == nid]:
                    if n.get("class_type") == "LTXVImgToVideoInplace" and isinstance(src, list):
                        o["inputs"][k] = src
                    else:
                        del o["inputs"][k]
    # Post-processing toggles: the graph chain is  save.images <- rife.frames
    # <- upscale.images <- VAEDecode.  upscale=false rewires rife.frames to the
    # decoder output; rife=false rewires save.images to rife's own source.
    upscaler = next((n for n in prompt.values() if "upscaler_trt_model" in n.get("inputs", {})), None)
    rife = next((n for n in prompt.values() if "rife_trt_model" in n.get("inputs", {})), None)
    saver = next((n for n in prompt.values()
                  if isinstance(n.get("inputs", {}).get("images"), list)
                  and n["inputs"]["images"]
                  and rife is not None
                  and n["inputs"]["images"][0] == next(k for k, v in prompt.items() if v is rife)), None) if rife else None
    if not job.get("upscale", True) and upscaler is not None and rife is not None:
        rife["inputs"]["frames"] = upscaler["inputs"]["images"]
    if not job.get("rife", True) and rife is not None and saver is not None:
        saver["inputs"]["images"] = rife["inputs"]["frames"]
        # The VHS saver bakes 50 (the RIFE'd rate) — halve it. CreateVideo takes
        # fps as a LINK from the math node (the pre-RIFE rate): leave it alone.
        if "frame_rate" in saver["inputs"] and not isinstance(saver["inputs"]["frame_rate"], list):
            saver["inputs"]["frame_rate"] = saver["inputs"]["frame_rate"] // int(rife["inputs"].get("multiplier", 2) or 2)
    # Upscaler model / target overrides — the loader node feeds "upscaler_trt_model".
    upscaler_loader = next((n for n in prompt.values()
                            if n.get("class_type", "").endswith("LoadUpscalerTensorrtModel")), None)
    if upscaler_loader is not None and job.get("upscale_model"):
        upscaler_loader["inputs"]["model"] = job["upscale_model"]
    if upscaler is not None and job.get("upscale_target"):
        upscaler["inputs"]["resize_to"] = job["upscale_target"]
    # Output size: "1344x768" rewrites the PVR tier — explicit size beats the
    # tier labels (_parse_resolution falls through to _parse_size for a "WxH"
    # value) and use_image_aspect goes off: a picked size is literal. "auto"
    # keeps the baked tier (the PVR has no image input by design, so the aspect
    # always comes from its own widgets).
    m = re.match(r"(\d+)\s*x\s*(\d+)", str(job.get("image_size") or job.get("video_size") or ""))
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        for node in prompt.values():
            if node.get("class_type") == "ComfyUI-PerfectVideoResolution":
                inp = node.setdefault("inputs", {})
                inp["resolution"] = f"{w}x{h}"
                inp["use_image_aspect"] = False
    # Per-job seed: baked seeds would repeat identical results across jobs.
    # An explicit job["seed"] pins every seed consumer to the same value.
    job_seed = job.get("seed")
    if job_seed is None:
        job_seed = random.randrange(0, 2**32)
    for node in prompt.values():
        inp = node.get("inputs", {})
        node_seed = job_seed % (2**32)
        for skey in ("seed", "noise_seed"):
            if isinstance(inp.get(skey), int) and not isinstance(inp.get(skey), bool):
                inp[skey] = node_seed
    # %date:fmt% tokens are resolved by the ComfyUI frontend only for widget
    # values — an API-format prompt reaches the nodes literally (VHS then saves
    # the raw token into the filename and /view 404s). Expand here with the
    # same field codes as Pixaroma's _expand_date_tokens.
    now = time.localtime()
    fields = {
        "d": now.tm_mday, "dd": f"{now.tm_mday:02d}",
        "M": now.tm_mon, "MM": f"{now.tm_mon:02d}",
        "h": now.tm_hour, "hh": f"{now.tm_hour:02d}",
        "H": now.tm_hour, "HH": f"{now.tm_hour:02d}",
        "m": now.tm_min, "mm": f"{now.tm_min:02d}",
        "s": now.tm_sec, "ss": f"{now.tm_sec:02d}",
        "yy": f"{now.tm_year % 100:02d}", "yyyy": now.tm_year,
    }
    for n in prompt.values():
        for k, v in n.get("inputs", {}).items():
            if isinstance(v, str) and "%date:" in v:
                n["inputs"][k] = re.sub(
                    r"%date:([^%]+)%",
                    lambda m: "".join(str(fields.get(t, t))
                                      for t in re.findall(r"dd?|MM?|hh?|HH?|mm?|ss?|yyy?y?|.", m.group(1))),
                    v)
    return prompt


def _apply_node_params(prompt, params):
    """Apply generic {node_id:widget} patches, after the built-in passes."""
    for key, value in (params or {}).items():
        node_id, _, widget = key.rpartition(":")
        node = prompt.get(node_id)
        if isinstance(node, dict) and widget:
            node.setdefault("inputs", {})[widget] = value
    return prompt


def _interrupt_prompt(prompt_id):
    """Best-effort ComfyUI abort for a cancelled job: dequeue if pending,
    interrupt if mid-flight."""
    for path, payload in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {})):
        try:
            _http(path, payload, timeout=5)
        except Exception as exc:
            print(f"[handler] {path} failed during cancel: {exc}", flush=True)


async def _queue_and_wait(prompt):
    # Async + awaited sleeps: the runpod SDK stops a job by cancelling its
    # task, so the CancelledError can only be delivered at an await — a sync
    # handler would freeze the event loop and never see the stop signal.
    client_id = uuid.uuid4().hex
    resp = json.loads(await asyncio.to_thread(
        _http, "/prompt", {"prompt": prompt, "client_id": client_id}, 60))
    prompt_id = resp["prompt_id"]
    deadline = time.time() + JOB_TIMEOUT_S
    try:
        while time.time() < deadline:
            hist = json.loads(await asyncio.to_thread(_http, f"/history/{prompt_id}", None, 30))
            entry = hist.get(prompt_id)
            if entry and entry.get("status", {}).get("status_str") == "error":
                msgs = [str(m) for m in entry.get("status", {}).get("messages", [])]
                raise RuntimeError(f"ComfyUI job failed: {' | '.join(msgs)[:800]}")
            if entry and entry.get("status", {}).get("completed"):
                return entry
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        # Even if this await gets cancelled again, the orphan thread still
        # completes and ComfyUI gets interrupted.
        await asyncio.to_thread(_interrupt_prompt, prompt_id)
        raise
    raise TimeoutError(f"prompt {prompt_id} exceeded {JOB_TIMEOUT_S}s")


def _s3_cfg(job):
    cfg = job.get("s3Config") or (job.get("input") or {}).get("s3Config")
    if isinstance(cfg, dict) and cfg.get("endpointUrl") and cfg.get("accessId"):
        return cfg
    return None


def _s3_client(cfg):
    import boto3
    from botocore.config import Config
    host = urllib.parse.urlparse(cfg["endpointUrl"]).netloc
    region = cfg.get("region") or host.split(".", 1)[0].removeprefix("s3api-") or "us-east-1"
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpointUrl"],
        aws_access_key_id=cfg["accessId"],
        aws_secret_access_key=cfg["accessSecret"],
        region_name=region,
        config=Config(s3={"addressing_style": "path"}),
    )


def _emit_file(filename, data, job, inline_used):
    """Small outputs inline as b64; large ones upload to the job's s3Config
    storage (e.g. the RunPod network volume S3 API) and return a presigned URL."""
    oversized = len(data) > INLINE_MAX or inline_used[0] + len(data) > INLINE_BUDGET
    s3c = _s3_cfg(job)
    if oversized and s3c:
        try:
            key = f"{S3_PREFIX}/{job.get('id', 'job')}/{filename}"
            client = _s3_client(s3c)
            client.put_object(Bucket=s3c["bucketName"], Key=key, Body=data)
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": s3c["bucketName"], "Key": key},
                ExpiresIn=int(os.environ.get("S3_URL_TTL_S", "3600")),
            )
            print(f"[handler] {filename} -> s3://{s3c['bucketName']}/{key} ({len(data)/1e6:.1f}MB)", flush=True)
            return {"filename": filename, "url": url}
        except Exception as e:
            print(f"[handler] s3 upload failed for {filename}: {e} — falling back to inline", flush=True)
    elif oversized:
        print(f"[handler] {filename}: {len(data)/1e6:.1f}MB over inline limit, no s3Config — result may be dropped by RunPod", flush=True)
    inline_used[0] += len(data)
    return {"filename": filename, "b64": base64.b64encode(data).decode()}


def _prune_volume_outputs(max_age_s=86400):
    """Drop stale volume output dirs — ForgeHub downloads right after the job."""
    root = Path("/runpod-volume") / S3_PREFIX
    cutoff = time.time() - max_age_s
    try:
        for d in root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def _collect_new_files(since_ts, job, inline_used):
    """Fallback: anything written under ComfyUI's output dir since the job ran."""
    out_dir = Path(COMFY_DIR) / "output"
    try:
        files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.stat().st_mtime >= since_ts)
    except Exception:
        return []
    out = []
    for p in files:
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".gif", ".wav", ".mp3", ".flac"):
            continue
        out.append(_emit_file(p.name, p.read_bytes(), job, inline_used))
    return out


def _collect_outputs(history_entry, prompt, job, inline_used):
    out, texts = [], {}
    for node_id, node_out in (history_entry.get("outputs") or {}).items():
        print(f"[handler] output node {node_id}: keys={list(node_out.keys())}", flush=True)
        for key in ("images", "gifs", "videos"):
            for item in node_out.get(key, []) or []:
                params = urllib.parse.urlencode({
                    "filename": item["filename"],
                    "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output"),
                })
                data = _http(f"/view?{params}", timeout=300)
                out.append(_emit_file(item["filename"], data, job, inline_used))
        src = (prompt or {}).get(node_id, {}).get("inputs", {}).get("source")
        if not isinstance(src, list):  # only ShowText-style nodes carry live text
            continue
        for t in node_out.get("text", []) or []:  # ShowText nodes → prompt trace
            src_ct = (prompt or {}).get(str(src[0]), {}).get("class_type", "")
            label = "Wildcards expanded" if "Wildcard" in src_ct else "Final prompt (QwenVL)"
            texts[label] = t if isinstance(t, str) else str(t)
    return out, texts


async def handler(job):
    print(f"[handler] job {job.get('id')} received", flush=True)
    inputs = job.get("input") or {}
    if inputs.get("action") == "health":
        # Hub smoke test: answers 200 without touching ComfyUI or models, so it
        # also works on an endpoint deployed without the network volume.
        return {
            "status": "ok",
            "comfyui": "failed" if _BOOT_ERR else ("ready" if _BOOT_DONE.is_set() else "booting"),
            "comfyui_error": str(_BOOT_ERR) if _BOOT_ERR else None,
            "workflows": sorted({p.name for d in (WF_SYNC_DIR, WORKFLOWS_DIR) for p in d.glob("*.json")}),
            "volume_mounted": os.path.isdir("/runpod-volume"),
        }
    # Any real workload needs ComfyUI — wait for the background boot first.
    await _ensure_comfy()
    _prune_volume_outputs()
    # _patch uploads input media (blocking, up to minutes for video) — run it
    # off-thread so a cancel signal can still be delivered during uploads.
    if isinstance(inputs.get("prompt_graph"), dict):
        # Raw API-format graph shipped inside the job (ForgeHub mode): the same
        # _patch applies — media upload, vl_preset, duration, size, toggles.
        prompt = await asyncio.to_thread(_patch, inputs["prompt_graph"], inputs)
    else:
        wf = _load_workflow(inputs.get("workflow") or "")
        prompt = await asyncio.to_thread(_patch, wf, inputs)
    prompt = _apply_node_params(prompt, inputs.get("params"))
    print(f"[handler] queuing prompt ({len(prompt)} nodes)", flush=True)
    started = time.time()
    comfy_log = Path("/workspace/comfy.log")
    log_mark = comfy_log.stat().st_size if comfy_log.is_file() else 0
    history = await _queue_and_wait(prompt)
    print(f"[handler] comfy execution took {time.time() - started:.1f}s", flush=True)
    inline_used = [0]
    outputs, texts = await asyncio.to_thread(_collect_outputs, history, prompt, job, inline_used)
    try:  # surface ComfyUI warnings/errors from this job in the worker log
        with open(comfy_log, "rb") as f:
            f.seek(log_mark)
            interesting = [l for l in f.read().decode(errors="replace").splitlines()
                           if re.search(r"warn|error|missing|unexpected|fail|dtype|clip|traceback", l, re.I)]
            for l in interesting[-40:]:
                print(f"[comfy] {l}", flush=True)
    except Exception:
        pass
    if not outputs:
        outputs = _collect_new_files(started, job, inline_used)
        print(f"[handler] fallback sweep — {len(outputs)} file(s) in output/", flush=True)
    print(f"[handler] done — {len(outputs)} output(s)", flush=True)
    return {"outputs": outputs, "texts": texts}


if __name__ == "__main__":
    threading.Thread(target=_boot_in_bg, daemon=True).start()
    runpod.serverless.start({"handler": handler})
