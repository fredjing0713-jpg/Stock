# copybot：Discord 信号跟单 → IBKR 自动下单

把信号群里的「组合收益 / 仓位变动 / 持仓明细」消息，按同样的仓位比例在你的 IBKR
**保证金账户**里自动下单，并且自动管理融资额度、盘前挂单、券商端止损和降杠杆。
每一步都会播报到你的 Discord 频道。

> **风险提示**：本项目不构成投资建议。杠杆交易可能亏损超过本金。务必按「[上线步骤](#6-上线步骤)」先用模拟盘验证。

完整的模块设计见 [`docs/DESIGN.md`](docs/DESIGN.md)。

---

## 1. 它做什么

```
他的信号频道 (别人的 Discord 服务器)
    │  你用自己的账号「关注」或「转发」(见第 3 节)
    ▼
你的服务器 #信号 频道
    │  机器人只读取 allowed_signal_author_ids 里的人 / webhook 发的消息
    ▼
机器人解析: 仓位变动 + 持仓明细 + 时间戳
    │  计算 G (他的「100% 仓位」= 多少美元) → 每只股票的目标股数 → 订单
    ▼
IBKR (docker 里的 IB Gateway, 保证金账户) 下单 + 挂止损
    │
    ▼
你的服务器 #播报 频道: 下单 / 成交 / 跳过原因 / 止损 / 报警 (重要报警会 @你)
```

能识别的信号格式（纯文本、Discord embed、Discord 原生「转发」都可以；全角/半角冒号、
`->`、多余空格、加粗等 markdown 都能容忍）：

```
组合收益
10月收益：3.4%
总收益：168.3%
仓位变动
建仓 WDC | 仓位：0.0% → 100.0% | 成交价：422.6
清仓 BE | 仓位：50.0% → 0.0% | 成交价：无
清仓 ARM | 仓位：50.0% → 0.0% | 成交价：无
持仓明细
WDC | 仓位：100.0% | 成本：422.6 | 盈亏：+0.1%
(2026-10-02 09:50:59 美东)
```

不是这种格式的消息会被**静默忽略**（不会有任何播报）。

---

## 2. 交易规则

下面提到的参数都在 `config.yaml` 里，括号里是配置项名字。所有时间都是**美东时间**
（北京时间 = 美东 + 12 小时（夏令时）/ + 13 小时（冬令时））。

### 2.1 「100% 仓位」是多少钱：G

他说「WDC 仓位 25%」，我们就买 **25% × G** 美元的 WDC。G 在**每次下单前**用最新的
净资产和保证金率重新计算（这就是「动态调整融资」）：

```
w_i      = 他每只股票的仓位 (来自持仓明细; 没有持仓明细时 = 上次已知持仓 + 本次变动)
u        = max(0, 1 - Σw_i)                       他没用满的部分, 按默认保证金率估算
M        = Σ w_i × 维持保证金率_i + u × 默认维持保证金率      (混合维持保证金率)
N        = Σ w_i × 初始保证金率_i + u × 默认初始保证金率      (混合初始保证金率)

G_剩余流动性 = (1 - 25%) × 净资产 / M      满仓后剩余流动性仍 ≥ 净资产 × 25%   (trading.reserve_ratio)
G_初始保证金 = (1 - 2%)  × 净资产 / N      IBKR 允许开这么多仓, 并留 2% 余量   (trading.init_margin_buffer)
G_杠杆上限   = 1.9 × 净资产 / max(1, Σw_i)  总持仓市值不超过净资产 1.9 倍       (trading.max_gross_leverage)

G = min(G_剩余流动性, G_初始保证金, G_杠杆上限)
```

每只股票的保证金率来源（优先级从高到低）：`trading.margin_overrides` 手动指定 →
IBKR 实时 whatIf 试算（缓存 30 分钟，`trading.margin_cache_seconds`）→ 默认值
（初始 50%、维持 30%，`trading.default_init_margin` / `default_maint_margin`）。
结果会被限制在 `trading.min_margin_rate`（10%）到 100% 之间。高波动股票的保证金率高，G 会自动变小。

**例子**：净资产 10 万美元，他持有 WDC 50% + ARM 50%。

| 股票 | 他的仓位 | 初始保证金率 | 维持保证金率 |
|---|---|---|---|
| WDC | 50% | 35% | 30% |
| ARM | 50% | 45% | 40% |
| 混合 | 100% | N = 40% | M = 35% |

- 保留 25% 剩余流动性：G_剩余流动性 = 7.5 万 / 0.35 ≈ **21.4 万**
- 初始保证金：G_初始保证金 = 9.8 万 / 0.40 = **24.5 万**
- 杠杆上限：G_杠杆上限 = 1.9 × 10 万 = **19 万**
- **G = 19 万**（这次是杠杆上限起作用）。他的 25% 仓位 = 4.75 万美元；
  WDC 50% = 9.5 万美元，按 422.6 计算 = 224 股。
- 满仓 19 万时维持保证金 ≈ 19 万 × 35% = 6.65 万，剩余流动性 ≈ 3.35 万（33.5%），高于 25% 的底线。

注意：
- G 只在**执行他的某条变动时**用于那只股票。净资产涨跌导致 G 变化时，已有持仓**不会**自动调整
  （避免频繁交易）；想整体对齐用 `!sync`。
- 他没满仓时（Σw < 100%），空着的部分按默认保证金率估算，保证他以后加满仓时我们也放得下。

### 2.2 每条仓位变动怎么执行

方向只看数字（新仓位 > 旧仓位 = 买），不看「建仓/加仓/减仓/清仓」这些字。

| 他的变动 | 我们的目标股数 | 下单 |
|---|---|---|
| 建仓 / 加仓（例 0% → 100%） | ⌊新仓位 × G ÷ 买入参考价⌋ | 买入「目标 − 当前持有」；已经 ≥ 目标就不动，**绝不卖** |
| 减仓（例 100% → 50%） | ⌊新仓位 × G ÷ 卖出参考价⌋ | 卖出「当前持有 − 目标」；已经 ≤ 目标就不动，**绝不买** |
| 清仓（→ 0%） | 0 | 全部卖出（不需要价格） |

- 买入参考价 = 卖一价 → 最新价 → 昨收；卖出参考价 = 买一价 → 最新价 → 昨收。
- 例：G = 19 万，「建仓 WDC 0% → 100%」，卖一价 423 → 目标 449 股，买 449 股。
  之后「减仓 WDC 100% → 50%」，买一价 430 → 目标 220 股，卖 229 股。
- 按**目标仓位**计算，而不是「他卖了一半我也卖一半」：重复执行不会重复买卖；盘前只成交了一半
  也能在开盘时补对；不依赖信号时的持仓。
- 永远不做空；同一条信号里同一只股票出现多次以最后一行为准；`trading.ignore_symbols`
  里的股票永远不碰。

### 2.3 交易时段（NYSE 交易日历，含节假日和提前收盘日）

| 时段 | 美东时间 | 买入 | 卖出 |
|---|---|---|---|
| 常规 | 09:30–16:00（提前收盘日到 13:00） | 市价 | 市价 |
| 盘前 | 04:00–09:30 | 先下目标增量的 **50%** 限价单（卖一价 × 1.005），其余开盘后 5 秒市价补齐 | **全部**下限价单（买一价 × 0.995），开盘时未成交部分撤单改市价 |
| 盘后 | 16:00–20:00（提前收盘日 13:00–17:00） | 同盘前，其余在**下一个交易日**开盘补齐 | 同盘前 |
| 休市 | 夜间 20:00–04:00、周末、节假日 | 存为待执行，下一个开盘执行 | 同左 |

- 常规时段的顺序：撤掉这些股票上机器人未成交的单 → **先卖**，等成交（最多 60 秒，
  `trading.fill_timeout_seconds`）→ 检查剩余流动性 → **再买** → 同步止损。
- 开盘时（`trading.open_delay_seconds` 秒后）：撤掉盘前没成交完的限价单，用**最新价格和最新 G**
  按常规时段规则把整条指令执行一遍——盘前已成交的部分会被算进去，只补差额。
- 盘前限价的比例和缓冲：`trading.extended_hours_buy_fraction`（0.5）、
  `extended_hours_sell_fraction`（1.0）、`ext_limit_buffer_pct`（0.5%）。
- 盘前 / 盘后换仓（同时有卖和买）：先挂卖单并等成交（最多 60 秒），卖出释放的保证金再用来买那 50%。
- 拿不到任何价格（没有行情、他的成交价是「无」）时，不下盘前单，全部等开盘。
- 同一只股票有更新的信号时，会替换掉它还没执行的待执行指令。
- 收到信号时 IB Gateway 正好断线：指令先存起来，重连后（常规时段）或下一个开盘执行。
- `!status` 可以看到所有待执行指令。

### 2.4 买入前实时检查剩余流动性

每批买单执行前，先估算「卖单成交后」的账户，再把这一批**所有买单按同一比例缩小**，保证买完后：
剩余流动性 ≥ 净资产 × 25%，可用资金 ≥ 净资产 × 2%，总持仓市值 ≤ 净资产 × 1.9。
缩小了会在播报里说明。

### 2.5 止损（券商端）

- 每个多头持仓一张 **GTC 止损单（STP 卖出）**，止损价 = 持仓均价 × (1 − `trading.stop_loss_pct`%)。
  按你的要求设为 **10%**（`config.example.yaml` 和程序默认值都是 10%）。
  例：均价 422.6，10% 止损 → 380.34。
- 止损单挂在 IBKR 服务器上，机器人或 VPS 宕机也有效。**只在常规时段触发**（盘前盘后不触发）。
- 止损按**持仓均价**，不是移动止损；加仓后均价变化，止损价跟着变。
- 每 30 秒核对一次（`trading.stop_sync_interval_seconds`），成交后立即核对；数量 = 持仓 − 机器人正在卖的股数。
  机器人卖出某只股票前会先撤掉它的止损单，下一次核对再按新持仓重新挂。
- 止损成交后：这只股票标记为「已止损」，删除它的待执行指令，播报并 @你。
  之后他对这只股票**建仓/加仓** → 重新跟入（按他的新仓位买满）；他**减仓/清仓** → 清除标记。
  如果那条建仓/加仓信号因为超时 / 暂停没有交易，标记也会清除，之后用 `!sync` 就能跟回。
- 止损触发后，这只股票还没成交的盘前 / 盘后限价单会被自动撤销（不会刚止损又被买回）。

### 2.6 动态降杠杆

常规时段每 60 秒检查一次（`risk.liquidity_check_interval_seconds`）：剩余流动性 / 净资产 **跌破 15%**
（`risk.deleverage_trigger_ratio`），且距上次降杠杆超过 15 分钟（`risk.deleverage_cooldown_seconds`），
就把**所有持仓按同一比例卖出**，让剩余流动性回到 25%，并 @你。`!pause` 时也照常运行（这是保护）。
可用 `risk.deleverage_enabled: false` 关闭（不建议）。

### 2.7 其他保护

| 保护 | 规则 | 配置 |
|---|---|---|
| 追高保护 | 买入参考价比他的成交价高出 10% 以上 → 不买，报警 | `trading.max_price_deviation_pct` |
| 信号时效 | 收到时信号时间戳已超过 10 分钟 → 不交易，报警（他的持仓记录照常更新，之后可 `!sync`） | `trading.max_signal_age_seconds` |
| 时间倒序 | 时间戳比已处理的最新信号还早 → 忽略，报警 | — |
| 重复信号 | 同一条信号转发两次 → 第二次静默忽略（就算重复处理，按目标仓位计算也不会重复买） | — |
| 小单 | 金额 < 100 美元的买单 / 非清仓卖单不下 | `trading.min_order_value_usd` |
| 暂停 | `!pause` 后不跟单，但仍记录他的持仓；止损和降杠杆照常 | — |
| 启动 | 启动时读取信号频道最近 20 条消息，只用来了解他的最新持仓，**不下单** | — |

> **重要：机器人管理账户里的所有多头持仓**（`ignore_symbols` 除外）：都会挂止损、参与降杠杆，
> `!sync` 还会卖掉他没有持有的股票。请使用**专用账户**；手动管理的股票必须写进
> `trading.ignore_symbols`。

---

## 3. Discord 设置

### 3.1 创建机器人

1. 打开 <https://discord.com/developers/applications> → **New Application**。
2. 左侧 **Bot** → **Reset Token** → 复制 token 填到 `.env` 的 `DISCORD_TOKEN`（只显示一次，不要泄露）。
3. 同一页 **Privileged Gateway Intents** → 打开 **MESSAGE CONTENT INTENT**（否则读不到消息内容）。
   建议关闭 **Public Bot**。
4. 左侧 **OAuth2 → URL Generator**：SCOPES 勾 `bot`；BOT PERMISSIONS 勾
   **View Channels**、**Send Messages**、**Read Message History**。打开生成的链接，把机器人加入**你自己的**服务器。
5. 在你的服务器建两个频道：`#信号`（信号转发进来）和 `#播报`（机器人播报；命令也可以在这里发，
   或者再建一个 `#控制`）。如果是私密频道，记得在频道权限里允许机器人查看和发言。

### 3.2 拿到 ID 并填写配置

Discord **用户设置 → 高级 → 开发者模式** 打开后：右键频道 → **复制频道 ID**；右键自己的头像 → **复制用户 ID**。

```yaml
discord:
  signal_channel_id: 111111111111111111          # 换成 #信号 频道 ID
  allowed_signal_author_ids: [222222222222222222] # 你的用户 ID; 用「关注」时再加上 webhook ID
  log_channel_id: 333333333333333333             # 换成 #播报 频道 ID
  control_channel_id: null                       # 或 #控制 频道 ID
  owner_ids: [222222222222222222]                # 能发命令、报警时被 @ 的人 (你的用户 ID)
```

**一定要填 `allowed_signal_author_ids`**：留空时任何能在 `#信号` 发言的人都能触发交易。
同时把 `#信号` 设成只有你（和机器人）能发言。

### 3.3 把信号送进你的频道

机器人支持纯文本、embed、Discord 原生「转发」三种形式。按推荐顺序：

1. **「关注」（Follow）**：如果他的频道是**公告频道**并允许关注，点频道名旁的「关注」，选你的
   `#信号` 频道。之后 Discord 官方会自动、实时地把消息推送过来（以 webhook 身份发送）。
   右键推送进来的消息的发送者 → 复制用户 ID（webhook 消息的发送者 ID 就是 webhook ID），加入 `allowed_signal_author_ids`。
2. **原生「转发」（Forward）**：右键消息 → 转发 → 选你的 `#信号` 频道。发送者是你自己。
   注意 10 分钟时效（2.7 节），转发晚了不会交易。
3. **复制粘贴**原文也可以（需要包含时间戳那一行）。

> **注意**：用脚本自动操作你的**个人账号**（所谓 self-bot / 用户 token 脚本）违反 Discord 服务条款，
> 账号可能被封（连带失去信号来源）。本项目不提供这类功能，也不建议这样做。能用「关注」就用「关注」。

---

## 4. IBKR 设置

1. **专用账户**：机器人会管理账户里的所有多头持仓（见 2.7 节的提示），建议单独开一个账户
   （IBKR 支持在账户管理里申请额外的关联账户，具体以 IBKR 官网为准）。一个用户名下有多个账户时，
   在 `ibkr.account` 里填写账户号。
2. **先用模拟盘（PAPER）**：`.env` 设 `TRADING_MODE=paper`、`IBKR_PORT=4004`。
   模拟账户的用户名可在 Client Portal 的「模拟交易账户」设置里查看 / 重置密码；也可以用实盘用户名登录
   对应的模拟账户（以 IBKR 为准）。想让模拟盘也有实时行情，需要在同一设置页开启「与模拟账户共享行情订阅」（以 IBKR 为准）。
3. **API**：IB Gateway 在 docker 里运行，API 由它自动开启，不需要手动操作。`.env` 里
   `READ_ONLY_API=no` 必须保留，否则只能查看不能下单。
4. **行情订阅**：盘前限价、追高保护都依赖实时报价（含盘前盘后）。通常需要
   *US Securities Snapshot and Futures Value Bundle* + *US Equity and Options Add-On Streaming Bundle*
   （**以 IBKR 官网为准**，非专业用户价格较低）。没有订阅时可把 `ibkr.market_data_type` 设为 `3`
   （延时 15 分钟行情），但盘前价格会不准，机器人会更多地退回使用他的成交价或等开盘。
5. **订单预防性限制（Precautions）**：Gateway 可能会拦截超过「预防性数量 / 金额限制」的 API 订单，
   大单被拒（播报里会显示被拒原因）。解决：`.env` 设 `BYPASS_WARNING=yes`
   （= 勾选 *Global Configuration → API → Precautions* 里的 *Bypass Order Precautions for API Orders* 等选项），
   或者用 VNC 进入 Gateway 手动调整。通过 VNC 改的设置在容器重建后可能丢失，优先用环境变量。
6. **实盘的两步验证（2FA）**：实盘登录需要在手机 **IBKR Mobile** 上点确认。需要确认的时机：
   第一次启动、容器重启 / 重建、每周一次的完整重新登录（通常在周末，可用 `TWS_COLD_RESTART`
   指定一个你醒着的时间）。每天的自动重启（`AUTO_RESTART_TIME`，默认美东 23:59）**不需要** 2FA。
   没及时确认会再次推送（`TWOFA_TIMEOUT_ACTION=restart`、`RELOGIN_AFTER_TWOFA_TIMEOUT=yes`）。
   等待登录期间机器人无法交易：信号会存为待执行，连上后执行，并在 Discord 报警。
7. **保证金账户类型**：
   - **Reg T**（普通保证金账户）：隔夜最多约 2 倍杠杆，`trading.max_gross_leverage` 保持 ≤ 1.9（默认）。
   - **Portfolio Margin**（需要较高净资产，门槛以 IBKR 官网为准）：保证金率更低，可以适当调高
     `max_gross_leverage`；`reserve_ratio` 仍然会限制总仓位。
   - 净资产较低的保证金账户可能受日内交易（PDT）规则限制，以 IBKR / FINRA 最新规定为准。

---

## 5. 部署（云服务器 + Docker）

**服务器**：1 vCPU / 2 GB 内存 / 20 GB 硬盘足够（IB Gateway 是 Java 程序，约占 1 GB 内存，建议再加
1–2 GB swap）。推荐**美国东部机房**（离 IBKR 服务器近，访问 Discord 无障碍）。不需要开放任何入站端口（SSH 除外）。

```bash
# 1. 安装 Docker (Ubuntu / Debian)
curl -fsSL https://get.docker.com | sudo sh

# 2. 上传代码 (git clone 或 scp 整个目录)
git clone <你的仓库地址> copybot && cd copybot

# 3. 配置
cp .env.example .env && chmod 600 .env && nano .env              # token、IBKR 账号密码
cp config.example.yaml config.yaml && nano config.yaml           # 频道 ID、用户 ID 等
mkdir -p data logs && sudo chown -R 1000:1000 data logs          # 容器内以 uid 1000 运行

# 4. 启动
docker compose up -d --build
docker compose ps
```

启动后 `#播报` 频道会出现「跟单机器人已上线（模式: DRY-RUN）」，在命令频道发 `!status` 检查账户。

| 操作 | 命令 |
|---|---|
| 看机器人日志 | `docker compose logs -f copybot`（文件日志在 `logs/` 目录，自动轮转） |
| 看 Gateway 日志（登录 / 2FA 问题） | `docker compose logs -f ib-gateway` |
| 改了 `config.yaml` | `docker compose restart copybot` |
| 改了 `.env` | `docker compose up -d`（实盘时 Gateway 重建需要 2FA 确认） |
| 更新代码 | `git pull && docker compose up -d --build` |
| 停止 | `docker compose down` |
| 备份 | `docker compose stop copybot` 后复制 `data/copybot.db`（信号记录、待执行指令、止损标记都在里面） |
| 查看 Gateway 界面 | `.env` 设 `VNC_SERVER_PASSWORD`，本地执行 `ssh -L 5900:127.0.0.1:5900 用户@服务器IP`，再用 VNC 客户端连 `localhost:5900` |

说明：
- 停止机器人后，IBKR 上的 GTC 止损单**仍然有效**；盘前 / 盘后挂的限价单是当日有效（DAY），当天结束自动失效，但在那之前仍可能成交（机器人不在就不会有开盘补单）。
- 待执行指令保存在 `data/` 里，重启后继续执行；但只执行**当天**到期的，之前交易日没执行的会被丢弃并报警（可用 `!sync` 对齐）。
- 机器人重启时会读取信号频道最近 20 条消息：重启期间收到、且时间戳还在 10 分钟内的新信号会**照常执行**；
  更早错过的只报警（@你），不自动交易。
- IB API 端口只在 docker 内部网络可见，不要把 4003/4004 映射到公网（IB API 没有加密和认证）。

---

## 6. 上线步骤

强烈建议按这个顺序，每一步确认没问题再进入下一步：

| 步骤 | `.env` | `config.yaml` | 时长 | 检查什么 |
|---|---|---|---|---|
| ① 模拟盘 + 只算不下 | `TRADING_MODE=paper`、`IBKR_PORT=4004` | `dry_run: true` | 1–2 天 | 每条信号都有播报；G、股数、保证金率合理 |
| ② 模拟盘真实下单 | 同上 | `dry_run: false` | 几天（至少各经历一次盘前信号和常规时段信号） | 成交、开盘补单、IBKR 里能看到 GTC 止损单；试一遍 `!pause` `!resume` `!sync` `!cancel` |
| ③ 实盘 + 只算不下 | `TRADING_MODE=live`、`IBKR_PORT=4003` | `dry_run: true` | 1 天 | `!status` 里的净资产、持仓与你在 IBKR 看到的实盘账户一致；计划订单合理 |
| ④ 实盘 | 同上 | `dry_run: false` | — | 可以先把 `max_gross_leverage` 调低（如 1.0），跑顺后再调回 |

`TRADING_MODE` 和 `IBKR_PORT` 必须一起改；不匹配时机器人只是连不上 Gateway，不会下错账户。
`.env` 里的 `COPYBOT_DRY_RUN` 留空即以 `config.yaml` 为准。

---

## 7. Discord 命令

在命令频道（`control_channel_id`，没设置时就是 `#播报`）发送，只有 `owner_ids` 里的人有效。
全角「！」也可以。

| 命令 | 作用 |
|---|---|
| `!status` | 查看账户、持仓、止损单、挂单、待执行指令 |
| `!pause` | 暂停跟单（仍记录他的持仓；止损和降杠杆保护继续运行） |
| `!resume` | 恢复跟单 |
| `!sync` | 预览：按他最新的持仓、用当前 G 把所有股票对齐的下单计划（差额小于目标市值 5% 的不动） |
| `!sync confirm` | 执行上面的计划（必须在 `!sync` 后 180 秒内；按当前时段规则执行） |
| `!cancel` | 撤销机器人所有未成交的单（**不含止损单**），并清空待执行指令 |
| `!help` | 显示帮助 |

`!sync` 适合：错过信号后（例如信号超时没交易、机器人停过机）、或者想按新的净资产整体调整仓位。

---

## 8. 常见问题 & 风险提示

**风险**
- **杠杆风险**：1.9 倍杠杆下，他的组合跌 10%，你的净资产约跌 19%。剩余流动性跌到 0 以下时，
  IBKR 会**直接强平**（没有追保通知），不经过机器人。降杠杆（2.6 节）就是为了尽量避免走到这一步，但不能保证。
- **止损只在常规时段触发**：隔夜或盘前跳空低开时，止损单在开盘后变成市价单，成交价可能远低于止损价。
- **滑点**：机器人用市价单和盘前限价单，成交价和他的不同；盘前流动性差、价差大。
- **跟单延迟**：转发越慢，价格偏离越大；超过 10 分钟的信号不交易。

**信号收不到 / 没反应**
- 他换了消息格式时，机器人认不出来，会当普通消息**静默忽略**，`#播报` 不会有任何动静。
  他发了信号而你没看到播报：发 `!status`，再看 `docker compose logs copybot`。
- 检查：消息是否发在 `signal_channel_id` 对应的频道；发送者是否在 `allowed_signal_author_ids` 里
  （用「关注」时是 webhook ID）；Developer Portal 里 MESSAGE CONTENT INTENT 是否打开。

**部署问题**
- 日志里出现 `unable to open database file` / `PermissionError`：没有执行
  `sudo chown -R 1000:1000 data logs`。
- `docker compose up` 报 `bind source path does not exist ... config.yaml`：还没有
  `cp config.example.yaml config.yaml`。
- 报 `required variable ... is missing a value`：`.env` 里对应的值没填。
- 密码里有 `$`、`#`、空格：在 `.env` 里用单引号包起来，例如 `TWS_PASSWORD='ab$c#d'`。
- 机器人一直连不上 Gateway：看 `docker compose logs ib-gateway`——常见原因是实盘在等 2FA 确认、
  密码错误、`TRADING_MODE` 与 `IBKR_PORT` 不匹配。用同一个用户名在别处登录 TWS / Gateway 也可能把它挤下线。
- 每天美东深夜 IBKR 服务器维护、Gateway 自动重启时会短暂断线，机器人会自动重连。

**交易问题**
- 订单被拒，原因提到 precaution / size limit：见第 4 节第 5 条（`BYPASS_WARNING=yes`）。
- 盘前没下单、全部等开盘：没有报价（检查行情订阅 / `ibkr.market_data_type`）且他的成交价是「无」。
- 某只股票被跳过：看播报里的原因（追高保护、金额太小、拿不到价格、剩余流动性不足……）。
- 账户里有你自己的股票：写进 `trading.ignore_symbols`，否则会被挂止损、参与降杠杆、被 `!sync` 卖掉。
- `COPYBOT_DRY_RUN` 只认 `1/true/yes` 为「只算不下」，其他非空值（包括拼错）都等于**真实下单**；不用就留空。

---

## 9. 开发

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

本机直接运行（需要本机已登录的 IB Gateway / TWS 并开启 API；`ibkr.port`：桌面版 Gateway 模拟盘 4002、
TWS 模拟盘 7497）：

```bash
cp config.example.yaml config.yaml      # 改 ibkr.port 和 Discord ID
export DISCORD_TOKEN=xxxx
python -m copybot                       # 配置文件路径可用 COPYBOT_CONFIG 指定
```

代码结构（详见 `docs/DESIGN.md`）：

| 文件 | 作用 |
|---|---|
| `copybot/parser.py` | 解析信号文本 |
| `copybot/market_hours.py` | 美东交易时段、NYSE 节假日 |
| `copybot/sizing.py` | G、目标股数、流动性缩放、止损、降杠杆计算（纯函数） |
| `copybot/engine.py` | 交易流程、待执行指令调度、止损同步、降杠杆 |
| `copybot/broker/ibkr.py` / `dryrun.py` | IBKR 接口（ib_async）/ 只算不下的模拟包装 |
| `copybot/discord_bot.py` / `notifier.py` | Discord 读取信号、命令、播报 |
| `copybot/store.py` | SQLite 状态（`data/copybot.db`） |
| `copybot/config.py` | 配置项定义（`config.example.yaml` 与之一一对应） |
