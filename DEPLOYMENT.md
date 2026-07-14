# ShopLite 部署与验收

## 本地开发（PyCharm / shoplite 环境）

项目 SDK 使用 `E:\envs\shoplite\python.exe`（Python 3.11）。本地默认使用已被 Git 忽略的 SQLite，无需先创建 `.env`：

```powershell
E:\envs\shoplite\python.exe manage.py migrate
E:\envs\shoplite\python.exe manage.py seed_demo
E:\envs\shoplite\python.exe manage.py runserver
```

演示账号：

- 管理员：`shoplite_admin` / `ShopLiteAdmin!2026`
- 个人用户：`buyer_01`、`buyer_02`、`buyer_03` / `ShopLiteTest!2026`
- 对应手机号：`13800000001`、`13800000002`、`13800000003`

这些账号只用于本地验收。`seed_demo` 在生产环境默认拒绝运行。

## Docker 部署

1. 从 `.env.example` 复制 `.env`。
2. 至少修改 `SECRET_KEY`、MySQL 两个密码、域名、短信参数。
3. 设置：

```dotenv
PRODUCTION=True
DEBUG=False
ALLOWED_HOSTS=shop.example.com
CSRF_TRUSTED_ORIGINS=https://shop.example.com
SMS_PROVIDER=tencent
ENABLE_MOCK_PAYMENT=False
WECHAT_LOGIN_MODE=disabled
```

4. 启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web
```

Web 容器会自动执行迁移和 `collectstatic`。`/health/` 用于健康检查。

## HTTPS

生产设置会强制 HTTPS、安全 Cookie 与 HSTS。推荐由云负载均衡、CDN 或外层网关终止 TLS，并把 `X-Forwarded-Proto: https` 转发给本项目 Nginx。首次正式上线前确认 HTTPS 全站可用，再启用 `PRODUCTION=True`。

## 登录与支付接入状态

- 手机短信：已实现腾讯云短信通道；生产必须填写腾讯云参数。
- 微信登录：入口预留，默认关闭；获得开放平台资质后配置 allauth SocialApp，并将 `WECHAT_LOGIN_MODE=allauth`。
- 支付宝/微信支付：界面和订单字段已预留，但在完成官方 SDK 验签、退款 API 和密钥配置前保持关闭。
- 模拟支付：仅本地开发使用；生产必须设置 `ENABLE_MOCK_PAYMENT=False`。

## 上线前命令

```bash
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
python manage.py test
python manage.py collectstatic --noinput
```

数据库、媒体文件和 `.env` 必须纳入服务器备份，但不要提交到 Git。
