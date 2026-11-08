# Changelog

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

