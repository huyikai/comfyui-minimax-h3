---
name: h3-replica-run
description: 从一个抖音 / B 站链接一路做到成片。先用 video-script 的写稿技能拆段落库，再在本地 H3 上渲 0.4 分片、逐段自检修到过、合剪再查合剪，然后 0.9 直接合剪只查合剪，最后出本号封面并发邮件。用户丢链接说「做成片」「复刻这条」「按人间隙那套跑」时用。
disable-model-invocation: true
---

# 链接到成片

一条链接跑完全程，中途不问用户。质检与迭代纪律见 `finish-video` 规则，落库位置见 `script-library` 规则，中间物位置见 `workspace-hygiene` 规则——那三条是常驻的，这里不重复。

复制这份清单跟踪进度：

```text
- [ ] 1 取材：下片、下原封面、探时长
- [ ] 2 读片：切点、对白、屏幕字时间线
- [ ] 3 写稿落库到 video-script
- [ ] 4 precheck
- [ ] 4.5 故事逻辑审读（子 agent 通读，生成前）
- [ ] 5 生成 0.4 分片，不合剪
- [ ] 6 逐段自检 + 迭代，直到每条都过
- [ ] 7 合剪 0.4，查只有拼起来才看得出的问题
- [ ] 8 生成 0.9 并合剪（不再逐段自检）
- [ ] 9 合剪自检 0.9
- [ ] 10 出本号封面
- [ ] 11 完成邮件
```

**先把每条 clip 单独修干净，再去看合剪。** 一条崩了就整条重合剪、整条重看，是把二十多分钟的渲染和几十张读图反复浪费在同一个问题上。

**逐段自检只在 0.4 做。** 0.9 是同一批脚本同一个 seed，入画人数、口型、演的是不是那件事这些在 0.4 就定死了，再读一遍二十多张横条是白花时间。0.9 只走合剪自检——那一步本来就要求每段至少抽 1 帧，够兜住「这一段在高分辨率下崩了」。

## 1 取材

```powershell
yt-dlp -o "原片.%(ext)s" <链接>
yt-dlp --write-all-thumbnails --skip-download <链接>   # 只留 id 为 cover 的，丢掉 origin_cover
```

这里下的是**原片封面**，存成 `原封面.jpg` 留档，只作参考。本号自己的封面在第 10 步生成，不复刻它。

下不到（抖音要 cookie）就在 `发布.md` 的「原案归档」里写明，不要拿首帧顶替，也不要为此停下来。

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

`[FAIL]` 全部改完再往下。它替你机械判定的有：读不出时长、汉字漏在 `<d>` 外面、禁用词、缺 Identity lock 或 `Speaking assignment`、锁句跨条不一致、节拍没从 0 铺满到时长、同一句台词出现在两条、段总表对白与 clip 对不上、段总表缺表。这些到了生成阶段修一次要多花几十分钟。

还要**亲自读那张 `[SPEAK]` 表**：每句对白和它挂在谁身上并排列着，逐句问「这句话该由谁说」。

## 4.5 故事逻辑审读

`precheck` 只认格式，认不出「三十岁的救援者喊妈妈」为什么荒谬。**生成前**按段起子 agent 通读，一段一个，一条消息里并行发：

```text
读 D:\develop\video-script\人间隙\NN-作品\03-段名\ 下的 00-overview.md 和全部 clip-*.md。
不看画面，只读脚本，找逻辑说不通的地方：
1 每句 <d> 的内容该由谁说（称谓、通报的信息、祈使对象、评价方向），与 Speaking assignment 对不对得上
2 因果链：上一条的结果是不是下一条的前提，中间缺不缺一拍
3 这个年龄、这个身份的人做得到这件事吗
4 道具和外观连续：这条出现的东西上一条哪来的，同一个人衣服变没变
5 过去/现在的切换有没有交代，同场景的光线天气对不对得上
6 台词和动作有没有互相矛盾
7 这一段演的是不是段总表写的那句戏剧核

一条一行报：clip 名 + 哪一项 + 说不通在哪 + 建议怎么改。
没问题的段只回「过」。**不要改任何文件。**
```

报回来的每一条，自己读过脚本再动手，然后按 `finish-video` 的「改还是问」分流：**执行错误自己改，故事漏洞停下来问用户**。改完重跑 `precheck`，再进第 5 步。

## 5 / 8 生成

两次的 `--output-dir` 要分开，否则 0.9 会覆盖掉 0.4 的分片、想回滚就没了。

0.4 加 `--no-concat`，渲完就停，等第 6 步逐段过了再合剪：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --filter "人间隙/NN-作品" --megapixels 0.4 `
  --output-dir "$HOME\Downloads\人间隙-NN-作品-0.4" `
  --concat-out "$HOME\Downloads\人间隙-NN-作品-成片-0.4.mp4" --no-concat
```

`--concat-out` 照样要写，第 7 步同一条命令把 `--no-concat` 换成 `--concat-only` 就能接上。

0.9 **不加** `--no-concat`，渲完直接合剪，跳过逐段自检进第 9 步：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --filter "人间隙/NN-作品" --megapixels 0.9 `
  --output-dir "$HOME\Downloads\人间隙-NN-作品" `
  --concat-out "$HOME\Downloads\人间隙-NN-作品-成片.mp4"
```

程序自己会做的，不用另外操心：断点续跑（同参数已渲染的直接复用）、单条失败重试、**显存崩了自动杀掉 ComfyUI 重启再续**、排队超 45 分钟判超时、音频归一化与接缝桥接、出 ASS 烧字、合剪加片尾、发进度邮件。

## 6 逐段自检（只在 0.4 做）

每条 clip 抽三帧拼一张横条，一条一图地读：

```powershell
.\.venv\Scripts\python.exe tools\qc_frames.py --work "人间隙/NN-作品" `
  --dir "$HOME\Downloads\人间隙-NN-作品-0.4"
```

这一层只判**单条自己就能判的**四件事，见 `finish-video` 清单的分片级部分：入画人数对不对得上 Identity lock、说话的人嘴动没动、演的是不是那件事、画面有没有崩。横条上看不准就对那一条 `--at` 抽全分辨率的帧细看。

**发现问题先定位原因再改，不许直接换 seed 重跑。** 单条最多 5 轮，每轮追加 `logs/qc-history/<账号>-<NN-作品>.md`。

只重跑某一条，改完立刻重抽这一条的横条确认：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --filter "人间隙/NN-作品/03-段名/clip-02" --megapixels 0.4 `
  --output-dir "$HOME\Downloads\人间隙-NN-作品-0.4" --fresh --no-notify
```

每条都过了才进第 7 步。

### 读图交给子 agent

二十几条横条自己一张张读，上下文很快就撑满了。按段拆给几个子 agent 并行读，**子 agent 只筛不判**：它只回报「哪条可疑、看到什么现象」，改不改、改哪一层由你定。它报回来的每一条，**你要亲自打开那张图确认再动手**——描述不是证据，照着描述改脚本容易改错地方。

一个子 agent 带一到两段，一条消息里发多个 Task 并行。prompt 必须自带全部上下文，它看不到本轮对话：

```text
读 D:\develop\minmaxH3\.scratch\人间隙-NN-作品\qc\clips\ 里 h3-t2v-turbo-03-*.jpg 这几张。
每张是一条 clip 的三帧横排，左中右按时间先后。
对照脚本 D:\develop\video-script\人间隙\NN-作品\03-段名\clip-XX.md，逐条看四件事：
1 入画人数与该 clip 的 Identity lock 写的人数一致
2 有 <d> 对白的拍里，说话的那个人嘴是张开的
3 演的是中文对照里写的那件事，不是「看着像那么回事」
4 画面没崩：手指数目、肢体扭曲、道具或衣服上的乱码字

只回报可疑项：clip 名 + 第几帧 + 看到什么现象，一条一行。
其余的只回「过」。不要推测原因，不要改任何文件。
```

## 7 / 9 合剪与合剪自检

第 7 步要先合剪（0.9 在第 8 步已经自己合过，直接看）：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --filter "人间隙/NN-作品" --megapixels 0.4 `
  --output-dir "$HOME\Downloads\人间隙-NN-作品-0.4" `
  --concat-out "$HOME\Downloads\人间隙-NN-作品-成片-0.4.mp4" --concat-only
```

这一层只判**拼起来才看得出的**问题，见 `finish-video` 清单的合剪级部分：日志的 `[QC]` / `[QC-WARN]`、`ffprobe` 总时长与音轨、黄字白字的时机与位置、片尾字卡、跨段有没有把同一张脸用在两个角色上。

```powershell
.\.venv\Scripts\python.exe tools\qc_frames.py "$HOME\Downloads\人间隙-NN-作品-成片-0.4.mp4" `
  --work "人间隙/NN-作品" --every 12 --tail
```

`--every 12` 保证每段至少落到 1 帧。0.9 这一轮不再逐段读横条，就靠这些帧兜住「某段在高分辨率下崩了」——真崩了就单独重跑那一条 0.9，再 `--concat-only`。

**字幕、片尾、响度这些改的是 `run_video_scripts.py` 或段总表，不用重生成分片**，`--concat-only` 一分钟出结果。只有确实是某条 clip 画面的问题，才退回重跑那一条。

## 10 本号封面

合剪过了再做，因为要用成片的静帧当参考。这是本号自己的封面，不是第 1 步那张 `原封面.jpg`。

先从成片里抽几张认得出脸的静帧，连同 `video-script\人间隙\品牌\头像.png`、`主页背景.png` 一起作为参考图，生成 3:4 封面，存到作品目录 `封面.png`：

- 大字标题**在左上，但不贴顶**：上边缘离顶约 18%、离左 8%。信息流和主页宫格会在左上角压发布时间、置顶标签，贴顶就被啃掉。正中留给播放键
  英文提示词里写死：`stacked in the left third, top of the lettering starting about 18% down from the frame top — leave the very top strip empty for the platform badge, not flush with the top edge`
- 色调跟头像走：脸上暖光，环境冷；脸要曝光够、能看清，不要压成死黑
- 缝边 / 前景人物用本片人物，跟静帧对得上
- **不要**号名、不要拼音或英文、不要播放键、不要水印、不要「本片由AI生成」
- **不要**复刻原封面的字和构图

字写错就只改字重生成，别整张重画。定稿后把提示词照 `01-越界/发布.md` 的格式写进本作品 `发布.md` 的「封面」段，废稿标注弃用。

## 11 完成邮件

合剪成功时程序自己会发带成片的邮件。履历里还有没解决的条目，把它们补进正文，写清楚是哪一条、什么问题、试过什么。

## 预设故障

见 [troubleshooting.md](troubleshooting.md)。
