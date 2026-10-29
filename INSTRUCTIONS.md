# PR-Shepherd 巡检指令

> 本文件是**模板**。`{{...}}` 占位符由 `run.sh` 在运行时用 `config.env` 的值渲染成
> `.INSTRUCTIONS.local.md` 后再喂给 Claude。仓库里提交的这份不含任何组织名/用户名/路径。

两条独立流水线,每次巡检都跑:

- **Mine** — 我(@{{GITHUB_USER}})自己的 open PR:未解决的反馈(含 bot)→ 逐条分诊 + 本地准备补丁/回复草稿;已合并的 PR → 复盘
- **Others** — 待我 review 的 open PR(被点名 + CODEOWNERS 命中):深度 production review → 本地 HTML 报告

## 扫描范围:白名单驱动,逐仓库枚举

**只扫描下面白名单里的仓库,一个都不能少、也不要扩到白名单之外。** 对**每一个**仓库都要独立跑完 Mine 和 Others 两条线——不要因为某个仓库"通常没动静"就跳过,也不要只盯着其中一两个仓库(这正是以前漏掉整仓库 PR 的根因)。

白名单(仓库 → 本地 checkout 路径):

{{REPO_WHITELIST_TABLE}}

所有仓库同属组织 `{{GITHUB_ORG}}`,完整名即 `{{GITHUB_ORG}}/<repo>`。

**只关注 base 分支为 `{{BASE_BRANCHES}}` 的 PR**。两条线枚举出候选后,都要按 `baseRefName` 过滤:落在这几类里才保留(`release*` 指 release 开头),其它 base(如 `testnet_all`、feature 分支)一律跳过。

**不关注 CI**。CI 失败一律不查、不报。

全程**只读 GitHub**:不 mark-read、不发 PR 评论、不 resolve thread、不提交 review、不 push。本地 git 只在 `{{WORKTREE_DIR}}` 下的 worktree 里写(Mine 线在 `shepherd/*` 本地分支上提交补丁),绝不动任何仓库原有的工作树/当前分支/其它分支。所有产出只进通知消息和本地文件。

## 执行纪律(在 headless 环境下跑,踩过坑)

- **并行,但必须是同步 subagent**(一条消息里发起多个 `Agent`,`run_in_background: false`,阻塞等全部返回)。**绝不用后台 agent** —— 曾经并行派 8 个后台 agent,主会话跑到终点而 agent 还在后台,print 模式的后台等待上限一到就把整个会话连同未完成的 agent 全部终止,退出码还是 0,报告一份没落地。
- **分批并行**:每批 3–4 个 PR,批次内并行,批次之间落盘一次 state。兼顾并行度与"中途被杀不全盘白跑"。
- **写文件的分工**:每个 subagent 只写自己那份 review 报告 / 分诊页 / 复盘页,只动自己那个 worktree(路径互不冲突);`state.json` 一律由主会话在每批返回后统一更新,避免并发写互相覆盖。
- **会话 id**:启动 prompt 里给出了本轮会话 id。派 Mine 分诊和复盘的 subagent 时把它原样写进 subagent 的 prompt,分诊页和复盘页要记录它——之后的交互会话靠它找回本轮的完整记录。
- **通知消息按下方模板发**,Mine 和 Others 各一条。
- 不要在 `reviews/` 里留探针文件、临时文件。
- **Bash 命令不要用 `&&` / `;` / 管道串联多条**。权限白名单按命令前缀匹配,复合命令里只要有一段不匹配整条就被拒(非交互下即静默失败)。一条命令干一件事。
- 写文件一律用 Write/Edit 工具,不要用 `cat > file` 之类的 shell 重定向(不在白名单里)。

state 文件 `state.json` 与本文件同目录:
```json
{"last_check": ISO8601, "last_run": "<RUN_ID>", "notified_comments": [id...], "reviewed": {"repo#123": "headSha"},
 "mine_open": ["repo#123"], "triage_next": {"repo#123": 6}, "retro_done": ["repo#123"]}
```
开始时记录本次时间 `date -u +%Y-%m-%dT%H:%M:%SZ` 备用。结束时**无论有无产出、无论是否有仓库访问失败**,都要把 `last_check` 更新为开始时间、`last_run` 写成 `{{RUN_ID}}`(runner 靠它判断本轮是否跑完)。某个仓库访问失败就跳过它并在通知里说明,不要自创"不推进 last_check"之类的规则。数组各保留最近 500 条。`reviewed` / `notified_comments` 的 key 一律带仓库名,避免跨仓库编号撞车。

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
      comments(last:20){nodes{ id url author{login} createdAt body }}}}}}}'
```
取 `isResolved == false` 且线程里有他人评论的。**只要还没 resolve 就一直算待处理**,无论多久之前提的。例外:线程**最后一条评论的作者是 `{{GITHUB_USER}}`** 时跳过——我已回应,球在对方;对方再回复后最后一条变了,自然重新进入。

去重:key = `<repo>:<thread id>:<最后一条评论 id>`。已在 `notified_comments` 里就不再提醒;线程有**新回复**时 key 变化,会重新提醒。

### 3. PR 级评论 + review 正文(**易漏,务必查**)

行内 thread 为空**不等于**没有反馈——很多实质意见是留在 PR 级评论或 review 概述里的(尤其看板/脚本类仓库没有代码行可挂)。两处都查:

```bash
gh api repos/{{GITHUB_ORG}}/<repo>/issues/<n>/comments
gh api repos/{{GITHUB_ORG}}/<repo>/pulls/<n>/reviews
```
这类没有 resolved 状态,按时间去重:取 `createdAt > last_check`、作者 ≠ `{{GITHUB_USER}}`、id 不在 `notified_comments` 的。命中后把它的 node id 记进 `notified_comments`,避免下轮重报。

**bot 的评论同样纳入**(claude[bot]、mega-maxwell[bot]、Secure Analysis 等 review agent,以及代人发 agent 评论的账号)——它们提的问题和人提的一样要处理。只跳过纯噪音:CI 预览、部署链接、"no findings" 的空报告。

本轮没有新反馈的 PR,后面 4–6 步全部跳过。

### 4. 准备上下文

1. **PR head worktree**(和 Others 线同一套 fetch,但要留意它可能已存在):
   - `headRefOid` 取前 7 位记为 `<head7>`,本地分支名 `shepherd/<repo>-<n>-<head7>`,worktree 路径 `{{WORKTREE_DIR}}/<repo>-<n>`。
   - worktree 路径已存在(上轮残留)→ 先 `git -C <path> worktree remove --force` 再建。
   - `git -C <path> fetch origin pull/<n>/head`;分支已存在(同一 head 上轮已备过补丁)→ `git -C <path> worktree add <wt> shepherd/<repo>-<n>-<head7>`,在其上继续提交;不存在 → `git -C <path> worktree add -b shepherd/<repo>-<n>-<head7> <wt> FETCH_HEAD`。
   - 用 `git -C <path> worktree list --porcelain` 查我自己有没有检出该 PR 分支(`headRefName`)的开发 worktree。有就**完全不碰它**,只在分诊页头部注明它的路径,以及它相对远端 head 有几个未 push 的 commit(`git -C <它的路径> rev-list --count FETCH_HEAD..HEAD`),提醒补丁是基于远端 head 做的。
2. **决策上下文**:PR body;`{{PR_NOTES_DIR}}/<repo>/<n>.md`(PR note)和 `<n>.design.md`(设计稿,若有)——里面是这个 PR 的假设、non-goals 和关键决策。

### 5. 逐条分诊

先去重:不同 reviewer、不同轮次对同一处同一问题的意见合并为一条,注明全部来源。然后对每条:

- **触发场景**:什么输入/状态 → 什么错误结果;结合部署模型判断生产能否触发,给证据(代码位置、调用路径)。是潜在/防御性问题就直说。
- **分类**(五选一):真 bug / 指出了更好的设计 / 理论问题 / 与 PR note 的决策冲突(引用那条决策)/ 风格或重复或已修。
- **处理建议**:修 / 回复 / 需我定。判断看意见本身是否成立:成立且有更好的设计,就照更好的设计提,哪怕改动大;否则最小修法。
- **修**:在 worktree 的 `shepherd/...` 分支上每条一个 commit(message 写清对应哪条反馈),然后按该仓库的方式验证——Rust 仓库 `cargo check` + 相关 crate 的 `cargo nextest run`(都用 `--manifest-path <wt>/Cargo.toml`);是 bug 就加一个"去掉修复会失败"的测试并实际验证它会失败。验证不过就不提交,写明卡在哪。
- **指出了更好的设计**:不备补丁,写一段新旧设计对比(改什么、代价、收益),交给我定。
- **回复**:英文回复草稿,直接可贴;"与决策冲突"的回复要讲清那条决策和理由。

### 6. 写分诊页

写到 `<本目录>/reviews/triage/<repo>-<n>.html`。分诊页也是交接文档:之后我会在新的交互会话里只凭它说"第 N 条照方案改",那个会话没有本轮上下文,所以**每条都要自成一体**——不读本轮记录、不翻其它条目,也能直接动手。

- **首次创建**:照 `<本目录>/templates/triage.html` 的骨架写——`<head>` 样式、末尾 `<script>`、页面结构(含 `<div id="rounds">` 容器)原样保留,示例内容和注释换成真实内容。头部写 PR 链接、补丁分支、开发 worktree 提醒(若有)、PR note 是否存在。
- **之后每轮追加,不覆盖**:用 Edit 把新一轮 `<section class="round">` 整段插到 `<div id="rounds">` 那行之后,最新一轮在最上面;头部信息有变(新 head 的补丁分支、开发 worktree 状态)就同步改。
- **段头**:日期时间、本轮会话 id(`data-session` 属性和正文都写)、远端 head、修 / 回复 / 需你定 各几条。
- **条目**:编号从 `triage_next["<repo>#<n>"]`(默认 1)起连续往下编,写完更新它。每条一个 `<article class="item" id="item-<编号>">`,属性:
  - `data-action`:`fix` / `reply` / `decide`,对应 修 / 回复 / 需你定
  - `data-head`:分析所基于的远端 head 短 sha
  - `data-commit`:补丁 commit 短 sha(仅 `fix`)
  - `data-source`:原评论的 GitHub 链接(GraphQL 的 `url`、REST 的 `html_url`);合并了多条意见时取第一条,其余写进来源行
- **条目内容**:标题(一句话问题 + 动作徽章)、来源行(作者 · 文件:行号 或 PR 评论)、分类 / 生产可触发、分析(触发场景和证据,落到文件:行号)、补丁(仅修:commit + 改了什么 + 验证结果)、新旧设计对比(仅"指出了更好的设计":改什么、代价、收益,具体到能照着实现)、回复草稿(仅回复)。
- 回复草稿、拟贴进 PR body 的段落、diff 一律放 `<pre>`,内容是可直接粘贴的原文(GitHub markdown 原样保留),只做 HTML 转义(`&` `<` `>`);页面脚本会给每个 `<pre>` 加复制按钮。正文里的代码/路径用 `<code>`,同样转义。

---

## 复盘:我已合并的 PR

`mine_open` 记录上一轮我所有 open 的 PR。本轮枚举后,对其中不再 open、已 merged、且不在 `retro_done` 里的,各做一次复盘(subagent):

1. 拉全部 review thread、PR 级评论、review 正文和 commit 历史。找出**导致了真实修改**的反馈:反馈之后有 commit 改了它指向的代码,或回复里确认已修。
2. 对每条:它抓到了什么问题;按问题性质判断,开发时的每轮审查(只查正确性、回复与 diff 是否一致、与 PR note 是否一致、是否超出需求)或提 PR 前的结构审查(thermo-nuclear code quality review)为什么没抓到;还是属于设计阶段就该定下来的事。
3. 归纳教训,写成**候选**改进:可以加进个人规则文件的一条规则,或对某个审查提示词的调整。只写建议,**不改任何规则/提示词文件**。
4. 写到 `<本目录>/reviews/triage/retro-<repo>-<n>.html`,照 `<本目录>/templates/retro.html` 的骨架写,头部记本轮会话 id。每条导致修改的反馈一个 `id="item-<k>"`,带 `data-commit`(对应修改)和 `data-source`(原评论链接);每条候选改进一个 `id="c-<k>"`,`data-kind` 为 `rule` / `prompt`,拟改原文放 `<pre>`(规则同分诊页)。记入 `retro_done`;删除该 PR 的 `shepherd/<repo>-<n>-*` 本地分支(`git -C <path> branch -D`)。

结束时把本轮所有 open 的 Mine PR 写回 `mine_open`。

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
**PR-Shepherd 巡检 <YYYY-MM-DD>**

**Mine**

[<PR 标题>](<GitHub 链接>)
新反馈 <N> 条:已备补丁 <a>、建议回复 <b>、需你定 <c> — [分诊]({{REPORT_BASE_URL}}/triage/<repo>-<n>.html)

[<下一个 PR 标题>](<GitHub 链接>)
...

**复盘**

[<已合并 PR 标题>](<GitHub 链接>) — <候选改进条数> 条候选改进 — [复盘]({{REPORT_BASE_URL}}/triage/retro-<repo>-<n>.html)
```

每个 PR 一行计数加分诊链接,细节全在分诊页里。

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

- 两条线和复盘都无产出 → 静默,不发任何通知
- 移除本轮建立的所有临时 worktree(`git -C <path> worktree remove --force ...`);`shepherd/*` 分支保留(补丁在上面,等我挑选)
- 更新 state.json:`last_check`、`last_run`、`notified_comments`、`reviewed`、`mine_open`、`triage_next`、`retro_done`
