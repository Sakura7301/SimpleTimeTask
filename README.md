# SimpleTimeTask 插件

## 简介
**SimpleTimeTask** 是一个基于 chatgpt-on-wechat 的插件，用于触发定时任务，感谢[timetask](https://github.com/haikerapples/timetask)项目提供的帮助，部分实现参考于此。


## 更新日志

**V1.0.1 (2025-01-12)**
- 2025.1.12：新增多种任务频率，如：每周一，不含周日，每月1号，详见帮助文档。


## 安装
- 方法一：
  - 载的插件文件都解压到`plugins`文件夹的一个单独的文件夹，最终插件的代码都位于`plugins/PLUGIN_NAME/*`中。启动程序后，如果插件的目录结构正确，插件会自动被扫描加载。除此以外，注意你还需要安装文件夹中`requirements.txt`中的依赖。

- 方法二（推荐）：
  - 借助`Godcmd`插件，它是预置的管理员插件，能够让程序在运行时就能安装插件，它能够自动安装依赖。
    - 使用 `#installp git@github.com:Sakura7301/SimpleTimeTask.git` 命令自动安装插件
    - 在安装之后，需要执行`#scanp`命令来扫描加载新安装的插件。
    - 插件扫描成功之后需要手动使用`#enablep SimpleTimeTask`命令来启用插件。

## 功能

- **添加任务**：用户可以设置特定时间的任务提醒。
- **查看任务列表**：用户可以查看已设置的所有定时任务。
- **取消任务**：用户可以取消不再需要的任务。
- **支持群聊和私聊**：可在群聊和私聊中均可使用。

## 使用说明

### 指令格式

用户可以通过以下格式发送消息来控制插件：

1. **查看任务列表**

   ```
   /time 任务列表
   ```

2. **取消任务**

   ```
   /time 取消任务 <任务ID>
   ```

3. **添加任务**

   ```
   /time <频率> <时间> <内容> <group[群标题]>
   ```

   例如：
   - `/time 今天 17:00 提醒喝水`
   - `/time 每天 18:00 GPT 提醒运动`
   - `/time 明天 9:00 提醒开会 group[办公室]`
   - `/time 今天 17:00 提醒喝水`
   - `/time 今天 17:00 GPT 提醒喝水`
   - `/time 每周日 08:00 GPT 提醒我逛超市`
   - `/time 不含周日 08:55 摸鱼`
   - `/time 每月10号 17:00 GPT 提醒我存钱`

注意：如果本月没有指定的日期，任务会在本月的最后一天触发。

### 频率参数

- 今天
- 明天
- 每天
- 工作日

### 注意事项

- 确保任务时间的有效性，插件会在处理时进行验证。
- 发送指令的用户需要具有相应的权限。
- 取消任务时，请提供有效的任务 `ID`。

## 数据库

插件会自动创建一个 SQLite 数据库，用于持久化保存任务信息。数据库文件位于 `plugins/SimpleTimeTask/simple_time_task.db`。用户可以根据需要手动查看或修改数据库内容。

### 数据表结构

```
CREATE TABLE IF NOT EXISTS tasks (
   id TEXT PRIMARY KEY,
   time TEXT NOT NULL,
   frequency TEXT CHECK(frequency IN ('once', 'work_day', 'every_day')),
   content TEXT NOT NULL,
   target_type INTEGER DEFAULT 0,
   user_id TEXT,
   user_name TEXT,
   user_group_name TEXT,
   group_title TEXT,
   is_processed INTEGER DEFAULT 0
)
```

## 错误处理

如果在使用过程中出现错误，插件会记录错误日志。用户可以通过检查日志来获取详细的错误信息。

## 帮助指令

要获取插件的帮助信息，可以使用以下指令：

```
#help SimpleTimeTask
```

输入该指令后，插件会返回帮助文本，包含使用方法及示例。

## 记录日志
本插件支持日志记录，所有请求和响应将被记录，方便调试和优化。日志信息将输出到指定的日志文件中，确保可以追踪插件的使用情况。

## 贡献
欢迎任何形式的贡献，包括报告问题、请求新功能或提交代码。你可以通过以下方式与我们联系：

- 提交 issues 到项目的 GitHub 页面。
- 发送邮件至 [sakuraduck@foxmail.com]。

## 赞助
开发不易，我的朋友，如果你想请我喝杯咖啡的话(笑)

<img src="https://github.com/user-attachments/assets/db273642-1787-4195-af52-7b14c8733405" alt="image" width="300"/> 

## 许可
此项目采用 Apache License 版本 2.0，详细信息请查看 [LICENSE](LICENSE)。
