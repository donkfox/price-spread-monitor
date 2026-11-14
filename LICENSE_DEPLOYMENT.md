# 授权与链动小铺发售

当前软件支持按时间授权，不提供买断：

| 链动小铺商品 | 授权天数 | 建议售价 |
|---|---:|---:|
| 价差监控 1 天体验 | 1 天 | ¥4.8 |
| 价差监控月卡 | 30 天 | ¥88 |
| 价差监控季卡 | 90 天 | ¥238 |
| 价差监控年卡 | 365 天 | ¥798 |

## 一、准备授权服务器

服务器只需要 Python 3.11、一个域名和 HTTPS。服务器上不要放客户软件压缩包里的任何密钥。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r license-server-requirements.txt
$env:LICENSE_DB = "D:\license-data\licenses.sqlite3"
$env:LICENSE_ADMIN_KEY = "换成一串随机的管理员密钥"
uvicorn license_server.api:app --host 127.0.0.1 --port 8080
```

生产环境应由 Nginx、Caddy 或云服务器网关提供 HTTPS，再把 `/` 转发到本机的 8080 端口。客户软件只填写 `https://你的域名`，不要把 8080 端口直接暴露到公网。

## 二、生成链动小铺自动发货码

在授权服务器项目目录执行，明文激活码只会输出到你指定的 CSV；数据库只保存哈希值。

```powershell
python -m license_server.admin --database D:\license-data\licenses.sqlite3 generate --days 1 --count 100 --output D:\license-data\trial-1day.csv --batch "4.8元体验"
python -m license_server.admin --database D:\license-data\licenses.sqlite3 generate --days 30 --count 100 --output D:\license-data\monthly-30day.csv --batch "月卡"
python -m license_server.admin --database D:\license-data\licenses.sqlite3 generate --days 90 --count 50 --output D:\license-data\quarterly-90day.csv --batch "季卡"
python -m license_server.admin --database D:\license-data\licenses.sqlite3 generate --days 365 --count 20 --output D:\license-data\annual-365day.csv --batch "年卡"
```

在链动小铺分别创建四个数字商品，把对应 CSV 中的激活码作为自动发货库存导入。订单付款后，链动小铺发给客户一条激活码；目前不依赖链动小铺的回调接口，所以不会因为接口变化导致发码中断。

## 三、构建正式客户包

个人自用版仍然不校验授权，但只能使用明确的个人版 spec 构建，不能调用客户发布脚本：

```powershell
python -m PyInstaller --noconfirm PriceMonitorCenter.spec
python -m PyInstaller --noconfirm PriceMonitorService.spec
```

正式客户包必须传入固定 HTTPS 授权地址和四个链动小铺商品链接。缺少任一参数都会停止构建：

```powershell
.\build-release.ps1 `
  -LicenseServerUrl "https://example.com" `
  -TrialPurchaseUrl "https://链动小铺的1天体验商品链接" `
  -MonthlyPurchaseUrl "https://链动小铺的30天商品链接" `
  -QuarterlyPurchaseUrl "https://链动小铺的90天商品链接" `
  -AnnualPurchaseUrl "https://链动小铺的365天商品链接"
```

服务器私钥、管理员密钥、SQLite 数据库和激活码 CSV 都不会进入客户 ZIP。客户在“授权与续费”中点击套餐后，软件会显示对应链动小铺付款二维码；付款后输入自动发货的激活码即可激活。同一激活码只能绑定一台电脑，续费码会叠加当前剩余时间。

正式客户版忽略本地配置中的授权开关、服务器地址、证书路径和授权文件路径。授权令牌使用 Windows DPAPI 绑定当前 Windows 用户加密保存，不会以明文写入磁盘；启动监控必须在线校验，运行中每 5 分钟复核一次，授权服务器临时故障最多宽限 15 分钟。

链动小铺公开资料未提供可确认的支付回调接口，因此当前由链动小铺完成收款和自动发码，软件不能自动查询订单是否支付。若商家后台提供“异步通知地址、接口密钥、签名规则”，可再把支付成功回调接入授权服务器，实现付款后软件自动刷新授权。

## 四、售后命令

```powershell
python -m license_server.admin --database D:\license-data\licenses.sqlite3 list
python -m license_server.admin --database D:\license-data\licenses.sqlite3 extend --license-id 12 --days 7
python -m license_server.admin --database D:\license-data\licenses.sqlite3 revoke --license-id 12
python -m license_server.admin --database D:\license-data\licenses.sqlite3 enable --license-id 12
python -m license_server.admin --database D:\license-data\licenses.sqlite3 unbind --license-id 12
```

`unbind` 会停用旧电脑并生成一条剩余天数的迁移码，售后把这条新码发给客户即可换电脑。客户授权过期后仍能查看报警历史、订单记账和导出 Excel，但不能开启新的后台扫描或接收新的扫描报警。
