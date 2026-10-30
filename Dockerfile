# runpod-ltx-25 — LTX 2.5 serverless worker (CUDA 13.0)
#
# - LTX 2.5 22B distilled INT8 ConvRot DiT + Gemma-4 12B heretic INT8 TE
#   (+ E2B BF16 for the T2V prompt generator), 10Eros v1.5 LoRA, loaded from
#   the network volume (~60GB, needs a 96GB-class GPU for the dual-TE I2VA)
# - Missing volume files are auto-downloaded at worker boot from
#   models-manifest.txt (populate-volume.sh, VOLUME_AUTOPOPULATE=false to skip)
# - runpod serverless handler drives ComfyUI per-job (handler.py)
#

FROM huchukato/comfyui-base:cu130

# ──────────────────────────────────────────────────────────────────────────────
# Custom nodes — only what the three LTX API workflows reference
# ──────────────────────────────────────────────────────────────────────────────
ENV GIT_TERMINAL_PROMPT=0
RUN dl() { \
    local user="$1" repo="$2" branch="${3:-main}" dir="$4"; \
    echo "Cloning $user/$repo ($branch) -> $dir"; \
    git clone --depth 1 --branch "$branch" "https://github.com/$user/$repo.git" "/opt/comfyui-baked/custom_nodes/$dir"; \
  } && \
    cd /opt/comfyui-baked/custom_nodes && \
    dl Kosinkadink ComfyUI-VideoHelperSuite main ComfyUI-VideoHelperSuite && \
    dl yolain ComfyUI-Easy-Use main ComfyUI-Easy-Use && \
    dl huchukato ComfyUI-QwenVL-Mod main ComfyUI-QwenVL-Mod && \
    dl huchukato ComfyUI-RIFE-TensorRT-Auto master ComfyUI-RIFE-TensorRT-Auto && \
    dl huchukato ComfyUI-Upscaler-TensorRT-Auto master ComfyUI-Upscaler-TensorRT-Auto && \
    dl huchukato ComfyUI-TagForge v2.3.0 ComfyUI-TagForge && \
    dl huchukato ComfyUI-PerfectVideoResolution master ComfyUI-PerfectVideoResolution && \
    dl pixaroma ComfyUI-Pixaroma main ComfyUI-Pixaroma

# ──────────────────────────────────────────────────────────────────────────────
# Requirements + runpod serverless SDK
# ──────────────────────────────────────────────────────────────────────────────
# libgl1/libglib2.0-0: opencv-python (VideoHelperSuite / Easy-Use) fails to
# import without them → the whole pack never registers → missing_node_type.
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN bash -c 'cd /opt/comfyui-baked/custom_nodes && for node_dir in */; do \
        if [ -f "$node_dir/requirements.txt" ]; then \
            echo "Installing requirements for $node_dir..." && \
            pip install --no-cache-dir -r "$node_dir/requirements.txt" || echo "Failed to install requirements for $node_dir"; \
        fi; \
    done' && \
    pip install --no-cache-dir --no-deps "transformers>=5.2.0" && \
    pip install --no-cache-dir 'runpod>=1.12' requests boto3 "huggingface_hub[cli]" hf_transfer && \
    pip cache purge

# ──────────────────────────────────────────────────────────────────────────────
# Model dirs (empty — real models come from the network volume via
# extra_model_paths.yaml written by the entrypoint)
# ──────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /opt/comfyui-baked/models/{vae,vae_approx,diffusion_models,unet,text_encoders,clip_projections,loras,checkpoints,latent_upscale_models,LLM,clip,clip_vision}

ENV HF_TOKEN=""

# ──────────────────────────────────────────────────────────────────────────────
# Serverless entrypoint + handler + workflow templates (API format)
# ──────────────────────────────────────────────────────────────────────────────
COPY handler.py /opt/serverless/handler.py
COPY serverless-entrypoint.sh /opt/serverless/entrypoint.sh
COPY populate-volume.sh /opt/serverless/populate-volume.sh
COPY models-manifest.txt /opt/serverless/models-manifest.txt
COPY workflows-api/ltx/ /opt/workflows/
RUN chmod +x /opt/serverless/entrypoint.sh /opt/serverless/populate-volume.sh

ENV COMFYUI_DIR="/workspace/runpod-slim/ComfyUI" \
    WORKFLOWS_DIR="/opt/workflows" \
    JOB_TIMEOUT_S="1800" \
    VENV_SUFFIX="cu130"

# Base image carries ENTRYPOINT ["/start.sh"] (pod mode) — a bare CMD would be
# passed to it as args and the runpod handler would never start. Override the
# entrypoint so the container runs the serverless handler directly.
ENTRYPOINT ["/opt/serverless/entrypoint.sh"]
