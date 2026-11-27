# higgsfield2api 反代（回环；服务侧**自带 SERVICE_TOKEN 鉴权**，见 DEPLOY.md §11）
proxy_pass http://127.0.0.1:39420;
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_buffering off;          # 建任务是同步阻塞上游（最长 ~60s），不能攒包
proxy_read_timeout 180s;
proxy_send_timeout 180s;
client_max_body_size 8m;      # 只收 JSON，素材走 asset:// ID
