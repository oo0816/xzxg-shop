# 原版平台本地启动与审查记录

当前目录是用户 fork `https://github.com/oo0816/xzxg-shop` 的工作副本，基线为 `cabd0a1b01abac05f666c522d238c6c1ce092815`。初次以 GitHub API 源码包获取，随后通过系统现有代理恢复真实 Git 元数据。当前为浅克隆，未提交、未推送；`origin` 指向用户 fork，`upstream` 指向 `LYP-leo/xzxg-shop`。原作者包名、仓库说明和代码保留。

## 启动和停止

在 `C:\Users\13077\Desktop\project-agent\xzxg-shop` 打开 PowerShell：

```powershell
python scripts/local_dev.py status
python scripts/local_dev.py start
# 使用完后：
python scripts/local_dev.py stop
```

已经在运行时无需再次 start。停止只针对启动器记录且核实过程序路径的前后端，以及新建的本地 MySQL；数据库文件保留。

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 原 React 管理/商家后台 | http://127.0.0.1:5173 | Vite 代理连接原 Go API |
| 原 Go API | http://127.0.0.1:8080/api/v1/health | 仅本机监听 |
| 新独立 MySQL | 127.0.0.1:3307 | 数据库 xzxg_shop_local |
| 原有 Python 导购页面 | http://127.0.0.1:8000 | 独立保留，尚未接入原平台 |

演示后台账号来自原项目：`admin / admin123456`、`merchant / merchant123456`。普通用户 API 演示账号为 `user / user123456`。原 React 页面主要面向管理员和商家，不能当成完整消费者商城；原项目消费者入口以 Android 为主，本次没有构建 Android。

## 环境

- Go 1.27.3 官方 Windows ZIP，校验 SHA256 后放在 `.local/go`，不改系统 PATH。
- MySQL 8.4.13 官方 Windows ZIP，核对官方摘要后放在 `.local/mysql-8.4.13-winx64`。新数据在 `.local/mysql-data`，3307端口，仅本机访问；没有改动遗留 MySQL80 服务或旧数据目录。
- 专用数据库用户只拥有新数据库权限。随机数据库凭据位于 `.local/connection.json`，管理账户位于 `.local/mysql-admin.ini`；`.local/` 已加入忽略规则，不要提交这些文件。
- Node.js 使用本机原有环境，前端依赖通过原 package-lock 执行 npm ci 安装。
- 只为此仓库设置 Git 的 http.proxy，地址来自系统已有代理。代理变更后需要调整该仓库配置，不影响全局 Git。

Nacos 未启动，原程序回退内存默认配置；本阶段不启动 Redis、Milvus、MinIO 等完整中间件。启动器清空子进程模型密钥、关闭 BOOTSTRAP_VECTOR_INDEX，因此这是无付费模型调用的电商业务基线，不是原版 AI 效果验收。

原导入命令已将294个商品、973个SKU导入新库，另保留3个内置演示商品。导入生成1121份文档、1962个知识片段；没有调用 Embedding 建索引。与独立 Python 导购的3229个片段不是同一套切分结果，后续接入时需要明确使用哪套检索数据。

## 验证与改动

1. 原 Go 后端 `go test ./...` 通过，API 已构建运行。
2. 原 React `npm run build` 通过。
3. 无头 Chrome 验证管理员登录、商品巡检、退出、商家登录和商品预览，未发现未捕获页面异常。记录在 `.local/original-browser.json`。
4. 真实本地 HTTP 验证三类演示账号登录、新用户购物车、指定SKU加购、修改数量、优惠预览、创建待支付订单、读取及取消订单。库存闭环覆盖商品总库存和具体SKU库存。没有调用支付端点或付费模型。
5. 发现商品分页总数为0但实际返回商品：`ListProductsPage` 的计数语句替换掉了包含 FROM/JOIN 的整个查询头。已提取共享 `productCardFrom()`，让计数和列表使用同样的连接关系；实际分页验收覆盖全部唯一商品数。
6. 修复取消及支付超时只改订单状态、不释放库存的问题。库存释放与订单状态迁移处于同一数据库事务，并通过订单行锁保证取消、支付和超时分支只能有一个成功迁移。
7. 压力测试发现并修复两个并发问题：`UnixNano` 单独生成主键在 Windows 高并发下可能碰撞；超时扫描通过状态索引执行 `FOR UPDATE`，会与取消事务形成反向锁序。当前 ID 增加原子序列，超时扫描改为先读取候选、再逐单按主键锁定和复核。

本地脚本：

```powershell
python scripts/smoke_local.py
python scripts/stress_stock_race.py
node scripts/check_original_browser.cjs
```

`smoke_local.py` 会先收敛历史过期订单，再新建本地测试账号，分别验证取消和超时关单的库存回补。它还会重复取消、重复触发过期，确认不会重复增加库存。报告在 `.local/original-smoke.json`；任一断言失败都会返回非零退出码。浏览器脚本需要本机 Chrome/Edge 和支持原生 WebSocket 的 Node。

`stress_stock_race.py` 使用专用临时商品执行40单支付/取消竞态和20单并发超时扫描，验证每单只有一个终态胜出、库存只回补一次。测试结束会清理专用商品、账号和订单，报告写入 `.local/stock-race-report.json`。支付动作是项目内置的本地虚拟支付，不连接真实支付网关。

## 已修复：取消与超时关单释放库存

`CreateOrderFromCart` 仍按原逻辑预扣商品和具体SKU库存。新增的 `releaseOrderStock` 根据订单明细恢复相同数量，供用户取消、惰性超时关单以及支付锁内的截止时间边界共同调用。

取消和超时会先锁定仍为 `pending_payment` 的订单，再在同一事务中完成库存回补、订单状态更新和支付状态更新。订单一旦离开待支付状态，重复请求不能再次回补。真实数据库冒烟测试每轮28项检查全部通过；60单并发测试连续两次完整通过。测试未连接真实支付网关，也未调用模型API。

## 下一步

当前库存闭环已修复并验证。下一步可以确定 Python 导购与 Go 商品/SKU/购物车接口的边界。当前尚未替换原 ReAct、未向原平台配置任何模型密钥，也未声称已有 LangGraph 电商集成。
