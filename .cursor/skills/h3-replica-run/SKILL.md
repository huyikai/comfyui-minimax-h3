---
name: h3-replica-run
description: 从抖音 / B 站链接一路做到成片，一条或一批都走这套。先用 video-script 的写稿技能拆段落库，再在本地 H3 上渲 0.4 分片、逐段自检修到过、合剪再查合剪，然后 0.9 直接合剪只查合剪，最后出本号封面并发邮件。用户丢链接说「做成片」「复刻这条」「这几条都做了」「按人间隙那套跑」时用。
disable-model-invocation: true
---

# 链接到成片

链接跑完全程，中途不问用户。**全自动铁律见 `never-stop.mdc`**：用户不参与；唯一可中断的是环境检查失败（记盘 + 邮件）。质检与迭代纪律见 `finish-video` 规则，落库位置见 `script-library` 规则，中间物位置见 `workspace-hygiene` 规则——那三条是常驻的，这里不重复。

**上一条没到第 11 步（完成邮件）之前，不准开下一条的取材。** 「等正在跑的完成再做这条」＝ 等到成片邮件，不是等到分片渲完。新链接先占号，`batch-status.json` 写成 `排队`。

**多个链接一次丢进来时先读[批量](#批量多个链接)那一节**，它决定这份清单要走几遍、哪几步能合起来跑。

复制这份清单跟踪进度，**一个作品一份**：

```text
- [ ] 0 环境检查：cookie（Chrome + Cursor 内置浏览器）
- [ ] 1 取材：下片、下原封面、探时长
- [ ] 2 读片：source_agent 出时间线（切点 / OCR / ASR / 按窗画面）
- [ ] 3 写稿落库到 video-script
- [ ] 4 precheck
- [ ] 4.5 故事逻辑审读（子 agent 通读，生成前）
- [ ] 5 生成 0.4 分片，不合剪
- [ ] 6 逐段自检 + 迭代（5 次仍不过记遗留，照样合剪）
- [ ] 7 合剪 0.4，查只有拼起来才看得出的问题
- [ ] 8 生成 0.9 并合剪（不再逐段自检）
- [ ] 9 合剪自检 0.9
- [ ] 10 出本号封面
- [ ] 11 程序完成邮件 + 有遗留则另发遗留邮件
```

**先把每条 clip 单独修干净，再去看合剪。** 一条崩了就整条重合剪、整条重看，是把二十多分钟的渲染和几十张读图反复浪费在同一个问题上。

**逐段自检只在 0.4 做。** 0.9 是同一批脚本同一个 seed，入画人数、口型、演的是不是那件事这些在 0.4 就定死了，再读一遍二十多张横条是白花时间。0.9 只走合剪自检——那一步本来就要求每段至少抽 1 帧，够兜住「这一段在高分辨率下崩了」。

## 0 环境检查（唯一可中断）

开跑、或从排队里领下一条去取材之前，先查 cookie。**不要问用户关没关 Chrome。**

```powershell
.\.venv\Scripts\python.exe tools\envcheck.py --url "<链接>" --mail --label "人间隙"
```

它按这个顺序试，哪边能用就写出 `logs/cookies.txt`：

1. 已有的 `logs/cookies.txt`
2. 系统 Chrome（库被锁就走卷影拷贝；新版 Chrome App-Bound 加密解不开就继续往下试）
3. Cursor 内置浏览器盘上的库：`%APPDATA%\Cursor\Partitions\cursor-browser\Network\Cookies`（同样走卷影）
4. Edge（若有）

exit 0：后面所有 `yt-dlp` 都加 `--cookies logs/cookies.txt`。

exit 2 还不算停。主 agent 再用 Cursor 内置浏览器走一遍：

1. `browser_navigate` 打开那条抖音链接
2. `browser_cdp` → `Network.getAllCookies`
3. JSON 存成 `.scratch/_env/cdp-cookies.json`（不要写仓库根）
4. `tools\envcheck.py --import-cdp .scratch/_env/cdp-cookies.json --url "<链接>" --mail`

还是 2：这才是环境失败。`batch-status.json` 写成失败原因，邮件已经由 `--mail` 发出，**整条链停**。不要占一堆空号、不要问人怎么补 cookie。

磁盘 < 2 GB、ComfyUI 重启后仍连不上：`run_video_scripts.py` 自己硬挡，同样算环境失败，不要绕过。

## 1 取材

```powershell
yt-dlp --cookies logs/cookies.txt -o "原片.%(ext)s" <链接>
yt-dlp --cookies logs/cookies.txt --write-all-thumbnails --skip-download <链接>   # 只留 id 为 cover 的，丢掉 origin_cover
```

这里下的是**原片封面**，存成 `原封面.jpg` 留档，只作参考。本号自己的封面在第 10 步生成，不复刻它。

环境检查已经过了之后，单条仍然下不到（片被删、403、风控）才占号跳过：目录里只留 `发布.md`（原案归档写链接和失败原因，正文写「取材失败，未生成」），**不要**拿首帧顶替，不要建 clip，不要把这条放进 `--works`。后面的链接继续往下编号。**缺 cookie 不属于这一类**——缺 cookie 在第 0 步已经停过了。

## 2 读片

```powershell
.\.venv\Scripts\python.exe tools\source_agent.py 原片.mp4 --work "人间隙/NN-作品"
```

它会 0.5 秒抽帧、OCR、转写人声，再按 3 秒窗把六帧拼成横条交给 SDK。产物在 `.scratch/<作品>/source/timeline.json` 和 `timeline.md`。

**主对话不要通读 `frames/`。** 写稿读 ticks；只打开 `timeline.md`「必须开图」里的横条，以及拟拆段之后每段 2–3 张人最清楚的帧（写外观锁）。附录的看不清、「有对白嘴未动」不必开图——后者当画外音提示。说话人按台词内容判，不要按这一窗画面上是谁。

`timeline.md` 要读出：每句对白的起止（OCR 有字时以 OCR 为准，无字幕才看 ASR）、**解释性标题什么时候出、什么时候消失**、每一窗入画几人、谁的嘴在动。黄字别默认全程挂着。

## 3 写稿落库

按 `script-library` 规则读 video-script 那边的 README 和 `writing-h3-replica-scripts` 技能，按那边的骨架落盘。拆段按**事件**不按刀数，单条时长按这一拍的实际长度写。

## 4 precheck

```powershell
.\.venv\Scripts\python.exe tools\precheck.py --work "人间隙/NN-作品"
```

`[FAIL]` 全部改完再往下。它替你机械判定的有：读不出时长、汉字漏在 `<d>` 外面、禁用词、缺 Identity lock 或 `Speaking assignment`、锁句跨条不一致、节拍没从 0 铺满到时长、同一句台词出现在两条、段总表对白与 clip 对不上、段总表缺表。这些到了生成阶段修一次要多花几十分钟。

还要**主 agent 自己读那张 `[SPEAK]` 表**：每句对白和它挂在谁身上并排列着，逐句问「这句话该由谁说」。不要把表丢给用户。

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

报回来的每一条，自己读过脚本再动手，按 `finish-video` 的「改还是记」分流：**执行错误自己改；要动故事才能圆的记进履历，按现稿继续，遗留邮件列出。不要停下来问。** 改完重跑 `precheck`，再进第 5 步。

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

这一层只判**单条自己就能判的**四件事，见 `finish-video` 清单的分片级部分：入画人数对不对得上 Identity lock、**口型两头**（该说的在动、不该说的闭着、说话人的嘴在不在画幅里）、演的是不是那件事、画面有没有能点名的崩坏。横条上看不准就对那一条 `--at` 抽全分辨率的帧细看。

**发现问题先定位原因再改，不许直接换 seed 重跑。** 单条最多 5 轮，每轮追加 `logs/qc-history/<账号>-<NN-作品>.md`。5 次仍不过：记进履历，**照样进第 7 步合剪，照样出 0.9**。

只重跑某一条，改完立刻重抽这一条的横条确认：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --filter "人间隙/NN-作品/03-段名/clip-02" --megapixels 0.4 `
  --output-dir "$HOME\Downloads\人间隙-NN-作品-0.4" --fresh --no-notify
```

5 次仍不过的条目留在履历里，不要为它们卡住合剪。

### 读图先让 qc_agent 初筛

二十几条横条自己一张张读，上下文很快就撑满了。`tools\qc_agent.py` 走 Cursor SDK 并发跑，一条 clip 一个只读 agent，把横条和该 clip 的中文脚本一起发过去：

```powershell
.\.venv\Scripts\python.exe tools\qc_agent.py --work "人间隙/NN-作品" `
  --model gemini-3.7-flash --param effort=low --concurrency 4
```

报告落在 `.scratch\<作品>\qc\agent-report-<模型档位>.md`，**点名的排在前面**。27 条约两分钟。

**它是初筛，不是门禁。** 点名的那些**主 agent 自己打开那张图确认再动手**——不是用户看。描述不是证据，照着描述改脚本容易改错地方。同样配置两轮之间约有六分之一的条目会翻面，所以不要拿单轮的「过」当放行。

`Read` 返回 captioning unavailable / 看不见像素时，立刻 SDK 兜底，同一回合继续，不许停：

```powershell
.\.venv\Scripts\python.exe tools\see_image.py --strip "<横条.jpg>"
```

再失败才起只读子 agent。三条都失败：用报告里「它看见的」继续改，记履历，照样往下。

报告里每条都带一栏「它看见的」，那是它对三帧的**独立描述**（生成时被要求先描述、后对脚本，防止它顺着脚本编）。先看结论是不是从这段描述推出来的，对不上就自己开图。

几个实测出来的坑，改这个脚本时别踩回去：

- **给它看脚本会诱发确认偏误**。早期版本没要求先描述，它会把脚本当答案——脚本写「踩在肩上」，它就报告看见了踩在肩上，而画面里踩在后脑。两步法是为这个加的
- **让它「宁可多报」会退化成每条都报「手指发糊」**。改成要求它**点名**具体是什么坏了，说不出就判过，标记率从 81% 降到六成左右且没丢真缺陷
- **给 `内容` 项写采样豁免要写窄**。写成「三帧看不出变化就判过」，它会连「脚本要求的闪电压根没出现」一起豁免掉
- **`画面` 项的缺陷清单不能封闭**。列成五类它就只查那五类，背景里多出来的异常物体判过

儿童单人特写有时会被 provider 以内容安全为由拒掉（`status=error`，重试无效），`--fallback` 默认换 `composer-2.5` 再试一次，27 条里大约撞上 2 条。

自己起子 agent 读图也可以，规矩一样：**只筛不判**，且四项要按 `finish-video` 那份写（口型是两头都看的）。

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

合剪成功时程序自己会发带成片的邮件，**不要改那封的正文**。履历里还有没解决的条目，另发一封遗留邮件，**开头**就是未过检：哪一条、什么现象、试过几轮。

```powershell
.\.venv\Scripts\python.exe notify.py leftover --label "04-懦弱" --file leftover.txt
```

`leftover.txt` 第一行开始就是坏镜，不要先写「成片已完成」。没有遗留就不用发这封。

## 批量：多个链接

**先认清哪几步是显卡的活、哪几步是模型的活。** 写稿和读图自检要的是模型，换不成显卡；渲染要的是显卡，几小时无人值守也行。批量就是把显卡那两段合起来跑，剩下的按作品走。

序号按本号发布顺序连排，用户给链接的顺序就是序号顺序（`script-library` 规则）。

| 阶段 | 步 | 怎么跑 |
| --- | --- | --- |
| 0 环境 | 0 | 一条 `envcheck.py --url <第一条链接> --mail`。失败则整批停 |
| A 写稿 | 1–4.5 | 主 agent 先占号，子 agent 池 4 路补位写，逐条过 precheck 和逻辑审读才放行 |
| B 渲 0.4 | 5 | 一条 `--works` 命令把**已放行的**全渲，不合剪 |
| C 逐段自检 | 6 | 按作品，一个作品一轮读图 |
| D 合 0.4 → 渲 0.9 | 7–8 | 各一条 `--works` 命令 |
| E 合剪自检 / 封面 / 邮件 | 9–11 | 按作品；有遗留的另发遗留邮件 |

每条链接走到哪一步记在 `logs/batch-status.json`，换会话靠它接手。

### A 写稿：子 agent 并行写，主 agent 把关

**先占号再开工。** 主 agent 按链接顺序把序号和目录名全部定死，写进 `logs/batch-status.json`，再派活——序号是发布顺序，让子 agent 自己挑号必撞。

**池子最多 4 路**，一个写完立刻补下一条已占号的作品，不要凑够四个再一起发下一波。目录和号已经占死了，补位不会撞。

一条链接一个子 agent。子 agent 只做第 1–3 步（取材、读片、写稿落库），**不许自己判定通过**：

```text
按 D:\develop\video-script\README.md 和 .cursor\skills\writing-h3-replica-scripts\SKILL.md 复刻这条链接：<链接>
落库到 D:\develop\video-script\人间隙\<NN-作品名>\，骨架和命名照那两份文档。
先用 D:\develop\minmaxH3\tools\source_agent.py 出时间线（0.5 秒抽帧 + OCR + 转写 + SDK 按窗读图），再按 .scratch/<作品>/source/timeline.json 拆段写稿。不要通读 frames/。说话人按台词内容判，不要按画面上是谁。
拆段按事件不按刀数；单条时长按这一拍的实际长度写，不足 4 秒写 4.00。
写完自己跑一遍 tools\precheck.py --work "人间隙/<NN-作品名>"，把 [FAIL] 改干净。
回报：落了哪几段、每段几条、precheck 最后的结果、你拿不准的地方。不要出图、不要渲染。
```

它们回来之后，**主 agent 逐条自己跑 precheck、自己读 `[SPEAK]` 表、自己起逻辑审读**（第 4、4.5 步）。子 agent 说过了不算过——写稿是最吃判断的一步，方差最大的也是它。

**写不出来的那条：号占住，不进渲染。** 片被删、`[FAIL]` 修不完，都算这一类。在该作品目录只留一份 `发布.md`，写清链接、卡在哪一步、为什么，然后把它从 `--works` 列表里去掉。**序号不要让给后面的作品**。写得过的先进 0.4 队列，不为它等。缺 cookie 不走这里，走第 0 步环境检查。

### 批量状态：换了会话还能接着干

显卡进度有 `logs/video_script_progress.json`，但「谁已放行、谁跳过了、跳过原因」只在对话里，换一次会话就没了。十几条链接跨很多轮，**必须落盘** `logs/batch-status.json`（在 `logs/` 下，已 gitignore）：

```json
{"人间隙/05-甲": {"链接": "...", "阶段": "已放行", "原因": ""},
 "人间隙/06-乙": {"链接": "...", "阶段": "跳过", "原因": "原片 404，未生成"}}
```

阶段就用这几个词：`环境检查` `排队` `取材` `写稿` `已放行` `跳过` `0.4` `自检` `0.9` `邮件`。**新会话先读它再动手**，别靠「作品目录在不在」猜——占了号但没生成的目录里也有 `发布.md`，只看目录会误判成「写到一半，接着写」。会话接手时若上一条还没到 `邮件`，先把它跑完，不要去开 `排队` 里的下一条。

### B / D 批量渲染

`--works` 接多个作品，每个作品各自一个子目录、各自一条成片、各自一封邮件。一个作品塌了记下来，接着跑下一个，最后来一封汇总。**不要**再给 `--concat-out`，那是每个作品自己派生的。

0.4 全渲、不合剪：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --works "人间隙/05-甲" "人间隙/06-乙" "人间隙/07-丙" `
  --megapixels 0.4 --no-concat `
  --output-dir "$HOME\Downloads\人间隙-0.4"
```

`--output-dir` 在 `--works` 下是**父目录**：分片落到 `人间隙-0.4\人间隙-05-甲\`，成片是 `人间隙-0.4\人间隙-05-甲-成片.mp4`。0.4 和 0.9 给不同的父目录，否则 0.9 覆盖掉 0.4 就没法回滚。

全部自检过了再批量合剪，同一条命令换 `--concat-only`：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --works "人间隙/05-甲" "人间隙/06-乙" `
  --megapixels 0.4 --concat-only `
  --output-dir "$HOME\Downloads\人间隙-0.4"
```

0.9 不加 `--no-concat`，渲完自己合，直接进第 9 步：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --works "人间隙/05-甲" "人间隙/06-乙" `
  --megapixels 0.9 `
  --output-dir "$HOME\Downloads\人间隙-0.9"
```

断点续跑是按 clip 记的，同参数已渲的会跳过，所以批量中断了原样重发同一条命令即可。`--fresh` 只清当前作品范围，不会碰别的作品。

开跑前程序自己会挡和报，不用另外操心：

- **同时只允许一份**。已有任务在跑（pid 还活着）第二份直接退出；上次崩了留下的僵尸锁会自动接管并记一行 `[LOCK] 清了僵尸锁`。所以**不要**在另一个终端再开一条 `--works`，单卡并行只会互相抢 ComfyUI、还会把进度文件写坏
- **硬挡**：输出盘剩余 < 2 GB、ComfyUI 连不上（`--concat-only` 不查 ComfyUI，它不用显卡）
- **只警告、写进「开始」邮件**：盘剩余 < 10 GB、电源计划会睡眠（过夜会被冻死，要无人值守就自己先改成从不）、缺 `logs/smtp.json`

### C / E 按作品，不要混着看

读图和封面必须一个作品一轮。**不要**把两个作品的横条混在一个子 agent 里筛——跨作品撞脸本来就是要查的项，混着看就查不出来了。某个作品自检卡住，按 `finish-video` 的规矩记进履历、继续下一个，不要停下来问。

## 预设故障

见 [troubleshooting.md](troubleshooting.md)。
