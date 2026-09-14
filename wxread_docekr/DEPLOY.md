# WxReadAssistant Docker v2.4.0 部署指南

## 架构

```
NAS (飞牛 fnOS / Debian 12 / Docker 28.5.2)
└── 容器 wxreadassistant
    ├── Python 3.14 + FastAPI + Uvicorn :5911
    ├── Playwright (headless chromium) 扫码登录
    └── /data (volume: 配置/日志/数据库)
        └── 仅绑定到 127.0.0.1:5911（宿主机回环）
```

## 安全红线

> **端口仅发布到宿主机回环 `127.0.0.1:5911`**，局域网不可直接访问。
> 对外访问通过反向代理（如 Caddy/Nginx）加 TLS + Basic Auth 转发。

`docker-compose.yml` 中端口映射已固定为 `127.0.0.1:5911:5911`，请勿改为 `0.0.0.0`。

## 前置条件

1. NAS 已安装 Docker 及 docker compose
2. NAS 用户已加入 `docker` 组（或可 sudo 执行 docker 命令）
3. SSH 可登录 NAS

## 部署步骤

### 1. 上传项目到 NAS

```bash
# 在本机执行
python devtools/nas.py putdir . /vol1/1000/wxreadassistant
```

### 2. 构建镜像

```bash
# NAS 上执行（国内加速）
cd /vol1/1000/wxreadassistant
export HOME=/tmp
docker compose build \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
```

构建约 3-5 分钟（含 chromium 运行库和字体安装）。

### 3. 配置环境变量

创建 `docker-compose.override.yml`（不修改交付文件，compose 自动合并）：

```yaml
services:
  wxread:
    environment:
      WXREAD_WEB_TOKEN: "<你的访问令牌>"      # 必填，浏览器登录用
      WXREAD_LICENSE_CODE: "<授权码>"          # 必填，激活用
```

生成授权码（在本机项目目录执行）：

```bash
python -c "import sys; sys.path.insert(0,'.'); from app.core.licensing import generate_code; print(generate_code())"
```

### 4. 启动容器

```bash
cd /vol1/1000/wxreadassistant
export HOME=/tmp
docker compose up -d
```

### 5. 验证

```bash
# 健康检查
curl http://127.0.0.1:5911/healthz

# 版本
curl http://127.0.0.1:5911/api/version

# 授权状态（替换 YOUR_TOKEN）
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:5911/api/license/status
```

容器约 10 秒进入 `healthy` 状态（HEALTHCHECK 检查 heartbeat 文件）。

## 日常使用

### 浏览器访问

1. SSH 端口转发：`ssh -L 15911:127.0.0.1:5911 user@nas-ip`
2. 浏览器打开 `http://127.0.0.1:15911`
3. 输入 `WXREAD_WEB_TOKEN` 令牌保存

### 手机扫码登录

1. 在面板「扫码登录」区点击「获取二维码」
2. 二维码图片会显示在页面中
3. 手机微信「扫一扫」→ 扫描屏幕上的二维码 → 手机上确认登录
4. 也可长按图片保存到相册，再到微信「扫一扫 → 相册」选中二维码

> 二维码有效期约 90 秒，超时后点击「重新获取」即可。

### 配置 Skill API Key

在面板「配置」Tab → Skill 区填入 API Key，点保存后在「维护」区点「验证 Skill」。

### 配置 WxPusher 推送

推送分两条通道，在面板「配置」Tab → 推送区填写：

- **文本通道（SPT）**：填 SPT 令牌（wxpusher.zjiecode.com 首页扫码获取），用于日常文字通知。
- **图片通道（AppToken + UID）**：Cookie 失效自动推送登录二维码必须配置。
  1. 打开 wxpusher.zjiecode.com/admin 微信扫码登录 → 创建应用 → 复制 `AT_` 开头的 appToken
  2. 手机微信扫码**关注该应用**（应用详情页的应用二维码，不关注收不到消息）
  3. 公众号「wxpusher」→ 我的 → 我的UID，复制 `UID_` 开头的 UID

填写后保存，在「维护」区点「测试推送」验证两条通道。

### Cookie 失效自动扫码推送（图片通道）

登录态失效时（调度器启动检测或巡检发现硬失效），助手自动在容器内唤起无头浏览器生成登录二维码，并推送到微信：手机**长按二维码图片 → 识别 → 确认登录**，新登录态自动写入，调度器自动恢复阅读，全程无需打开管理面板。

- 开关：推送区「失效后自动推扫码二维码」（默认开）
- 冷却：「自动扫码冷却(分钟)」（默认 30，防登录态抖动轰炸）
- 未配图片凭证时自动降级为 SPT 文字提醒；手动扫码会话进行中时自动唤起让位

## 运维命令

```bash
# 查看日志
docker logs -f wxreadassistant

# 重启
docker compose restart

# 停止（优雅停机，≤40s）
docker compose stop

# 更新代码后重建
docker compose up -d --build

# 清理旧镜像
docker image prune
```

## 数据持久化

| 路径（容器内） | 用途 | 持久化 |
|---|---|---|
| `/data` | 配置、日志、数据库 | Docker volume `wxread_data` |

重建镜像不会丢失数据（volume 独立于镜像层）。

## 配置文件结构（/data/WxReadAssistant/config.json）

```
reading      阅读参数（min_hours/max_hours/min_interval_sec 等）
risk         风控参数（fail_pause_threshold/fail_streak_threshold）
skill        Skill API（api_key/version）
push         WxPusher（wxpusher_spt + 6 个通知开关）
app          应用参数（log_retention_days）
```

保护段（不可通过 PUT 修改）：`cookies` / `cookies_raw` / `headers` / `scheduler` / `daily_plan` / `risk_alerts`

## 端口绑定策略

| 条件 | 绑定地址 | 安全保障 |
|---|---|---|
| 设了 WXREAD_WEB_TOKEN | 0.0.0.0 | compose 只发布到 127.0.0.1 |
| 容器内 (WXREAD_IN_CONTAINER=1) | 0.0.0.0 | 同上 |
| 裸机无 token | 127.0.0.1 | 本机回环 |

启动日志会用红字警告绑定地址。

## 常见问题

**Q: 容器启动后访问不到？**
A: compose 端口映射是 `127.0.0.1:5911`，需通过 SSH 转发或反代访问，不支持直接 `NAS_IP:5911`。

**Q: 扫码提示"浏览器启动失败"？**
A: 容器内 chromium 运行库缺失，检查 Dockerfile 是否包含 `apt-get install` 运行库步骤。

**Q: Skill 验证失败？**
A: 确认 API Key 格式正确且未过期。在容器内测试：
```bash
docker exec wxreadassistant python -c "import sys; sys.path.insert(0,'/app'); from app.core.skill_api import SkillAPI; from app.core.config import ConfigStore; s=SkillAPI(ConfigStore()); print(s.verify_api_key())"
```

**Q: WxPusher 收不到推送？**
A: 确认 SPT 令牌有效且已关注 WxPusher 服务号。

**Q: 重建后数据丢失？**
A: 不会。数据在 `wxread_data` volume 中，重建镜像不清理 volume。除非手动 `docker compose down -v`。
