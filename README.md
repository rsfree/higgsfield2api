# Higgsfield MAGI-2 → 方舟视频协议

把 `higgsfield.ai/ai/video?model=magi_2` 的网页调用收敛成一个异步任务 API。**默认零消耗**：不开
`ALLOW_BILLING` 时它只翻译、校验、打印真实上游报文，一次额度都不花。

支持**多账号号池**：粘贴 N 份 Copy-as-cURL 就能把 N 个号的免费额度接进来，按「最少在飞 + 最久未用」
轮询，401/429/Cloudflare 拦截自动换号重试，失效号自动熔断并在面板上标红。

> 服务器部署（Docker 镜像 / 反代 / 证书 / 升级回滚 / 排障表）：见 **[DEPLOY.md](./DEPLOY.md)**。

---

## 一、结论先行

| 问题 | 结论 |
|---|---|
| 官方 API 有没有 MAGI-2？ | **没有**。`docs.higgsfield.ai` sitemap 154 个模型页全表核对，无 magi；官方 API 最新到 seedance-2-5 / kling-3 / wan-3 |
| 网页用的是什么接口？ | `POST https://fnf-api-gw.higgsfield.ai/fnf/jobs/v2/magi_2`，`{params:{...}}` 信封 |
| 能不能纯服务端跑？ | **能**，但必须先过两道门：Cloudflare 指纹 + Clerk 会话。两者都能用 cookie 复用，不需要常驻浏览器 |
| 唯一会花钱的动作 | `POST /jobs/v2/magi_2`。查询 `GET /jobs/{id}` **匿名可读**（实测返回 404 而非 401） |
| 号池怎么保证不重复计费 | 只在「上游明确没受理」时换号重试（401 / 429 / CF 403）；网络异常与 5xx 一律不重发，因为 `{id}` 是唯一幂等键 |

---

## 二、取证事实（可复核）

| # | 事实 | 证据 |
|---|---|---|
| 1 | API 基址是 `https://fnf-api-gw.higgsfield.ai/fnf` | 前端常量：`Xp=(Yp='https://fnf-api-gw.higgsfield.ai/fnf')` |
| 2 | 创建路径 `/jobs/v2/{jobType}`，MAGI-2 的 jobType 是 `magi_2` | `fetch(Vt(\`/jobs/v2/${gn.X}\`))`；映射表 `[V.MAGI_2]:'magi_2'` |
| 3 | 创建体是 `{params:…}`，可选 `use_free_gens` / `use_unlim` | `JSON.stringify({params:t})`、`{...t,use_free_gens:…}` |
| 4 | 鉴权头只认这几个 | 允许列表：`authorization / cookie / hf-workspace-id / hf-surface / cf-access-client-id / cf-access-client-secret / x-hf-secret` |
| 5 | JWT 可用 cookie 换，无需浏览器 | `GET https://higgsfield.ai/_clerk/v1/client` 返回 `{response:{sessions:[{last_active_token:{jwt}}]}}` |
| 6 | Cloudflare 拦裸 curl，不拦 Chrome 指纹 | 裸 curl `GET /jobs/v2/magi_2` → 403 `Just a moment`；`curl_cffi impersonate=chrome` → 直接进业务层 |
| 7 | 单任务查询匿名可读 | `GET /fnf/jobs/{随机UUID}` → `404 {"detail":"Job not found"}`（不是 401） |
| 8 | 状态词表 6 态 | 官方 SDK：`queued / in_progress / completed / failed / nsfw / canceled` |
| 9 | MAGI-2 硬约束 | 只出 1080p（前端校验原文 `MAGI-2 renders only 1080p`）、4–30 秒、参考图 ≤10、参考视频 ≤5、参考音频 ≤5 |
| 10 | 额度画像 | 前端常量 `magi_2_free_gens = {freeGens:1, credits:10, daysTotal:7}` |

> 复核方式：上述字符串都能在 `assets.higgsfield.ai/tanstack/assets/*.js` 里 grep 到；本仓库没改任何前端代码。

---

## 三、数据流

```
client ──canonical──▶ POST /contents/generations/tasks
                        │ parse_request()  纯函数：结构/场景/取值域校验（免费）
                        │ build_params()   纯函数：canonical → magi_2 params
                        ▼
                   ALLOW_BILLING? ──否──▶ 403 Forbidden.BillingDisabled + 提示 /admin/dry-run
                        │是
                        ▼
              POST fnf-api-gw/fnf/jobs/v2/magi_2   {params:{…}, use_free_gens:true}
                        │ 200 + {id}  ← 只等于受理
                        ▼
                   sqlite 台账（job_id / job_set_id / submitted_at）
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
   GET /tasks/{id}   回调轮询器      （列表只读本地台账）
        │               │
        └──────► job_to_task() 同一个序列化函数 ◀──────┘
                        │
                  status=succeeded 且 content.video_url 非空  ← 唯一成功判据
```

---

## 四、快速开始

```bash
cd higgsfield-magi2-api
pip install -r requirements.txt
cp .env.example .env

# 1) 拿会话：浏览器登录 higgsfield.ai → DevTools → Network → 任意 XHR → Copy as cURL
#    存成 req.txt，然后：
python -m tools.cookie_import --curl req.txt

# 2) 零计费自检（只打 GET /jobs?size=1）
curl -s localhost:8000/admin/session

# 3) 起服务
uvicorn hfapi.server:app --host 127.0.0.1 --port 8000

# 4) 看真实上游报文（不花钱）
curl -s -X POST localhost:8000/contents/generations/tasks \
  -H 'Content-Type: application/json' -H 'X-Dry-Run: 1' \
  -d '{"model":"magi_2","content":[{"type":"text","text":"一只橘猫在雨天的霓虹街头慢跑"}],"duration":4,"ratio":"16:9"}'
```

确认报文没问题，再开计费跑第一次真任务（最短时长、单条）：

```bash
# .env: ALLOW_BILLING=1 MAX_DURATION=4 MAX_BATCH=1
curl -s -X POST localhost:8000/contents/generations/tasks \
  -H 'Content-Type: application/json' -H 'X-Idempotency-Key: first-shot-001' \
  -d '{"model":"magi_2","content":[{"type":"text","text":"一只橘猫在雨天的霓虹街头慢跑"}],"duration":4,"ratio":"16:9"}'
# → {"id":"<job_id>"}

curl -s localhost:8000/tasks/<job_id>
```

---

## 五、鉴权与运维端点

这个服务是「账号额度的看门人」，对外发布必须加锁。设 `SERVICE_TOKEN` 后，除 `/healthz` 外全部端点要求
`Authorization: Bearer <token>`（也接受 `X-Api-Key: <token>`）：

```bash
curl -s https://<host>/healthz                              # 公开：只说明服务活着 + 当前护栏
curl -s https://<host>/admin/session -H "Authorization: Bearer $SERVICE_TOKEN"
```

| 端点 | 计费 | 用途 |
|---|---|---|
| `GET /healthz` | — | 存活 + 护栏快照（`auth_required` / `billing_enabled` / `max_batch`） |
| `GET /admin/session` | 0 | 号池概览 + 逐号 `GET /jobs?size=1` 自检（`?probe=0` 只看状态） |
| `GET /admin/accounts` | 0 | 号池状态快照，不探测上游，秒回 |
| `POST /admin/dry-run` | 0 | 等价于 `X-Dry-Run: 1`：翻译 + 校验，打印真实上游报文 |
| `POST /admin/reload` | 0 | 从 `data/accounts.json` 重读号池，不必重启容器 |

`SERVICE_TOKEN` 为空时不做鉴权 —— **该模式只能绑回环地址**，否则任何人都能拿你的额度建任务。

### 号池（多账号）

号池存在 `data/accounts.json`（0600，含 cookie 明文，**不要入 git、不要外发**）。一个账号 = 一份浏览器
cookie + 独立的 JWT 缓存 + 独立的 curl_cffi 客户端，号与号之间完全隔离。

| 机制 | 行为 | 为什么这么定 |
|---|---|---|
| 选号 | 可用号里取 `(在飞数, 最后使用时间)` 最小者 | 等价于加权轮询，但短任务不会让某个号被反复砸中 |
| 换号重试 | 仅 401 / 429 / Cloudflare 403，最多 `POOL_RETRY`（默认 2）个号 | 这三种是「上游明确没受理」，重发不会多扣一次额度 |
| **不重试** | 网络异常、5xx、超时 | 请求可能已到达上游，重发就是第二个计费任务 |
| 熔断 | 连续 `POOL_AUTH_STRIKES`（默认 2）次 401 判死；429 冷却 90s；其它指数退避 | 单次 401 可能只是 Clerk 抖动，一次就判死会误杀好号 |
| 恢复 | 冷却到期自动半开；换 cookie 或点「启用」立刻清零 | 不需要重启，也不需要删号重建 |
| 任务归属 | 建任务时把 `account_id` 写进台账，查询优先用同一个号 | 换号查别人的任务虽然上游也认，但归因会乱 |

号池为空时建任务返回 `503 NoAvailableAccount`，错误信息里带「几个停用 / 几个失效 / 几个冷却」，
不会含糊地说一句「服务不可用」。

账号数量没有硬上限，但**别把几十个号同时挂上去**：上游风控看的是 IP + 指纹维度，
同一个出口 IP 上高并发多号，比单号跑更容易整段被封。

### 运维面板 `/panel`

登录口令 = `PANEL_PASSWORD`，未单独配置时复用 `SERVICE_TOKEN`（一个口令管 API + 面板）。

| 能力 | 说明 |
|---|---|
| 号池 | 每个号的状态 / 成功失败计数 / 最后成功时间；单号自检、换 cookie、停用、删除 |
| 批量导入 | 一次粘贴多段 Copy as cURL（按行首 `curl` 自动切分），导入后逐个自检 |
| 总览 | 计费开关、号池可用数、台账计数、护栏值 —— 全部只读 |
| 任务台账 | 最近 50 条：状态 / 归因到哪个号 / 时长 / 比例 / prompt / 产物链接，可单条回源刷新 |
| 报文干跑 | 表单直接产出真实上游报文，零计费 |
| 刻意不做 | **开计费开关**。`ALLOW_BILLING` 只能改 `.env` 重启，让"花钱"不会是一次误点 |

面板会话是 `HttpOnly + SameSite=Strict` 的签名 cookie（HMAC 密钥即口令，改口令即全端下线），
连续 8 次口令错误锁 5 分钟；`PANEL_PASSWORD` 与 `SERVICE_TOKEN` 都为空时面板直接 503 关闭，
不存在「无口令的 cookie 上传入口」。

---

## 六、这两次免费额度 + 10 积分怎么花

| 顺序 | 动作 | 花费 | 目的 |
|---|---|---|---|
| 0 | `/admin/session` | 0 | 确认 cookie 没烂，省掉「跑一次才发现 401」 |
| 1 | `X-Dry-Run: 1` | 0 | 拿到逐字报文，人工核一遍字段名 |
| 2 | 第 1 次真跑：`duration=4, batch=1, 纯文本` | 1 次免费额度 | 验证创建 + 状态机 + 产物 URL 三件事，**一次拿全** |
| 3 | 若第 2 步 422 | 0 | 参数错在上游校验阶段，不扣额度；按 `error.upstream.body` 改 `build_params` |

> 关键：**422 是免费的**。所以第一次真跑要挑「纯文本 + 最短时长」，把不确定的字段（`aspect_ratio` 拼写、
> `medias` 空数组是否可省）一次问清楚。别拿首尾帧去做第一次实验 —— 那会同时引入素材 ID、role、场景互斥三个变量。

---

## 七、协议差异表

| 方舟字段 | MAGI-2 | 处置 |
|---|---|---|
| `model` | `magi_2` | A：别名归一，**回显客户端原名** |
| `content[].text` | `prompt` | A |
| `image_url` + `first_frame` | `start_image` | A |
| `image_url` + `last_frame` | `end_image` | A |
| `image_url` + `reference_image` | `image` | A（≤10） |
| `video_url` / `audio_url` | `video` / `audio` | A（各 ≤5） |
| `duration` 4–30 | `duration` | A；越界 400，不静默取默认 |
| `resolution` | **仅 1080p** | 传 720p → **400**（静默升档会改账单） |
| `ratio` | `aspect_ratio` | A；`adaptive`→`auto` |
| `batch_size` | `batch_size` | A，但受 `MAX_BATCH` 硬上限 |
| `seed` `frames` `watermark` `draft` `tools` `priority` `service_tier` `output_format` `camera_fixed` | 无 | **C：400 UnsupportedParameter** |
| `generate_audio:false` | 无开关 | C：400 |
| 公网 URL / data URI 素材 | 只收素材 ID | C：400，提示先转 `asset://<id>` |
| `callback_url` | 无 | B：本服务自己轮询后 POST 推送 |

---

## 八、已知偏差与未取证（别当成承诺）

1. **`usage` 缺席**：上游不返回 token 计量，本服务不编造。有 `credits_cost` 时才写 `usage.credits_cost`。
2. **`content.last_frame_url`**：上游只给缩略图时不做假承诺，该字段可能缺。
3. **创建响应结构未实测**：`{id}`（单个 job）与 `{id, jobs:[…]}`（job set）两种形状都兼容，取第一个可用 id。
4. **素材上传未实现**：上传是三步（申请票据 → PUT 预签名 → `POST /media/{id}/upload` 确认），票据端点未取证，
   所以本版只支持 `asset://<id>`（素材 ID 可从网页生成记录的 URL 里拿）。
5. **取消端点**：`DELETE /tasks/{id}` 返回 501，不伪造成功。
6. **`nsfw` 不做二次判定**：上游说 nsfw 就是 nsfw，映射成 `OutputVideoSensitiveContentDetected` + 400。

---

## 九、风险提示

- 这是**复用你个人网页会话**的内部接口，不是公开 API：字段随前端发版即失效，`build_params` 是唯一需要改的地方。
- 会话 cookie 等同账号密码，只放 `secrets/cookies.json`（已在 `.gitignore`），别进 git、别进镜像。
- 高频调用会触发风控并可能封号。`MAX_BATCH=1` + 手动触发是当前唯一安全的用法。
- 商用/分发前请自行核对 Higgsfield 服务条款。未设 `SERVICE_TOKEN` 时本服务不做鉴权，**此时只能绑
  `127.0.0.1` 或内网**；公网发布一律 nginx + token（见 [DEPLOY.md](./DEPLOY.md)）。
