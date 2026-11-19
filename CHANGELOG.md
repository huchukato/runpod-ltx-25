# Changelog

## v1.0.10

Fast worker registration: entrypoint materialization/volume-populate moved to a background subshell — handler.py starts immediately and ComfyUI boot waits on a prep-done marker, so cold starts and the Hub test deadline no longer include image prep

## v1.0.0

LTX 2.5 serverless worker — initial commit: handler (adapted from runpod-minimax-h3: temporal image fill with palindrome mirroring for the pose-loop slots, T2V/I2V switch drive, targeted duration patch), entrypoint + volume populate, models manifest (~61GB), Dockerfile (8 node packs), three baked API workflows, Hub manifest + README


## v1.0.1

Restore Hub tests.json — required for first listing approval (health-check smoke test, no volume needed)
Remove Hub tests.json — the automatic Hub test burns releases when no test GPU is available (same reason runpod-minimax-h3 and runpod-qwen21 ship without one); the health action stays in the handler for manual smoke tests
Hub tests.json: health-check smoke test (the handler answers without touching ComfyUI or models, so the test passes on a volume-less environment)


## v1.0.2

Style param, dual prompt trace, Loop-ABCBA audio
Fix first-job failures: chain order, filename_prefix fallback, hf %20
Single QwenVL_Unified enhancer + latent upscaler selection
Replace native LTX prompt enhancer with QwenVL_Unified_PromptEnhancer


## v1.0.3

handler: passthrough follows the enhance flag in both directions


## v1.0.4

handler: Director timeline builder (stills + | segments -> timeline_data) + workflow


## v1.0.5

boot: --enable-triton-backend (safe on int8-convrot, pods run it)


## v1.0.6

drop baked workflows-api (ForgeHub sync is the only source), manifest: taeltx2_3 + normal video VAE


## v1.0.7

Image: add ComfyUI-KJNodes (LTX2AttentionTunerPatch — the Director workflow needs it) and clone ComfyUI-LTX-Director-Enhanced explicitly so the node tracks main instead of the baked base-image copy (picks up the auto-split enhancement)
Drop empty HF_TOKEN env var (warning noise; all manifest models are public)



## v1.0.8

RelayVSR x2 video upscale: new ForgeHub workflow (VHS_LoadVideo → RelayVSRConditioning/KSampler/VSRDecode → VHS_VideoCombine) + manifest entries for the x2 DiT (4.81GB) and Flash VAE
handler: core LoadVideo input key is "file" (was "video" — VHS_LoadVideo keeps "video"); workflow sync honors meta.remote_file so one json can serve several endpoints

## v1.0.9

handler: URL-encode workflow sync paths (filenames with spaces broke the whole sync)
