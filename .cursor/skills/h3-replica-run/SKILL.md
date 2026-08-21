---
name: h3-replica-run
description: 从一个抖音 / B 站链接一路做到成片。先用 video-script 的写稿技能拆段落库，再在本地 H3 上生成 0.4 试片、自检迭代、0.9 正片、合剪出片并发邮件。用户丢链接说「做成片」「复刻这条」「按人间隙那套跑」时用。
disable-model-invocation: true
---

# 链接到成片

一条链接跑完全程，中途不问用户。质检与迭代纪律见 `finish-video` 规则，落库位置见 `script-library` 规则，中间物位置见 `workspace-hygiene` 规则——那三条是常驻的，这里不重复。

复制这份清单跟踪进度：

```text
- [ ] 1 取材：下片、下封面、探时长
- [ ] 2 读片：切点、对白、屏幕字时间线
- [ ] 3 写稿落库到 video-script
- [ ] 4 precheck
- [ ] 5 生成 0.4 并合剪
- [ ] 6 自检 + 迭代（每轮记履历）
- [ ] 7 生成 0.9 并合剪
- [ ] 8 自检 + 迭代
- [ ] 9 完成邮件
```

## 1 取材

```powershell
yt-dlp -o "原片.%(ext)s" <链接>
yt-dlp --write-all-thumbnails --skip-download <链接>   # 只留 id 为 cover 的，丢掉 origin_cover
```

下不到封面（抖音要 cookie）就在 `发布.md` 的「原案归档」里写明，不要拿首帧顶替，也不要为此停下来。

## 2 读片

```powershell
.\.venv\Scripts\python.exe tools\read_source.py 原片.mp4 --work "人间隙/NN-作品" --ocr
```

出 `probe.json`、`scene.txt`（硬切点）、`frames/`、`ocr.txt`（逐秒屏幕字时间线）。小字认不全时加 `--regions` 分区放大重认。

`ocr.txt` 要读出两件事：每句对白的起止秒、**解释性标题什么时候出、什么时候消失**。后者决定黄字挂多久，别默认全程挂着。

## 3 写稿落库

按 `script-library` 规则读 video-script 那边的 README 和 `writing-h3-replica-scripts` 技能，按那边的骨架落盘。拆段按**事件**不按刀数，单条时长按这一拍的实际长度写。

## 4 precheck

```powershell
.\.venv\Scripts\python.exe tools\precheck.py --work "人间隙/NN-作品"
```

`[FAIL]` 全部改完再往下。这一步能挡住的问题（读不出时长、汉字漏在 `<d>` 外面、禁用词、缺 Identity lock、段总表缺表），到了生成阶段修一次要多花几十分钟。

## 5 / 7 生成

0.4 试片先跑，确认没问题再上 0.9。两次的 `--output-dir` 要分开，否则 0.9 会覆盖掉 0.4 的分片、想回滚就没了。

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --filter "人间隙/NN-作品" --megapixels 0.4 `
  --output-dir "$HOME\Downloads\人间隙-NN-作品-0.4" `
  --concat-out "$HOME\Downloads\人间隙-NN-作品-成片-0.4.mp4"
```

程序自己会做的，不用另外操心：断点续跑（同参数已渲染的直接复用）、单条失败重试、**显存崩了自动杀掉 ComfyUI 重启再续**、排队超 45 分钟判超时、音频归一化与接缝桥接、出 ASS 烧字、合剪加片尾、发进度邮件。

只改了样式或字幕、分片不用重生成时，加 `--concat-only` 重合剪，一分钟出结果。

## 6 / 8 自检与迭代

按 `finish-video` 规则的十项清单逐条过。**发现问题先定位原因再改，不许直接换 seed 重跑。** 单条最多 5 轮，每轮追加 `logs/qc-history/<账号>-<NN-作品>.md`。

只重跑某一条：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --filter "人间隙/NN-作品/03-段名/clip-02" --megapixels 0.4 `
  --output-dir "$HOME\Downloads\人间隙-NN-作品-0.4" --fresh --no-notify
```

跑完再对整条 `--concat-only` 重合剪，重新自检。

## 9 完成邮件

合剪成功时程序自己会发带成片的邮件。履历里还有没解决的条目，把它们补进正文，写清楚是哪一条、什么问题、试过什么。

## 预设故障

见 [troubleshooting.md](troubleshooting.md)。
