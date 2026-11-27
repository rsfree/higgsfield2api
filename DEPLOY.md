# 部署手册（Docker · ai-prod）

> 目标形态：容器只监听 `127.0.0.1:39420`，公网入口是 nginx + `higgsfield2api.1task.cn`。
> 之所以不直接对公网放容器端口：**建任务会消耗上游账号额度**，暴露端口等于把额度送人。

---

## 0. 结论先行

| 项目 | 值 |
|---|---|
| 仓库 / 镜像 | `ghcr.io/rsfree/higgsfield2api`（private，节点需已 `docker login ghcr.io`） |
| 部署目录 | `/opt/higgsfield2api` |
| 监听端口 | `127.0.0.1:39420` → 容器 `8000` |
| 数据卷 | `higgsfield2api_hfdata`（sqlite 台账）、`higgsfield2api_hfsecrets`（cookies.json） |
| 公网入口 | `https://higgsfield2api.1task.cn`（nginx 反代 + acme.sh 证书） |
| 鉴权 | `SERVICE_TOKEN` 必填，`Authorization: Bearer <token>` |
| 计费开关 | `ALLOW_BILLING=0`（默认关；开=真花钱，见 §7） |

---

## 1. 出口网络要求

上游 `fnf-api-gw.higgsfield.ai` 与 `higgsfield.ai/_clerk` 都在 Cloudflare 后面，且做地区/指纹判定：

| 要求 | 说明 |
|---|---|
| 出站 443 可达 | `curl -sI https://fnf-api-gw.higgsfield.ai/fnf/jobs?size=1` 不能超时 |
| 出口 IP 不被 Cloudflare 拦 | 返回 `403` + HTML (`Just a moment`) = 该 IP 被判风险；本服务用 `curl_cffi impersonate=chrome`，多数机房 IP 可过 |
| 出口 IP 稳定 | 会话 cookie 与 IP 绑定较弱，但频繁跳变会触发风控 |
| DNS 可用 | 容器内解析 `higgsfield.ai`；如内网需自建 DNS，则给 compose 加 `dns:` |

机房 IP 被 403 时的处置顺序：换 `HF_IMPERSONATE`（`chrome` → `chrome124`）→ 换出口（该节点换 IP / 加代理）→ 最后才是网页重导 cookie。

---

## 2. 环境要求

| 组件 | 版本 | 校验 |
|---|---|---|
| Docker | ≥ 24 | `docker --version` |
| Docker Compose | v2 插件 | `docker compose version` |
| 磁盘 | ≥ 200 MB | 镜像 ~180 MB + sqlite 增长 |
| 内存 | ≥ 256 MB | 单进程 uvicorn，无并发压力 |

---

## 3. 获取镜像（三选一）

```bash
# 方式 A：直接拉 GHCR（需已登录；ai-prod 已登录）
docker login ghcr.io -u <github_user>       # 密码用 PAT（read:packages）
docker pull ghcr.io/rsfree/higgsfield2api:latest

# 方式 B：Actions 里的离线包（registry 不可达时的兜底，保留 7 天）
#   下载 image-tar-<sha7> artifact → 上传服务器
gunzip -c higgsfield2api-ci.tar.gz | docker load

# 方式 C：服务器本地构建（无外网 registry 时）
git clone git@github.com:rsfree/higgsfield2api.git && cd higgsfield2api
docker build --build-arg VERSION=local -t ghcr.io/rsfree/higgsfield2api:local .
```

镜像 tag 约定：`latest` / `<branch>` / `sha-<7位>` / 打 `v*` tag 时的 semver。
**生产请钉版本**：在 `.env` 里写 `TAG=sha-abc1234`，`latest` 只用于首次试跑。

---

## 4. 配置 `.env`

```bash
mkdir -p /opt/higgsfield2api && cd /opt/higgsfield2api
cp /path/to/repo/docker-compose.yml .

cat > .env <<EOF
TAG=latest

HF_GATEWAY=https://fnf-api-gw.higgsfield.ai/fnf
HF_CLERK_PROXY=https://higgsfield.ai/_clerk
HF_IMPERSONATE=chrome
HF_EXTRA_HEADERS={"hf-surface":"web"}

ALLOW_BILLING=0
USE_FREE_GENS=1
MAX_DURATION=5
MAX_BATCH=1

SERVICE_TOKEN=$(openssl rand -hex 16)

EXECUTION_EXPIRES_AFTER=172800
POLL_INTERVAL=5
EOF
chmod 600 .env
```

`.env` 永不入库；`SERVICE_TOKEN` 就是对外 API Key，泄露等于把额度送人。

---

## 5. 导入会话 cookie（必需）

服务没有 cookie 时 `/healthz` 正常，但 `/admin/session` 与建任务都会 401 —— 这是“活着但没钥匙”，不是故障。

```bash
cd /opt/higgsfield2api
docker compose up -d

# 本机浏览器登录 higgsfield.ai → DevTools → Network → 任意 XHR → 右键 Copy as cURL
# 存成 req.txt 后上传到服务器，然后：
docker compose exec -T higgsfield2api \
  python -m tools.cookie_import --curl /dev/stdin < req.txt
```

该命令会写 `/app/secrets/cookies.json`（具名卷内）并顺手跑一次零计费的 `GET /jobs?size=1` 自检：

- 输出 `"ok": true` → 会话可用；
- 输出 `缺少 ['__client']` → 你导的是 `document.cookie`，HttpOnly 拿不到，必须用 Copy as cURL；
- 输出 `Clerk 返回 401/403` → cookie 过期或被 Cloudflare 拦，重登再导。

重导后**不必重启**：

```bash
docker compose exec -i higgsfield2api sh -c 'cat > /app/secrets/cookies.json' < cookies.json
curl -s -X POST localhost:39420/admin/reload -H "Authorization: Bearer $TOKEN"
```

---

## 6. 验证清单

```bash
TOKEN=$(grep ^SERVICE_TOKEN /opt/higgsfield2api/.env | cut -d= -f2)
B=http://127.0.0.1:39420

# 1) 进程活着 + 鉴权已开
curl -s $B/healthz
# {"ok":true,"auth_required":true,"billing_enabled":false,...}

# 2) 无 token 必须 401（这是红线，不是可选）
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/contents/generations/tasks \
  -H 'Content-Type: application/json' -d '{"model":"magi_2","content":[{"type":"text","text":"x"}]}'
# 401

# 3) 会话可用（零计费）
curl -s $B/admin/session -H "Authorization: Bearer $TOKEN"
# {"ok":true,"status":200,...}

# 4) 报文翻译（零计费）
curl -s -X POST $B/admin/dry-run -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"magi_2","content":[{"type":"text","text":"一只橘猫"}],"duration":4,"ratio":"16:9"}'
# dry_run=true, will_charge=false

# 5) 公网入口（证书就位后）
curl -s https://higgsfield2api.1task.cn/healthz
curl -s -o /dev/null -w '%{http_code}\n' https://higgsfield2api.1task.cn/tasks   # 401 = 鉴权生效
```

---

## 7. 打开计费（真花钱，确认后再动）

```bash
cd /opt/higgsfield2api
sed -i 's/^ALLOW_BILLING=.*/ALLOW_BILLING=1/' .env
docker compose up -d                       # 只有 .env 变了，容器会重建

# 第一次真跑：最短时长 + 单条 + 纯文本，一次拿全「受理 / 状态机 / 产物 URL」
curl -s -X POST localhost:39420/contents/generations/tasks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: first-shot-001' \
  -d '{"model":"magi_2","content":[{"type":"text","text":"一只橘猫在雨天的霓虹街头慢跑"}],"duration":4,"ratio":"16:9"}'
# {"id":"<job_id>"}  ← 只代表受理
curl -s localhost:39420/tasks/<job_id> -H "Authorization: Bearer $TOKEN"
```

`terraform` 式护栏（改 `.env` 即生效，无需改代码）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `MAX_DURATION` | 5 | 请求未给 `duration` 时的兜底秒数；超过 30 一律 400 |
| `MAX_BATCH` | 1 | `batch_size` 硬上限，防一次提交烧掉整月额度 |
| `USE_FREE_GENS` | 1 | 优先消耗免费次数（`use_free_gens:true`） |

---

## 8. nginx 反代 + 证书

ai-prod 沿用宝塔目录约定，三个文件：

```
/www/server/panel/vhost/nginx/higgsfield2api.1task.cn.conf        # :80 ACME + 301
/www/server/panel/vhost/nginx/higgsfield2api.1task.cn-ssl.conf    # :443
/www/server/panel/vhost/nginx/higgsfield2api-proxy.inc            # location 共用体
```

签发证书（HTTP-01，要求 DNS 已指向本机）：

```bash
~/.acme.sh/acme.sh --issue -d higgsfield2api.1task.cn --webroot /www/wwwroot/acme --keylength ec-256
~/.acme.sh/acme.sh --install-cert -d higgsfield2api.1task.cn --ecc \
  --key-file      /www/server/panel/vhost/cert/higgsfield2api.1task.cn/privkey.pem \
  --fullchain-file /www/server/panel/vhost/cert/higgsfield2api.1task.cn/fullchain.pem \
  --reloadcmd     "nginx -s reload"
nginx -t && nginx -s reload
```

反代要点（`higgsfield2api-proxy.inc`）：

```nginx
proxy_pass http://127.0.0.1:39420;
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_buffering off;          # 建任务最长阻塞 ~60s，别让 nginx 攒包
proxy_read_timeout 180s;
proxy_send_timeout 180s;
client_max_body_size 8m;
```

---

## 9. 升级与回滚

```bash
cd /opt/higgsfield2api
cp .env .env.bak
sed -i 's/^TAG=.*/TAG=sha-<新sha7>/' .env
docker compose pull && docker compose up -d
docker compose ps                # STATUS 必须是 healthy

# 回滚：改回旧 tag 再来一次，30 秒内完成；sqlite 台账向后兼容
sed -i 's/^TAG=.*/TAG=sha-<旧sha7>/' .env && docker compose up -d
```

镜像 tag 与 commit 的对应关系只在 `sha-<7位>` 上可靠 —— 排障时先 `docker inspect` 查镜像 label 里的 `org.opencontainers.image.version`。

---

## 10. 排障表

| 现象 | 判断 | 处置 |
|---|---|---|
| 容器 `unhealthy` | `/healthz` 不通 | `docker logs higgsfield2api`；多为端口占用或 uvicorn 启动异常 |
| `/healthz` 200 但 `auth_required:false` | `.env` 没被读到 | 确认 `env_file` 生效：`docker compose exec higgsfield2api env | grep SERVICE_TOKEN` |
| 建任务 401 `AuthenticationError` | 缺/错 token | 客户端补 `Authorization: Bearer` |
| 建任务 403 `Forbidden.BillingDisabled` | 计费没开 | 见 §7。**这是保护，不是 bug** |
| 建任务 503 `NoAvailableService` + HTML | Cloudflare 拦 | 见 §1；换 `HF_IMPERSONATE` 或换出口 IP |
| `/admin/session` 401 `Clerk 返回 401` | cookie 过期 | 重导 cookie（§5），再 `/admin/reload` |
| 建任务 400 `上游拒绝参数` | 前端发版改了字段 | 看 `error.upstream.body`，改 `hfapi/convert.py: build_params` |
| 任务一直 `running` | 上游真慢 / 上游 404 | `curl 上游 /jobs/<id>`；超过 `EXECUTION_EXPIRES_AFTER` 会判 `expired` |
| 502 且日志 `curl: (35)` | 出站 TLS 被拦 | 检查节点出口策略；必要时给容器加 `HTTPS_PROXY` |
| nginx 502 | 端口未监听 | `ss -ltnp | grep 39420`；确认 compose 已起 |

---

## 11. 安全清单（逐条打勾再对外）

- [x] 容器端口绑 `127.0.0.1`，公网只走 nginx
- [x] `SERVICE_TOKEN` 已设置（`/healthz` 的 `auth_required` 为 `true`）
- [x] `ALLOW_BILLING` 与 `MAX_BATCH` / `MAX_DURATION` 三件套按需收紧
- [x] `.env`、`secrets/` 权限 `600`，不进 git、不进镜像
- [x] 不带 token 的请求返回 401（§6 第 2 条）
- [ ] cookie 属于个人账号：泄露等同账号密码，轮换时同步 `/admin/reload`
- [ ] 高频调用会触发上游风控甚至封号 —— `MAX_BATCH=1` + 手动触发是当前唯一安全用法
