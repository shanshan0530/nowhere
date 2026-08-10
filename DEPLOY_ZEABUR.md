# 在 Zeabur 部署 Nowhere

这个仓库已经带有根目录 `Dockerfile`。Fork 后不需要修改源码，也不需要手动填写启动命令。

## 最短流程

1. Fork 本仓库到你自己的 GitHub 账号。
2. 在 Zeabur 新建服务，并选择你 Fork 后的仓库。
3. 在服务的环境变量中新增：

   ```text
   NOWHERE_MCP_TOKEN=你自己生成的一段足够长的随机字符串
   ```

4. 部署并绑定域名。
5. MCP 地址为：

   ```text
   https://你的域名/mcp
   ```

6. 客户端请求使用：

   ```text
   Authorization: Bearer 你的_NOWHERE_MCP_TOKEN
   ```

每个部署都应该生成自己的 Token。不要复制别人的 Token，也不要把真实 Token 写进仓库、README、截图或 `.env.example`。

## 可选：天气 API

如果有和风天气 API Key，可以再设置：

```text
NOWHERE_QWEATHER_KEY=你的_Key
```

不设置也能运行；Nowhere 会使用离线数据和气候估算作为兜底。

## 可选：保存旅程状态

Dockerfile 默认把 `NOWHERE_HOME` 指向 `/data`。如果希望重新部署或重启后仍保留旅程、标记和明信片，请在 Zeabur 为服务挂载持久化 Volume 到：

```text
/data
```

如果不挂 Volume，服务本身仍可使用，但容器重建后本地旅程数据可能丢失。

## 安全提醒

不要在公网部署中设置：

```text
NOWHERE_ALLOW_UNAUTHENTICATED_HTTP=true
```

该选项只适用于你明确控制的私有网络。公网部署应始终使用 `NOWHERE_MCP_TOKEN`。
