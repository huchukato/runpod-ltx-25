![LTX 2.5 — pose loops and AV generation](https://raw.githubusercontent.com/huchukato/runpod-ltx-25/main/media/banner.jpeg)

# LTX 2.5 — ComfyUI Serverless Worker

[![Runpod](https://api.runpod.io/badge/huchukato/runpod-ltx-25)](https://console.runpod.io/hub)

A production-ready [Runpod Serverless](https://docs.runpod.io/serverless/overview) worker running **LTX 2.5** (22B distilled, INT8 ConvRot) on ComfyUI — text/image/first-last-frame-to-video **with synchronized audio**, **pose-loop keyframe workflows** (A→B→C→B→A seamless loops from 2–3 still poses), TensorRT RIFE + upscaling, wildcards and an uncensored Qwen3-VL enhancer. Built for [ForgeHub](https://github.com/huchukato/ForgeHub), but the endpoint speaks plain Runpod API.

## Deploy

1. **Storage first**: create a [network volume](https://console.runpod.io/user/storage) (≥80GB) in a datacenter with 96GB Pro GPUs — e.g. `US-NE-1`.
2. Click **Deploy** → endpoint → **Edit** → set **Max workers = 1** + attach your volume. Cold start is heavy (~60GB of models on first boot, big model loads after), so one warm worker with jobs queued beats N cold workers — Runpod's deploy default is 3, lower it. Workers auto-download the models on first boot (once). No volume = every job fails on missing models.
3. Call it via the Runpod API or ForgeHub below — workflows, wildcards, job queue and outputs included; paste your API key and it detects the endpoint by itself.

[![Download ForgeHub](https://raw.githubusercontent.com/huchukato/runpod-ltx-25/main/media/forgehub-release.jpeg)](https://github.com/huchukato/ForgeHub/releases/latest)

## Workflows included

| Workflow | Description |
| --- | --- |
| `LTX25-I2VA-T2VA.json` | Text OR image → video + audio (2-stage: base pass → 2x latent upscale → refine); the worker switches I2V/T2V automatically from the presence of `images` |
| `LTX25-FL2VA.json` | First/last frame → video + audio — a closed A→A loop when both slots carry the same still |
| `LTX25-Loop-ABCBA.json` | **Pose loop**: up to 5 keyframe stills pinned at exact frames (0 / ¼ / ½ / ¾ / −1) — 3 stills A,B,C mirror into A→B→C→B→A and the loop closes by construction |

## Requirements

- **GPU**: 96GB class (`BLACKWELL_96` pool). The I2VA 2-stage loads both text encoders (Gemma-4 12B heretic INT8 + E2B BF16) plus the 22B DiT — 48GB-class cards are at the limit.
- **Network volume**: ≥80GB at `/runpod-volume`. Models auto-download from `models-manifest.txt` (~60GB). Disable with `VOLUME_AUTOPOPULATE=false`.
- **CUDA**: 13.0+ hosts.

## Model stack (auto-provisioned)

- DiT: `ltx-2.5-22b-distilled-transformer` INT8 ConvRot
- Text encoders: `gemma4-12b-uncensored-heretic` INT8 (main) + `gemma4_e2b_it_bf16` (T2V prompt generator)
- VAEs: `ltx-2.5-video-vae-conv` + `ltx-2.5-audio-vae` (synchronized audio)
- Latent upscaler: `ltx-2.5-latent-spatial-upscaler-x2` (2-stage refine)
- LoRA: `LTX_10Eros-v15` (uncensored style, strength 0.7 in the graphs)
- Enhancer LLM: Qwen3.5-9B Defiant-Fable NEO-MAX Q6_K GGUF + mmproj

## Job input

```json
{
  "input": {
    "workflow": "LTX25-Loop-ABCBA.json",
    "prompt": "Doggystyle: the man takes her from behind with a deep steady rhythm...",
    "seconds": 5,
    "images": ["<pose A>", "<pose B>", "<pose C>"],
    "vl_preset": "LTX › NSFW Loop",
    "image_size": "1344x768",
    "upscale": true,
    "rife": true
  }
}
```

- `prompt` — enhancer text; `__mmh3/...__` wildcards expand inside the QwenVL node (TagForge is baked in). The LTX presets want ONE flowing paragraph: motion arc, one continuous camera move that returns to the starting framing, audio cues
- `images` — filled into the LoadImage slots in **temporal order** (sorted by each slot's keyframe `frame_idx`); 3 stills into the 5 pose-loop slots mirror automatically (A,B,C → A,B,C,B,A); a single still into FL2VA closes an A→A loop
- `seconds` — 5 / 10 / 20: drives both the video length and the QwenVL preset duration variant
- `vl_preset` — `LTX › NSFW I2V` / `LTX › NSFW FL2VA` / `LTX › NSFW Loop` (the enhancer's guidance preset)
- `enhance: false` — skip the QwenVL, use the prompt verbatim (passthrough)
- `image_size` — explicit `WxH` pins the PVR tier; omit for the baked default
- `upscale` / `rife` — TensorRT post chain · `"action": "health"` — status + workflow list, no generation

## Custom nodes baked in

`ComfyUI-VideoHelperSuite` · `ComfyUI-Easy-Use` · `ComfyUI-QwenVL-Mod` · `ComfyUI-RIFE-TensorRT-Auto` · `ComfyUI-Upscaler-TensorRT-Auto` · `ComfyUI-TagForge` · `ComfyUI-PerfectVideoResolution` · `ComfyUI-Pixaroma`

## Companion stack

- 🎥 Video sibling: [huchukato/runpod-minimax-h3](https://github.com/huchukato/runpod-minimax-h3) (MiniMax H3 + Director)
- 🖼 Image sibling: [huchukato/runpod-qwen21](https://github.com/huchukato/runpod-qwen21) (Qwen Image 2.1 T2I/Edit + Pony)
