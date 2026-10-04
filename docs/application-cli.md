# 应用命令行

这是给本机程序用的独立通知命令，包名是 `notify_me_app`。aiusage 用它排队、查询和取消自己的通知。

它不安装 Grok、Codex 或 Cursor 插件，也不写这些 Agent 的规则、MCP 或技能。插件仍走各自的绑定文件。应用自己的状态在单独的配置目录里：`state.sqlite3` 和一行 `BARK_URL=` 的 `.env`。

需要 Python 3.9 或更高。这台机器用系统自带的 Python 3.9.6 和标准库测过，不安装第三方包。

本次交付不往真实的 Bark 发通知，也不把程序装进真实的用户目录。

## 怎么调用

每条命令往标准输出写一个 JSON 对象，并换行。成功时进程退出码是 0，`ok` 为 true。`ok` 只表示命令被接住了，不表示手机已经收到。

协议版本是 1。输出含换行在内不超过 65536 字节。错误只有 `error.code`，没有说明文字，也不打印推送地址、设备密钥或绑定内容。

任何命令都可以带 `--protocol-version`。版本不对时返回 `protocol_mismatch`，并且不读不写文件。值不是整数时返回 `invalid_arguments`，同样不碰文件。

从源码运行时，`version --json` 里的 `source_commit` 和 `build_digest` 都是 `dev`。打好的 zipapp 会写上真实的提交号和摘要。

## 命令

`version --json` 只打印编进程序里的常量。它不解析路径，不建目录，不读绑定，也不打开数据库。

`status` 只读。未配置时 `ok` 为 true，`status` 为 `configuration_missing`。已有绑定但还没有数据库时，`status` 为 `not_initialized`，不创建数据库。schema 8 可以读，`migration_required` 为 true，`writable` 为 false。schema 9 可写。不认识的 schema 返回 `state_schema_unsupported`，不删除数据库。状态里不包含 Agent 规则或插件健康。只有已经绑定时才出现主机名，不出现设备密钥或完整推送地址。

`push` 仍接受原来的 `--source`、`--event-id`、`--priority`、`--title`、`--body`，并可以加 `--enqueue-only`、`--created-at`、`--expires-at`。先把事件写进数据库，再谈发送。同一来源和同一事件号再推一次，不延长有效期，也不改内容。内容不同返回 `event_conflict`，数据库和网络都不变。未配置时 `ok` 为 false，`error.code` 为 `configuration_missing`，不建目录。

`push-status` 按来源和事件号只读查询。没有这条记录时 `status` 为 `not_found`，不写墓碑。这不是取消成功。配置目录不存在是 `configuration_missing`。目录在但数据库不在是 `not_initialized`。

`push-cancel` 取消一条事件。数据库已经在时，不需要 Bark 绑定。目录不存在则 `configuration_missing`，不建目录。schema 8 返回 `schema_upgrade_required`，不写。

`push-drain` 必须带 `--source`。可以指定一个事件号，也可以用 `--max-items` 和 `--budget-ms` 批量发送。缺来源时即使同时带了 `--force` 也返回 `source_required`。来源和 `--force` 同时出现则返回 `invalid_arguments`，不访问网络和数据库。还没到期的事件保持原状态，不会为了清空而提前发送。

`install`、`upgrade`、`migrate-binding`、`recover` 只替换应用自己的启动程序，并在明确指定的配置目录上迁移。见下文。

## 状态和租约

对外能看见的状态是 `queued`、`sending`、`accepted`、`failed`、`expired`、`cancelled`。旧库把可重试失败、取消和过期都塞进 `failed`，读出来时会还原成上面的真实状态。重复推送时，`previous_status` 用的也是还原后的状态。

一次发送会占一个租约，默认 60 秒。查询结果里的 `lease_until` 是这次租约的到期时刻，没有租约就是 null。`active_lease` 只有在租约号还在、并且 `lease_until` 晚于现在时才是 true。历史记录里的不确定标记不会让它一直显示成“正在发送”。

`remote_withdrawn` 永远是 false。请求一旦离开这台机器，这个协议就不能把它从手机上撤回。

`ttl_elapsed` 只在排队或发送中、有效期已到、并且没有活租约时为 true。查询不会把库里的 `sending` 改写成 `expired` 或 `cancelled`，也不会把有效期往后推。协调器看 `ttl_elapsed`、`active_lease`、`lease_until`、`outcome_uncertain` 和 `retryable`。

`retryable` 只在排队或发送中、没有活租约、没有请求取消、并且有效期还没到时为 true。

`outcome_uncertain` 表示以前可能有一次发送已经到了手机，但本地没来得及记下。接受之后这个标记仍然留着，这时 `service_confirmed` 为 true，`uncertainty_applies_to` 为 `prior_attempt`，`retryable` 为 false。当前这次是服务确认过的，标记只说明更早的一次可能重复，不能用来永远挡住恢复。还没接受时，`uncertainty_applies_to` 为 `current_delivery`。

有效期在第一次入队时冻结，取业务时间和该优先级允许时间里更早的那个。以后的重复推送和发送都不再续期。`created-at` 最多比现在早到保留期，最多比现在晚 120 秒。

默认优先级效果是：P0 关键、alarm、音量 8、有效期 900 秒；P1 时效、telegraph、7200 秒；P2 主动、glass、14400 秒。P3 没有默认效果，必须另给。这些是程序默认值，不是某一台机器上的现用配置。用户原来合法的效果会保留。

失败后的下次尝试由这里计算：按效果级别先等 30、120 或 300 秒，然后翻倍，最长 3600 秒。还没到下次时间就不会发网络请求。一批发送优先 P0，条数默认 1、最多 32，时间默认 15 秒、最多 60 秒。时间用完就停。

同一来源加事件号决定身份。aiusage 的来源名是 `aiusage`。同一对永远得到同一个通知号。不能保证对方只收一次：租约过期后会用同一个号再发，手机上可能看到两条。程序不会另造一个号来躲开这个不确定窗口，也不会在接受后擦掉不确定标记。

## 取消

还在排队、并且没有活租约时，取消会在同一次写入里变成 `cancelled`。

租约还活着时，取消只记下 `cancel_requested`，返回 `not_pending`，`reason` 为 `in_flight`，状态保持 `sending`。这条发送失败后也不会再排期。若这次发送最终被接受，接受优先，取消来不及，`reason` 为 `accepted`。

租约已经到期的 `sending`，再次 `push-cancel` 会在同一次写入里变成 `cancelled`，`previous_status` 为 `sending`，`reason` 为 `lease_expired`，并保留远端结果不确定（`outcome_uncertain` 为 true，`error_code` 为 `delivery_uncertain`）。不需要再跑 `push-drain`。活租约仍是 `in_flight`。已经接受的不会被降级。过期的旧租约号不能把已取消或已接受的行改回去。

查询保持只读。租约过期或有效期已过时，它可以显示当前租约和有效期事实，但不能宣称远端通知已经撤回，也不能自己把行写成取消。

对一个还不存在的事件号做取消，会写一条没有正文指纹的墓碑，返回 `cancelled`，`previous_status` 为 `not_found`，`reason` 为 `tombstone`。之后任何正文再推这个号，都按已取消处理。查询里的 `not_found` 不能当成这次取消成功。

墓碑是终态，不占用活跃名额。活跃队列满了，只要总行数和每个来源的行数还没满，就允许写下墓碑。行数真的满了返回 `queue_full`。这是错误，不是取消成功，也不会留下墓碑。活跃中的 P0 和还年轻的墓碑都不会为了腾位置被悄悄删掉。终态记录保留 30 天，上限是每个来源 200 条活跃、总共 1000 条活跃、每个来源 2000 行、总共 10000 行。

## 安装和恢复

`install` 和 `upgrade` 必须给出 `--launcher`。`--config-dir` 可以不给。不给，或者指向一个还不存在的目录时，只安装启动程序，不创建配置目录。

配置目录已经存在并且权限正确时：schema 8 先备份再迁到 schema 9；没有数据库就新建 schema 9；已经是 schema 9 就跳过迁移。已有的 `.env` 和 scope salt 不动。第二次安装是幂等的。

换启动程序之前，先把恢复日记写稳。日记里有阶段 `replacing`、旧文件和新文件的 sha256，以及迁移时刻。然后才替换启动程序，最后把日记改成 `replaced`。没有配置目录、以及 schema 9 不需要迁移时，也写这份日记。崩溃点因此覆盖替换提交的前一刻和后一刻。

`recover` 按日记里的摘要核对启动程序。替换还没发生，就保持旧程序。替换已经发生，就从 `.previous` 把旧程序请回来。摘要对不上就停止，不猜。

新的应用写操作和恢复共用 `install.lock`。推送、取消、发送收尾和恢复不能同时改这份库。这把锁管不到旧 Agent：旧程序直接写 SQLite，不拿这把锁。回滚 schema 8 时，恢复会先独占这份库，在同一把锁里核对有没有新的接受、取消，以及 Agent 的 `notifications` 等表是否还和备份一致，然后在放开锁之前用 SQLite 自己的备份接口把内容拷回去。不能先看一眼再截断文件。

若这期间已经有新的接受或取消，就留下 schema 9，确认库还能读之后才删掉旧备份。`database_restored` 为 false，`delivery_preserved` 为 true，`database_rollback` 为 `preserved`。若 Agent 表已经和备份不同，或者当时拿不到这把 SQLite 锁，就只换回旧启动程序，保留 schema 9，`database_rollback` 为 `blocked`。这时不能说数据库已经回滚成功。这次不改 Agent。schema 9 对旧程序来说过新，旧程序会停在 `state_schema_too_new`，不会清库。

恢复自己若中途停下，日记和备份都还在，下一轮可以再试。库若已经不是一份可读的数据库，就报错并留下日记和备份，不会假装恢复成功。启动程序先写到临时文件再替换，避免截断后留下半个程序。

已经有投递或任何新行被写过之后，恢复只换回旧启动程序，不恢复旧数据库。失败发生在第一次投递之前，并且独占窗口里 Agent 表仍和备份一致，才把 schema 8 备份拷回去。

已有恢复日记时，安装会在拿到锁之后再确认一次。别人还没做完的安装不会被盖掉，返回 `recovery_required`。

`migrate-binding` 必须同时给出来源（`codex`、`grok` 或 `cursor`）和明确的绑定文件。它只会读那一个文件。已有应用绑定不会被覆盖，除非带 `--replace-binding`。只有插件、没有应用配置的机器，安装启动程序之后仍然是 `configuration_missing`。

配置目录是 `0700`，数据库、`.env` 和日记是 `0600`，启动程序是 `0700`。`/tmp`、`/var`、`/private/tmp`、`/private/var` 这些临时目录的上级可以是系统目录。数据库、`.env` 和启动程序本身不能是符号链接。

Bark 设备密钥取推送 URL 的最后一段，这样带服务路径前缀的插件绑定也能迁过来。只有一段的地址和原来的解析一致。明文 HTTP 只允许 localhost、127.0.0.1 和 ::1。不跟随重定向。成功是 HTTP 2xx 并且 JSON 的 `code` 为 200。

## 构建

在仓库根目录：

```bash
PYTHONPATH=apps/notify-me/src python3 apps/notify-me/build/build_zipapp.py --output /tmp/notifyme-191-notify-me
```

zipapp 以 `#!/usr/bin/env python3` 开头。摘要是 `sha256(提交号 + 换行 + 按路径排序的源文件清单)`。清单不含生成的 `build_info.py`。`apps/notify-me/src/notify_me_app` 有未提交改动时，提交号后面加 `-dirty`。不要把打好的 zipapp 提交进仓库，否则摘要会在计算之后又变掉。

安装程序在替换前会用当前解释器对这份 zipapp 做一次 `version --json` 自检，并确认它没有创建配置目录。

## 和旧用法的差异

可写的库是 schema 9。schema 8 在明确升级之前只读。旧程序看到版本大于 8 会返回 `state_schema_too_new`，不会清库。因此投递之后不能把库退回 schema 8。

时间用整数秒。未配置时，`status` 的 `ok` 为 true，`push` 的 `ok` 为 false。

不能保证恰好一次，也没有必达保证。有界重试可能把同一通知号再送出去，手机上可能出现重复。接受之后的 `outcome_uncertain` 是历史标记。

查询 `not_found` 不是取消成功。取消未知事件会写墓碑。墓碑不受活跃名额限制，但受总行数限制。

旧的 aiusage 若在 `push-drain` 上不带来源，现在会得到 `source_required`。这条旧的重发路径要两边一起改之后才能再用。查询和取消不依赖 `push-drain`：租约到期后的取消自己会落到终态。

`--force` 不能绕过退避。队列满时返回 `queue_full`，不删还活着的 P0。标准输出的 65536 字节把最后的换行算进去。不认识的错误码会变成 `legacy_error_redacted` 或 `internal_error`，不会原样打出来。

源码里的版本是 `dev` / `dev`。安装叶目录是 `0700`，但允许 `/tmp` 和 `/var` 作为上级。Bark 密钥是路径的最后一段。
