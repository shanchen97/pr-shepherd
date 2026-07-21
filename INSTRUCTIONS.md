# PR-Watch 巡检指令

> 本文件是**模板**。`{{...}}` 占位符由 `run.sh` 在运行时用 `config.env` 的值渲染成
> `.INSTRUCTIONS.local.md` 后再喂给 Claude。仓库里提交的这份不含任何组织名/用户名/路径。

两条独立流水线,每次巡检都跑:

- **Mine** — 我(@{{GITHUB_USER}})自己的 open PR:未解决的反馈 → 分析给修复方案
- **Others** — 待我 review 的 open PR(被点名 + CODEOWNERS 命中):深度 production review → 本地 HTML 报告

## 扫描范围:白名单驱动,逐仓库枚举

**只扫描下面白名单里的仓库,一个都不能少、也不要扩到白名单之外。** 对**每一个**仓库都要独立跑完 Mine 和 Others 两条线——不要因为某个仓库"通常没动静"就跳过,也不要只盯着其中一两个仓库(这正是以前漏掉整仓库 PR 的根因)。

白名单(仓库 → 本地 checkout 路径):

{{REPO_WHITELIST_TABLE}}

所有仓库同属组织 `{{GITHUB_ORG}}`,完整名即 `{{GITHUB_ORG}}/<repo>`。

**只关注 base 分支为 `{{BASE_BRANCHES}}` 的 PR**。两条线枚举出候选后,都要按 `baseRefName` 过滤:落在这几类里才保留(`release*` 指 release 开头),其它 base(如 `testnet_all`、feature 分支)一律跳过。

**不关注 CI**。CI 失败一律不查、不报。

全程**只读 GitHub**:不 mark-read、不发 PR 评论、不提交 review、不改代码、不 push。本地 git 只做只读操作(fetch + detached worktree),绝不动任何仓库的工作树/当前分支。所有产出只进通知消息和本地文件。

## 执行纪律(在 headless 环境下跑,踩过坑)

- **并行,但必须是同步 subagent**(一条消息里发起多个 `Agent`,`run_in_background: false`,阻塞等全部返回)。**绝不用后台 agent** —— 曾经并行派 8 个后台 agent,主会话跑到终点而 agent 还在后台,print 模式的后台等待上限一到就把整个会话连同未完成的 agent 全部终止,退出码还是 0,报告一份没落地。
- **分批并行**:每批 3–4 个 PR,批次内并行,批次之间落盘一次 state。兼顾并行度与"中途被杀不全盘白跑"。
- **写文件的分工**:每个 subagent 只写自己那份 HTML 报告(路径互不冲突);`state.json` 一律由主会话在每批返回后统一更新,避免并发写互相覆盖。
- **通知消息按下方模板发**,Mine 和 Others 各一条。
- 不要在 `reviews/` 里留探针文件、临时文件。
- **Bash 命令不要用 `&&` / `;` / 管道串联多条**。权限白名单按命令前缀匹配,复合命令里只要有一段不匹配整条就被拒(非交互下即静默失败)。一条命令干一件事。
- 写文件一律用 Write/Edit 工具,不要用 `cat > file` 之类的 shell 重定向(不在白名单里)。

state 文件 `state.json` 与本文件同目录:
```json
{"last_check": ISO8601, "notified_comments": [id...], "reviewed": {"repo#123": "headSha"}}
```
开始时记录本次时间 `date -u +%Y-%m-%dT%H:%M:%SZ` 备用,结束时无论有无产出都要更新 `last_check`。数组各保留最近 500 条。`reviewed` / `notified_comments` 的 key 一律带仓库名,避免跨仓库编号撞车。

---

## Mine:我自己的 PR

**对白名单里的每个仓库分别做。状态驱动,不用 `/notifications`。** 通知是事件流,读过就消失,表达不了"这条评论我还没处理"这个状态,还受已读污染。一律直接查 PR 当前状态。

### 1. 枚举我在该仓库的 open PR

```bash
gh pr list --repo {{GITHUB_ORG}}/<repo> --author @me --state open --limit 60 \
  --json number,title,isDraft,baseRefName,updatedAt
```
draft 也算(自己的 PR 上有人留言同样要知道)。按 `baseRefName` 过滤(见上)。

对每个 open PR,以下三处**都要查**——历史上漏报正是因为只看了行内 thread、忽略了 PR 级评论:

### 2. 未解决的 review thread(行内,**不设时间基线**)

```bash
gh api graphql -F repo=<repo> -F num=<n> -f query='query($repo:String!,$num:Int!){
  repository(owner:"{{GITHUB_ORG}}",name:$repo){pullRequest(number:$num){
    reviewThreads(first:100){nodes{ id isResolved isOutdated path line
      comments(first:20){nodes{ id author{login} createdAt body }}}}}}}'
```
取 `isResolved == false` 且线程里有他人评论的。**只要还没 resolve 就一直算待处理**,无论多久之前提的。

去重:key = `<repo>:<thread id>:<最后一条评论 id>`。已在 `notified_comments` 里就不再提醒;线程有**新回复**时 key 变化,会重新提醒。

### 3. PR 级评论 + review 正文(**易漏,务必查**)

行内 thread 为空**不等于**没有反馈——很多实质意见是留在 PR 级评论或 review 概述里的(尤其看板/脚本类仓库没有代码行可挂)。两处都查:

```bash
gh api repos/{{GITHUB_ORG}}/<repo>/issues/<n>/comments
gh api repos/{{GITHUB_ORG}}/<repo>/pulls/<n>/reviews
```
这类没有 resolved 状态,按时间去重:取 `createdAt > last_check`、作者 ≠ `{{GITHUB_USER}}`、id 不在 `notified_comments` 的。**机器人评论(如 `*[bot]`、CI 预览)一律忽略。** 命中后把它的 node id 记进 `notified_comments`,避免下轮重报。

### 4. 分析

读评论指向的代码和 diff 上下文,判断意见是否成立,给**最小干预**的修法;认为无需改也要说清理由。可用该仓库本地 checkout(白名单里的路径)只读参考。

---

## Others:待我 review 的 PR

**对白名单里的每个仓库分别做。**

### 1. 收集候选(三个口径求并集)

```bash
# 口径一:被点名(直接指定 + CODEOWNERS 自动请求都会落到这里)
gh pr list --repo {{GITHUB_ORG}}/<repo> --search "review-requested:@me" --state open --limit 60 \
  --json number,title,author,baseRefName,isDraft,updatedAt
# 口径二:团队被请求
gh pr list --repo {{GITHUB_ORG}}/<repo> --search "team-review-requested:{{GITHUB_TEAM}}" --state open --limit 60 \
  --json number,title,author,baseRefName,isDraft,updatedAt
```

**口径三 — CODEOWNERS 兜底**(覆盖 reviewer 被移除、团队展开失灵等口径一二漏掉的情况):取该仓库 `.github/CODEOWNERS`(`gh api repos/{{GITHUB_ORG}}/<repo>/contents/.github/CODEOWNERS -q .content`,再 `base64 -d`),挑出含 `@{{GITHUB_USER}}` 的路径前缀,对每个 open PR 用 `gh pr view <n> --repo {{GITHUB_ORG}}/<repo> --json files` 看改动文件是否落在这些前缀下。**这只是快照,每次都要重新读 CODEOWNERS,不要硬编码。**

**排除**:draft PR(`isDraft == true`,三个口径都要滤;转正后自然进入候选);自己作者的 PR(那是 Mine 线);已 approve 过且此后无新 push 的;base 不在 `{{BASE_BRANCHES}}` 里的。

### 2. 去重

对每个候选取 `headRefOid` 和 `baseRefName`(`gh pr view <n> --repo {{GITHUB_ORG}}/<repo> --json headRefOid,baseRefName`)。base 不符直接跳过。若 `reviewed["<repo>#<number>"] == headSha` → 已 review 过且无新 push,跳过。有新 push 则重新 review(报告里标出"自上次 review 后的增量")。

### 3. 拉取完整代码到临时 worktree(核心:对着全量代码审,不只看 diff)

白名单给了每个仓库的本地 checkout `<path>`。审某个 PR 前,把它的 head 拉成一个**临时 detached worktree**,这样能读到 diff 之外的完整上下文(调用方、trait 定义、并发前提),又完全不碰你的工作树和当前分支:

```bash
git -C <path> fetch origin pull/<n>/head
git -C <path> worktree add --detach {{WORKTREE_DIR}}/<repo>-<n> FETCH_HEAD
```
之后用 Read/Grep 直接读 `{{WORKTREE_DIR}}/<repo>-<n>/` 下的文件。审完移除:
```bash
git -C <path> worktree remove --force {{WORKTREE_DIR}}/<repo>-<n>
```
白名单里没有本地路径的仓库(理论上不该有),回退到 `gh pr diff` / `gh api .../contents`,并在报告头部标注"无本地树,仅基于 diff"。

### 4. 深度 review(认真做)

以 **critical Staff/Principal Engineer** 的标准做逐行 production review。判据是"这段代码上生产我敢不敢背",不是"能不能跑"。

材料:`gh pr view <n> --repo {{GITHUB_ORG}}/<repo> --json title,body,author,files,commits`、`gh pr diff <n> --repo {{GITHUB_ORG}}/<repo>`,加上临时 worktree 里的完整代码。

**必须回答三件事**:
1. **这个 PR 要解决什么问题** — 从 PR 描述、commit、代码反推真实动机;若描述与代码不符,这本身是一条 finding。
2. **它是怎么解决的** — 用自己的话讲清方案与关键设计选择,以及为什么是这个选择。
3. **你发现的所有问题** — 逐行审,不放过。

审查维度(backstop,不是打勾表;某一维度没问题就沉默,**绝不为凑数造 finding**):正确性与边界、并发与锁序、错误吞掉、回滚/异常路径、热路径性能、API 内聚与放置、对称性、footgun、兼容性、blast radius、可观测性、依赖、文档、未覆盖测试场景。**领域相关的风险从该仓库的代码与约定里自行判断**,不预设某一类项目的风险清单。

每条 finding 给:严重度(Critical / High / Medium / Low / Nit)、文件:行号、具体触发场景(什么输入/状态 → 什么错误结果)、建议修法。**先说触发场景**;若是潜在/防御性问题(当前无触发路径),直说。

### 5. 生成 HTML 报告

写到 `<本目录>/reviews/<repo>-<number>-<短sha>.html`(目录不存在则创建)。自包含单文件,不引外部资源。中文正文,代码/路径/diff 保持原文。

结构:
- **头部**:PR 标题、作者、仓库#编号、GitHub 链接、head sha、review 时间;严重度计数徽章(Critical/High/Medium/Low/Nit 各几条)
- **问题与方案**:上面的第 1、2 点,写成通顺段落,不要堆项目符号
- **Findings 列表**:按严重度降序,每条可展开,含触发场景与建议修法,点击能跳到 diff 中对应行
- **完整 diff 渲染**(重头戏):
  - 逐文件、逐行渲染,带行号(old/new 两栏),增删用绿/红底
  - **左侧留白栏做行内标注**:该行有 finding 就在旁边显示严重度色标 + 简述,点开是完整分析
  - 严重度配色统一:Critical 深红 / High 橙 / Medium 琥珀 / Low 蓝 / Nit 灰;色块之外同时用文字标签,不靠颜色单独传意
  - 长文件默认折叠、点击展开;无 finding 的纯上下文行可折叠
  - 浅色背景为主(用户偏好);表格/diff 各自 `overflow-x: auto`,页面本身不横向滚动

---

## 通知消息模板(严格照此,不要加总览、统计、元信息)

发送:`lark-cli im +messages-send --user-id {{NOTIFY_USER_ID}} --as bot --markdown '...'`

> 若 `{{NOTIFY_USER_ID}}` 为空,则跳过所有通知,只写本地文件。

**合并成一条消息发**,首行日期,然后 Mine,然后 Others。某一部分没内容就整段省略。

`--markdown` 走的是富文本(post)消息,**上限 30 KB**(文本消息才是 150 KB)。若拼出来超过 25 KB,就拆成 Mine 一条、Others 一条分别发。

### 整体结构

```
**PR-Watch 巡检 <YYYY-MM-DD>**

**Mine**

[<PR 标题>](<GitHub 链接>)
- **<发起人>** · `<文件:行号 或 "PR 评论">`:<问题,一两句说清>
  修复:<你认为应该怎么改,最小干预;若认为不需要改,说明理由>
- **<发起人>** · ...

[<下一个 PR 标题>](<GitHub 链接>)
...
```

**以一条未解决反馈为单位**(行内 thread 或 PR 级评论);作者只写**该反馈的发起人**,跟帖者不列进作者,但跟帖里的实质内容可以并入问题描述。同一处代码有多条独立线程就分别列,不要合并。

**不要**写 CI 状态、thread 计数、积压天数、"全绿"之类的元信息。

Mine 之后紧接 Others:

```
**Others**

[<PR 标题> — <作者>](<GitHub 链接>)
[review 报告]({{REPORT_BASE_URL}}/<报告文件名>.html)

[<下一个 PR 标题> — <作者>](<GitHub 链接>)
...
```

每个 PR 两行:第一行"标题 — 作者"作为 GitHub 链接,第二行报告链接。**不要**写结论、严重度计数、findings 摘要 —— 这些都在 HTML 里。

### 链接与转义规则

- 富文本**只把 http/https 渲染成可点**,`file://` 和裸本地路径都不可点。报告一律用 `{{REPORT_BASE_URL}}/<文件名>.html`,该地址由常驻的报告服务 LaunchAgent 提供(只绑本机,根目录 `reviews/`)。
- **PR 标题一律放在 markdown 链接的文本里**,不要裸放进 `**粗体**`。标题里常见的 `_` `*`(如 `timed_*`)在 `**...**` 内会被当成斜体/粗体标记,把整行格式吃掉;放在链接文本里则不会被解析,也不需要转义。
- 万一标题必须出现在链接之外,把 `_` 和 `*` 转义成 `\_` `\*`。

---

## 收尾

- 两条线都无产出 → 静默,不发任何通知
- 移除本轮建立的所有临时 worktree(`git -C <path> worktree remove --force ...`)
- 更新 state.json:`last_check`、`notified_comments`、`reviewed`
