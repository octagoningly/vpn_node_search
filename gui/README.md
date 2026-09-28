# NodeBench Desktop

一键优选节点测评桌面工具。

## 别人怎么用（普通用户）

1. 下载 `NodeBench` 文件夹（内含 `NodeBench.exe`）
2. 双击 `NodeBench.exe`
3. 首次会出现 **设置向导**：
   - 创建 GitHub 仓库（托管优选地址）
   - 填 GitHub Token / AbuseIPDB / IPinfo 密钥
   - 把 raw 链接填进 edgetunnel「自定义优选」
   - 说明测速二进制（tools/）
4. 主界面点 **开始运行**
5. 运行完在 edgetunnel 点「开始优选」即可

## 主界面

| 页面 | 作用 |
|---|---|
| 运行 | 一键测速 + 日志 |
| 评分与筛选 | 权重 / 速度门槛 / 国家白名单 |
| 导出格式 | 备注模板 `速度-纯净度-稳定性-国家` |
| 密钥与账号 | 本机 .env，不上传 |

## 开发者打包 exe

```powershell
powershell -ExecutionPolicy Bypass -File gui/build_exe.ps1
```

产物在 `dist/NodeBench/NodeBench.exe`。把整个 `dist/NodeBench` 文件夹压缩发给别人即可。

## 备注格式

```
104.17.29.227:8443#3.9-1.00-1.00-美国
                  │    │    │    └ 国家（IPinfo）
                  │    │    └ 稳定性 0-1
                  │    └ 纯净度 0-1（AbuseIPDB+IPinfo）
                  └ 速度 MB/s
```
