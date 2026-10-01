# jmcomic 插件说明文档

> 适用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的禁漫天堂（JMComic）插件，支持通过指令搜索、下载漫画（自动合并成 PDF 发送）、查看排行榜和标签。

> 本项目 Fork 自原作者 iamfromchangsha 的 [astrbot_plugin_JMCOMIC](https://github.com/iamfromchangsha/astrbot_plugin_JMCOMIC)，在此基础上进行了 PDF 合并发送、内存与并发调优等改进。


## ✨ 功能特性

- **`/jm <漫画ID>`**：根据漫画 ID 下载整本漫画，合并成 PDF 后发送。
- **`/jm 暂停`**：暂停当前正在进行的下载，并清理服务器上的临时文件。
- **`/jms <关键词> [页码]`**：根据关键词搜索漫画，支持指定页码，默认为第 1 页。
- **`/jmtag <漫画ID>`**：查询指定漫画 ID 的标题、作者和标签。
- **`/jmmr [页码]`**：获取月度热门排行榜，默认第 1 页。
- **`/jmwr [页码]`**：获取周度热门排行榜，默认第 1 页。
- **`/jmhelp`**：显示插件的帮助信息。


## 📦 安装

1. 确保已安装 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。
2. 将本插件文件夹放入 AstrBot 的 `data/plugins` 目录中。
3. 安装依赖：
   ```bash
   pip install jmcomic pyyaml img2pdf
   ```
4. 配置文件是本插件目录下的 `option.yml`（插件会从自己的目录读取，不依赖启动目录）：
   ```yaml
   plugins:
     after_init:
       - plugin: login          # 登录后可访问受限本子
         kwargs:
           username: 你的账号
           password: 你的密码

   dir_rule:
     base_dir: ./download        # 下载时会被覆盖成每个用户的独立临时目录
   ```

   下面这些配置由下载子进程 `jm_worker.py` 在下载时自动注入，**不需要**写进 `option.yml`：
   - `dir_rule.base_dir`：指向当前用户的临时目录，实现用户隔离。
   - `plugins.after_album`：`img2pdf` 合并成 PDF，文件名规则 `Aid`（即 `{漫画ID}.pdf`），
     且 `delete_original_file: true`（图片只为合并 PDF 而下载，转完即删）。


---

## 🧩 使用说明

### 1. 下载漫画 `/jm`

- **指令格式**：
  ```
  /jm <漫画ID>
  ```
  或
  ```
  /jm 暂停
  ```
- **示例**：
  ```
  /jm 456789
  ```
- **行为**：
  - 插件会从消息中提取第一个整数作为漫画 ID。
  - 自动为用户创建独立的临时下载目录并下载漫画，下载过程中不会阻塞机器人的其他消息。
  - 下载完成后由 `img2pdf` 合并成 `{漫画ID}.pdf` 并发送给用户。
  - 同一用户同一时间只允许一个下载任务，重复发送会提示等待或暂停。
  - PDF 超过 100 MB 时不发送（可用 `main.py` 里的 `MAX_PDF_MB` 调整）。
  - 无论成功、失败还是被暂停，最后都会清空该用户的临时下载目录。
  - 发送 `/jm 暂停` 会立即终止下载子进程并清理文件。
  - 下载与 PDF 合并运行在独立子进程中（Linux 下默认内存上限 1.5GB，
    可用环境变量 `JM_WORKER_MEM_GB` 调整），即使本子超大导致内存暴涨，
    也只会终止那一次下载，不会拖垮机器人本身。

### 2. 搜索漫画 `/jms`

- **指令格式**：
  ```
  /jms <关键词> [页码]
  ```
- **示例**：
  ```
  /jms 全彩 2
  ```
- **行为**：
  - 消息**最后一个**数字作为页码（若无则默认为 1），其余内容作为搜索关键词。
  - 关键词中的数字会被保留，`/jms 催眠術2` 可以正常搜索。
  - 返回该页的漫画列表，格式为 `[ID]: 标题`。

### 3. 查看标签 `/jmtag`

- **指令格式**：
  ```
  /jmtag <漫画ID>
  ```
- **示例**：
  ```
  /jmtag 456789
  ```
- **行为**：
  - 按 ID 直接查询本子详情，返回标题、作者和标签。

### 4. 月度排行榜 `/jmmr` / 周度排行榜 `/jmwr`

- **指令格式**：
  ```
  /jmmr [页码]
  /jmwr [页码]
  ```
- **示例**：
  ```
  /jmmr 3
  ```
- **行为**：
  - 获取禁漫天堂的月度/周度热门排行榜，页码默认为第 1 页。
  - 返回该页的漫画列表，格式为 `[ID]: 标题`。

### 5. 帮助信息 `/jmhelp`

显示所有可用命令及其简要说明。

---

## 📁 目录结构

```
data/plugins/astrbot_plugin_JMCOMIC/
├── main.py
├── jm_worker.py        # 下载子进程：负责「下载 → 合并 PDF」，自带内存上限
├── option.yml          # 本插件唯一的配置文件（账号、线程数等）
└── download/           # 临时下载目录（插件自动创建与清理）
    └── <用户标识>/      # 每个用户一个独立目录，用完即清
```

---

## 🔒 注意事项

- 本插件调用 `jmcomic` 库，账号信息配置在 `option.yml` 中；搜索、标签、排行榜和下载都会使用该账号登录。
- JM 原生图片是 webp 格式。`option.yml` 默认 `download.image.suffix: .jpg`，会把每页转成质量 90 的 JPEG 再嵌入 PDF，体积小、速度快；**不要改回 `.png`**——PNG 重编码会把下载时间和内存占用放大数倍，小内存机器上足以拖垮整个系统。
- 请遵守当地法律法规，合理使用本插件。
- 频繁请求可能导致 IP 被封，请勿滥用搜索或下载功能。

---

## 🛠 开发者信息

- **插件名称**：jm
- **原作者**：[iamfromchangsha](https://github.com/iamfromchangsha/astrbot_plugin_JMCOMIC)
- **作者**：SpongeFun
- **版本**：1.2.0

---

## 📜 依赖

- `astrbot >= v4.0`
- `jmcomic >= 2.0`
- `pyyaml`
- `img2pdf`
- `Python >= 3.8`

---

## 🌟 Enjoy your reading!

请合法合规使用本插件。
