# ShopLite 部署与验收

## 本地开发（PyCharm / shoplite 环境）

项目 SDK 使用 `E:\envs\shoplite\python.exe`（Python 3.11.15），数据库统一使用 MySQL 8，不再使用 SQLite。

### 1. 配置 PyCharm

1. 打开项目根目录（包含 `manage.py` 的目录）。
2. 在 `Settings > Project > Python Interpreter` 选择现有解释器 `E:\envs\shoplite\python.exe`。
3. 如果 PyCharm 显示旧名称“Python 3.8 (shoplite)”，以解释器路径和下面命令输出为准，名称只是本地标签：

```powershell
E:\envs\shoplite\python.exe --version
```

### 2. 安装依赖

首次运行或 `requirements.txt` 更新后执行：

```powershell
E:\envs\shoplite\python.exe -m pip install -r requirements.txt
E:\envs\shoplite\python.exe -m pip check
```

### 3. 准备 MySQL

确认 Windows 的 `MySQL` 服务已启动，并创建 UTF-8 数据库（已存在时可以跳过创建）：

```powershell
Get-Service MySQL
mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS shoplite CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

从 `.env.example` 复制一份 Git 忽略的 `.env`，填写本机数据库账号：

```powershell
Copy-Item .env.example .env
```

```dotenv
DATABASE_URL=mysql://root:你的本机密码@127.0.0.1:3306/shoplite
```

密码包含 `@`、`:`、`/` 等字符时，需要先进行 URL 编码。真实密码只能放在 `.env` 或服务器密钥管理中，不要写入源码、文档或 Git。

### 4. 初始化并启动

```powershell
E:\envs\shoplite\python.exe manage.py migrate
E:\envs\shoplite\python.exe manage.py seed_demo
E:\envs\shoplite\python.exe manage.py runserver
```

其中 `seed_demo` 只用于本地验收，可重复执行；它会按业务标识准备演示数据，不会清空已有 MySQL 数据。正常开发启动后访问：

- 商城：`http://127.0.0.1:8000/`
- 登录：`http://127.0.0.1:8000/accounts/login/`
- 管理后台：`http://127.0.0.1:8000/admin/`
- 健康检查：`http://127.0.0.1:8000/health/`

### 5. 本地演示账号

- 管理员：`shoplite_admin` / `ShopLiteAdmin!2026`
- 个人用户：`buyer_01`、`buyer_02`、`buyer_03` / `ShopLiteTest!2026`
- 对应手机号：`13800000001`、`13800000002`、`13800000003`

管理员账号用于 `/admin/`，个人账号用于商城登录页；个人账号既可输入用户名，也可输入对应手机号和密码登录。账号定义可在 `shop/management/commands/seed_demo.py` 查看。

这些密码是公开的本地演示密码，不能用于生产。`seed_demo` 在生产环境默认拒绝运行；正式上线前应删除演示账号，或在后台为其设置不可用，并单独创建强密码管理员：

```powershell
E:\envs\shoplite\python.exe manage.py createsuperuser
```

### 6. 常用检查

```powershell
E:\envs\shoplite\python.exe manage.py check
E:\envs\shoplite\python.exe manage.py makemigrations --check --dry-run
E:\envs\shoplite\python.exe manage.py test shop.tests shop.test_extended
```

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
