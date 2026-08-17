$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Comfy = Join-Path $Root "ComfyUI"

if (-not (Test-Path $Python)) {
    throw "Virtualenv missing. Create it with: python -m venv .venv"
}

$required = @(
    "models\diffusion_models\minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "models\text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "models\vae\minimax_h3_video_vae_fp16.safetensors",
    "models\vae\minimax_h3_audio_vae_fp32.safetensors"
)
$missing = @($required | Where-Object { -not (Test-Path (Join-Path $Comfy $_)) })
if ($missing.Count -gt 0) {
    Write-Host "Missing model files. Run: .\.venv\Scripts\python.exe download_models.py"
    $missing | ForEach-Object { Write-Host "  - $_" }
    throw "MiniMax H3 FL2VA weights are not complete."
}

$TurboNode = Join-Path $Comfy "custom_nodes\ComfyUI-MiniMax-H3-Turbo"
if (-not (Test-Path $TurboNode)) {
    Write-Host "Cloning MiniMax-H3 Turbo custom node..."
    New-Item -ItemType Directory -Force -Path (Join-Path $Comfy "custom_nodes") | Out-Null
    git clone --depth 1 https://github.com/larryvrh/ComfyUI-MiniMax-H3-Turbo.git $TurboNode
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: failed to clone ComfyUI-MiniMax-H3-Turbo. Turbo workflows need this node."
    }
}

# ComfyUI is a separate clone. Re-apply the Windows logger workaround if missing.
$Logger = Join-Path $Comfy "app\logger.py"
$Patch = Join-Path $Root "patches\comfyui-logger-flush-windows.patch"
$WritePatch = Join-Path $Root "patches\comfyui-logger-write-windows.patch"
if ((Test-Path $Logger) -and (Test-Path $Patch)) {
    $writeGuarded = Select-String -LiteralPath $Logger -SimpleMatch 'especially tqdm' -Quiet
    if (-not $writeGuarded) {
        git -C $Comfy apply --whitespace=nowarn $Patch
        if ($LASTEXITCODE -ne 0 -and (Test-Path $WritePatch)) {
            git -C $Comfy apply --whitespace=nowarn $WritePatch
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Warning: failed to apply Windows logger patches"
        }
    }
}

Set-Location $Comfy
& $Python main.py --use-sage-attention --disable-pinned-memory --preview-method auto
