---
name: h3-video-from-script
description: 拿用户自带的文案生成视频。先检查脚本并给优化方案，缺的参数问清楚，再落成生成程序读得懂的骨架，在本地 H3 上出片。用户丢一段文案、一个 md 文件或一个项目目录说「做成视频」时用，区别于走 video-script 复刻流程的 h3-replica-run。
disable-model-invocation: true
---

# 自带文案到成片

用户的输入不一定规范，**先检查、先给优化方案、先问缺的参数，再动手生成**。这一点跟 `h3-replica-run` 不同——那边是复刻，源片就是标准答案；这边没有标准答案，参数得问。

产物不进 `video-script`（那是人间隙的脚本库）。落在用户指定目录，默认 `~/Downloads/<作品名>/`。

```text
- [ ] 1 读懂输入
- [ ] 2 检查并给优化方案
- [ ] 3 问缺的参数
- [ ] 4 落成骨架
- [ ] 5 precheck
- [ ] 6 生成 + 自检迭代
- [ ] 7 出片
```

## 1 读懂输入

三种形态：一段文案、一个 md、一个目录。目录的话先摸清楚有没有现成的分镜、时长、人物设定。

先判断这是**短片**（一个场景、一条 clip 打得住）还是**长片**（要拆多条硬切合剪）。H3 单条只收 **4.00–15.00 秒**，超过就必须拆。

## 2 检查并给优化方案

照下面这张表过一遍，把问题连同改法一起摆给用户，不要只说「建议优化」：

| 查什么 | 不合格的样子 | 怎么改 |
| --- | --- | --- |
| 是不是可拍的动作 | 「表现主角的孤独」 | 落到看得见的动作：他停在门口，手放在把手上没拧 |
| 有没有空词 | `cinematic`、`emotional`、`beautiful` | 删掉，换成具体的光、景别、动作 |
| 一拍几件事 | 一条里又走路又打电话又哭 | 拆成两条，切在动作停顿处 |
| 对白长度 | 一条 5 秒里塞了三十个字 | 按语速估，中文约每秒 4–5 字；超了就拆条，**禁止把一句劈到两条** |
| 人物一致性 | 每条对同一个人的描述都不一样 | 定一份 Identity lock，各条**逐字复用** |
| 要不要字幕 | 没说 | 默认不要：英文里禁 `subtitle` / `caption`，汉字只放 `<d>` 里 |
| 时长 | 没写或统一 5 秒 | 按每一拍的实际需要写，不足 4 秒写 4.00 用保持反应填 |

英文三字段的写法读 `D:\develop\video-script\.cursor\skills\h3-prompt-writing\SKILL.md`，不要另起一套。

## 3 问缺的参数

用户没给就用 `AskQuestion` 一次问齐，不要一个个来回。默认值放第一项：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| 画幅 | 16:9 横屏 | 竖屏改 `ASPECT` |
| 清晰度 | 先 0.4 试片，确认后 0.9 | 8GB 显存跑 0.9 已接近上限 |
| steps | 4 | Turbo LoRA，建议 4–8 |
| 要不要字幕 | 要，白字底部 | 由程序后期出 ASS，不烧进 H3 |
| 要不要片尾字卡 | 不要 | 要的话给文案 |
| 配乐 | 不要 | H3 的 `non_diegetic_music` 写 N/A |
| 成片落哪 | `~/Downloads/<作品名>/` | |

## 4 落成骨架

生成程序只认这套结构，少一样就跑不起来：

```text
<作品名>/
  00-overview.md        ← 合剪顺序、生成时长表、片尾字卡
  01-段名/
    00-overview.md      ← 屏幕字表、每条的过去/现在
    clip-01.md
```

每个 `clip-XX.md` 必须有两样东西，程序靠正则抠：

- 文件头写 ``时长：`4.80s``
- 一个 ` ```text ` 围栏块，里面是英文 prompt，标题写 `## 英文 H3 Prompt`

只有一个场景、不需要黄字时，段总表的「屏幕字」表可以省，程序会跳过黄字。

## 5 precheck

```powershell
.\.venv\Scripts\python.exe tools\precheck.py --work "<作品名>"
```

注意 `precheck.py` 和 `run_video_scripts.py` 都从 `SCRIPT_ROOT`（`D:\develop\video-script`）找 clip。骨架落在别处时，要么把 `--filter` 指向那边，要么临时改 `SCRIPT_ROOT`——**改了记得改回来**，别把通用用法的产物混进人间隙的脚本库。

`[FAIL]` 全改完，`[WARN]` 逐条判断，再亲自读 `[SPEAK]` 表逐句核「这句话该由谁说」。

## 5.5 故事逻辑审读

`precheck` 只认格式，认不出「三十岁的人喊妈妈」为什么荒谬。**生成前**按段起子 agent 通读段总表和全部 clip，检查项和落笔分级照 `finish-video` 的「故事逻辑审读」「改还是记」两节走，子 agent 报的提示词见 [h3-replica-run](../h3-replica-run/SKILL.md) 第 4.5 步。

用户自带的稿子逻辑漏洞通常比复刻稿多。第 2 步给优化方案时可以把故事漏洞列进去；用户说做片之后按现稿跑——**执行错误自己改，要动设定才能圆的记进履历继续跑，不要中途停下来问，也不要替他补设定。**

## 6 生成与自检

先渲分片、不合剪：

```powershell
.\.venv\Scripts\python.exe run_video_scripts.py `
  --filter "<作品名>" --megapixels 0.4 `
  --output-dir "$HOME\Downloads\<作品名>-0.4" `
  --concat-out "$HOME\Downloads\<作品名>-成片-0.4.mp4" --no-concat
```

逐段自检，每条 clip 一张横条：

```powershell
.\.venv\Scripts\python.exe tools\qc_frames.py --work "<作品名>" --dir "$HOME\Downloads\<作品名>-0.4"
```

每条都过了再把上面那条命令的 `--no-concat` 换成 `--concat-only` 合剪，然后查合剪层面的问题。

自检和迭代纪律照 `finish-video` 规则走：分片级四项先过，再看合剪级；先定位原因再改，单条最多 5 轮，每轮记 `logs/qc-history/<作品名>.md`。没有源片可比对时，按脚本写的意图判——脚本说这拍两个人，画面就得是两个人。

故障对照表复用 [h3-replica-run/troubleshooting.md](../h3-replica-run/troubleshooting.md)，「生成阶段」和「画面」两节通用。
