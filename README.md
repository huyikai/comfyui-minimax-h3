# ComfyUI MiniMax H3

在本地 ComfyUI 上跑 [MiniMax H3](https://www.minimax.io/blog/minimax-h3) 的工作流和辅助脚本。H3 能同时理解文本、图像、视频和音频，并一次生成带原生立体声的视频（对白、音效、音乐一起建模）。

本仓库**不包含** ComfyUI 源码、Python 虚拟环境和模型权重。这些体积大、会过期，需要在本机另行准备。

## 仓库里有什么

| 路径 | 说明 |
| --- | --- |
| `workflows/` | MiniMax H3 工作流（文生、图生、首尾帧、参考生视频，以及 8GB 显存用的文生模板） |
| `download_models.py` | 断点续传下载官方剪枝 INT8 权重；国内可走 Hugging Face 镜像 |
| `start.ps1` | Windows 启动脚本：检查权重后用 SageAttention 拉起 ComfyUI |

## 实测环境

在以下环境跑通过文生视频（8GB 显存要降分辨率和时长，单次生成会比较慢）：

- Windows 10 / 11
- Python 3.12
- NVIDIA GPU（实测 RTX 5060 Ti 8GB + 约 32GB 内存）
- PyTorch `2.13.0+cu130`
- 较新的 [ComfyUI](https://github.com/comfyanonymous/ComfyUI)（需已内置 MiniMax H3 节点）
- SageAttention（`start.ps1` 会加上 `--use-sage-attention`）

16GB 及以上显存可以按官方默认分辨率跑。8GB 建议先用 `video_minimax_h3_t2v_8gb.json`。

## 快速开始

在仓库根目录执行（下面假设目录名就是克隆下来的 `comfyui-minimax-h3`）：

```powershell
git clone https://github.com/huyikai/comfyui-minimax-h3.git
cd comfyui-minimax-h3

git clone https://github.com/comfyanonymous/ComfyUI.git

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130
.\.venv\Scripts\python.exe -m pip install -r ComfyUI\requirements.txt
.\.venv\Scripts\python.exe -m pip install sageattention

.\.venv\Scripts\python.exe download_models.py
.\start.ps1
```

浏览器打开 ComfyUI 后，导入 `workflows/` 里的 JSON。

若还要参考生视频（Ref2VA），再下一份扩散模型（大约再加 20GB）：

```powershell
.\.venv\Scripts\python.exe download_models.py --ref2va
```

`download_models.py` 会先请求 Hugging Face 官方，失败则改走 `hf-mirror.com`。已存在且大于 1MB 的文件会跳过，支持断点续传。

## 工作流

| 文件 | 用途 | 需要的扩散模型 |
| --- | --- | --- |
| `video_minimax_h3_t2v.json` | 文生视频 | FL2VA |
| `video_minimax_h3_t2v_8gb.json` | 文生视频（8GB 显存，更低分辨率） | FL2VA |
| `video_minimax_h3_i2v.json` | 图生视频（首帧） | FL2VA |
| `video_minimax_h3_i2v_easycache.json` | 图生视频 + EasyCache | FL2VA |
| `video_minimax_h3_flf.json` | 首尾帧生视频 | FL2VA |
| `video_minimax_h3_r2v.json` | 参考生视频（图 / 视频 / 音频） | Ref2VA |
| `video_minimax_h3_r2v_video.json` | 参考生视频（偏视频参考） | Ref2VA |

FL2VA 和 Ref2VA 不能混用。默认下载的是 FL2VA，覆盖文生、图生、首尾帧。

## 模型文件

权重来自 [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3)，下载后放到 `ComfyUI/models/`：

```
ComfyUI/models/
├── vae/
│   ├── minimax_h3_video_vae_fp16.safetensors
│   └── minimax_h3_audio_vae_fp32.safetensors
├── text_encoders/
│   └── qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
└── diffusion_models/
    ├── minimax_h3_fl2va_pruned_int8_convrot.safetensors
    └── minimax_h3_ref2va_pruned_int8_convrot.safetensors   # 可选，--ref2va
```

本地开源的是 H3-Base（默认短边约 768p）。官方 2K 后处理没有开源，所以本地成片不等于官网 2K。

## 相关链接

- [MiniMax H3 介绍](https://www.minimax.io/blog/minimax-h3)
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
- [ComfyUI MiniMax H3 节点](https://github.com/Comfy-Org/ComfyUI/pull/15224)
- [Hugging Face 权重](https://huggingface.co/Comfy-Org/MiniMax-H3)
