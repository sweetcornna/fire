# Github Action 部署

> 前提：确保您已获取到所有配置，详见：[【DouYinSparkFlow 配置生成器】使用说明](配置生成器使用.md)

本项目已经预设Action配置，只需填写相关配置即可启用。

## 1. Fork 仓库

采用Action部署本项目需要先 Fork 仓库。

操作步骤如下：

1. 打开本项目主页，点击右上角 Fork，将仓库复制到你的 GitHub 账号下。
2. 进入你账号下新生成的仓库，完成后续配置

> 项目有用别忘了点Star支持开发者

## 2. 启用workflow与action

首次fork后需要手动启用`workflow`和对应`action`

在自己fork后的仓库上方点击`Actions`按照下方图示启用工作流

![启用workflow](images/启用workflow.png)

![启用action](images/启用action.png)

## 3. 创建 Environment（环境）

这一步在你 Fork 后的仓库中创建名为 `user-data` 的 Environment（环境）。

操作路径：进入你Fork项目后的 GitHub 仓库，依次点击 `Settings` -> `Environments` -> `New environment`，名称填写 `user-data` 并创建。

说明：这里创建的是部署环境（Environment），后续再在该环境下配置 Secrets 和 Variables。

![创建`user-data`环境图](images/屏幕截图%202026-02-14%20224915.png)

## 4. 配置 Secrets 和 Variables

在你刚创建的 `user-data` Environment 中，分别配置 Variables 和 Secrets。

操作步骤如下：

1. 打开已经填写好的配置生成器页面，先查看左侧上方`Environment Variables` 区域。
2. 进入 GitHub 仓库的 `Settings` -> `Environments` -> `user-data` -> `Environment variables`，逐条新增对应变量。
3. 回到配置生成器，查看左侧下方 `Environment Secrets` 区域。
4. 进入 GitHub 仓库的 `Settings` -> `Environments` -> `user-data` -> `Environment secrets`，逐条新增对应密钥。

注意事项：

- 变量名和变量值请与配置生成器保持完全一致（包含大小写）建议直接使用复制按钮复制粘贴。
- 不要把 Secrets 内容填到 Variables，也不要把 Variables 内容填到 Secrets。

![配置生成器](images/配置生成器.png)

## 4.1 启用 AI 生成消息（可选，推荐）

默认情况下消息走「模板 + 一言」生成。如果希望每天读取抖音实时热榜，再由 AI 参考公开真人评论声纹现写一句自然怪话，请在 `user-data` 环境中追加下列配置。热榜只作可选灵感，不会强行加入“续火花”语境；未配置 AI 时会自动回落模板，不影响运行。

在 `Settings` -> `Environments` -> `user-data` -> `Environment variables` 中新增：

| 变量名 | 值 | 说明 |
| --- | --- | --- |
| `ANTHROPIC_BASE_URL` | `https://api.cornna.xyz` | Anthropic 协议（`/v1/messages`）网关根地址；带结尾的 `/v1` 也行，程序会自动去掉 |
| `ANTHROPIC_MODEL` | `gemini-3.8-flash-high` | 使用的模型 ID，需与网关 `GET /v1/models` 返回的 `id` 完全一致 |
| `MESSAGE_AI_ENABLE` | `1` | `1` 强制开启 AI；`0` 关闭；留空表示有 key 即自动开启 |
| `MESSAGE_STYLE_EXAMPLES` | 可留空 | 可选；每行一条你过去真实发过的短消息，模型只模仿语气和节奏，不照抄内容 |
| `AI_MESSAGE_TEMPLATE` | `[盖瑞]今日续火花啦[加一]\\n[右边]今日一串：{content}[左边]` | 最终发送模板；`{content}` 会替换为 AI 正文，`\\n` 表示换行；方括号内容是抖音表情短码 |

在 `Environment secrets` 中新增：

| 密钥名 | 值 | 说明 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | `你的 API Key` | 必须放在 Secrets，切勿填到 Variables |

说明：

- **`ANTHROPIC_API_KEY` 只能放 Secrets**，不要写进 Variables、代码或任何会提交的文件。
- 旧的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 仍可用作回落（`ANTHROPIC_*` 缺省时读取），但**模型名没有回落**：只认 `ANTHROPIC_MODEL`，未设置时固定用 `gemini-3.8-flash-high`。从旧配置迁移时记得把模型名也改过来。
- 选择模型前请确认它在你的接口上可用，模型 ID 以网关 `GET /v1/models` 返回的为准（网关上的 ID 可能和厂商官方名称不同）。若上游有「User location is not supported」等地区限制会调用失败（自动回落模板），换一个可用模型即可。
- 还可选配 `MESSAGE_AI_PERSONAS`（JSON 数组）自定义聊天语气；不配则用内置的 5 种自然语气逐日轮换。
- 可在 Actions 中手动运行 dev 工作流，并将 `preview_only` 设为 `true`。程序会生成 1—10 条真实 AI 文案写入日志，但不会登录抖音或发送；外部每日定时触发默认仍会真实发送。

## 5. 修改执行时间（可选）

如需调整自动执行时间，编辑仓库文件 `.github/workflows/schedule.yml`，找到下方配置：

```yaml
on:
  workflow_dispatch: # 允许手动触发
  schedule: # 定时任务
    - cron: "0 1 * * *" # 每天 1:00 UTC（对应北京时间 9:00）
```

将 `cron: "0 1 * * *"` 修改为你需要的时间表达式即可。

注意事项：

- GitHub Actions 的 `cron` 使用 UTC 时区，不是北京时间。
- 北京时间（UTC+8） = UTC 时间 + 8 小时。
- 建议先手动触发一次工作流，确认配置无误后再依赖定时任务。

Cron 基础语法（5 段）：

`分钟 小时 日 月 星期`

常用写法：

- `*`：任意值
- `*/n`：每 n 个单位执行一次
- `a,b`：在多个指定值执行
- `a-b`：在一个范围内执行

示例（UTC）：

- `0 1 * * *`：每天 UTC 01:00（北京时间 09:00）
- `30 13 * * *`：每天 UTC 13:30（北京时间 21:30）
- `0 */6 * * *`：每 6 小时执行一次
- `0 1 * * 1-5`：工作日 UTC 01:00 执行

> 可以交给 AI 生成，下面给出提示词示例可以直接套用：
>
> GitHub Actions 的默认时区是 UTC。我需要每天在北京时间 XXX 自动触发工作流，请换算后给出 cron 表达式。除 `cron: "..."` 这一行外，不需要输出其他内容。

## 6. 手动触发测试（可选）

> 建议执行此步骤，可以验证配置是否达到预期，此外首次fork后也需要手动触发后续才会自动执行

仓库的工作流中添加了`workflow_dispatch`以便允许进行手动触发，在初次配置完成后可以通过手动触发Action来进行验证，操作方式如下图所示：

![手动测试](images/屏幕截图%202026-02-14%20224614.png)
