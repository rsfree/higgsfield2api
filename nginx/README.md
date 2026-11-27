# nginx 站点配置（ai-prod · 宝塔目录约定）

三个文件与服务器路径一一对应：

| 本仓库 | 服务器路径 |
|---|---|
| `higgsfield2api.1task.cn.conf` | `/www/server/panel/vhost/nginx/higgsfield2api.1task.cn.conf` |
| `higgsfield2api.1task.cn-ssl.conf` | `/www/server/panel/vhost/nginx/higgsfield2api.1task.cn-ssl.conf` |
| `higgsfield2api-proxy.inc` | `/www/server/panel/vhost/nginx/higgsfield2api-proxy.inc` |

## 前置

1. 容器已在 `127.0.0.1:39420` 监听（`cd /opt/higgsfield2api && docker compose up -d`）。
2. DNS：`higgsfield2api.1task.cn` A 记录 → 本机公网 IP（HTTP-01 签发硬依赖）。

## 安装

```bash
cd /path/to/repo
scp nginx/higgsfield2api* ai-prod:/www/server/panel/vhost/nginx/
ssh ai-prod 'nginx -t && systemctl reload nginx'
```

## 证书（acme.sh / Let's Encrypt，ec-256）

```bash
ssh ai-prod '
D=/www/server/panel/vhost/cert/higgsfield2api.1task.cn
~/.acme.sh/acme.sh --issue -d higgsfield2api.1task.cn --webroot /www/wwwroot/acme --keylength ec-256
~/.acme.sh/acme.sh --install-cert -d higgsfield2api.1task.cn --ecc \
  --key-file       $D/privkey.pem \
  --fullchain-file $D/fullchain.pem \
  --reloadcmd      "nginx -s reload"
'
```

`--issue` 之前若目录里已有自签占位证书，`--install-cert` 会直接覆盖，无需手工删。

## 验证

```bash
curl -s  -o /dev/null -w '%{http_code}\n' http://higgsfield2api.1task.cn/healthz    # 301
curl -s  https://higgsfield2api.1task.cn/healthz                                    # {"ok":true,...}
curl -s  -o /dev/null -w '%{http_code}\n' https://higgsfield2api.1task.cn/tasks      # 401（鉴权生效）
curl -sI https://higgsfield2api.1task.cn/healthz | grep -i strict-transport          # 无 HSTS 属预期
```

## 注意

- `limit_req_zone` 写在 `-ssl.conf` 的 **server 块之外**：该文件被 include 在 http 块里，所以合法；zone 名
  `hf2api` 带前缀防与其它站点重名。
- 入口层**不做认证**，鉴权在服务侧 `SERVICE_TOKEN`。若哪天要在入口加 IP 白名单，加在这个文件的 `location /` 里，
  不要改服务代码。
