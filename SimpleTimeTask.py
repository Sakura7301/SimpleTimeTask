# encoding:utf-8
import re
import gc
import time
import random
import plugins
import sqlite3
import datetime
import threading
from plugins import *
from lib import itchat
import config as RobotConfig
from common.log import logger
from bridge.bridge import Bridge
from channel import channel_factory
from bridge.reply import Reply, ReplyType
from bridge.context import ContextType, Context
from channel.chat_message import ChatMessage
from channel.wechat.wechat_channel import WechatChannel
from plugins.SimpleTimeTask.Task import Task


@plugins.register(
    name="SimpleTimeTask",
    desire_priority=100,
    hidden=False,
    desc="一个简易的定时器",
    version="1.0",
    author="Sakura7301",
)
class SimpleTimeTask(Plugin):
    def __init__(self):
        super().__init__()
        try:
            self.config = super().load_config()
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            # 定义数据库路径
            self.DB_FILE_PATH = "plugins/SimpleTimeTask/simple_time_task.db"
            # 创建数据库锁
            self.db_lock = threading.Lock()
            # 初始化数据库并加载任务到内存
            self.tasks = []
            self.init_db_and_load_tasks()
            # 防抖动字典
            self.user_last_processed_time = {}
            # 启动任务检查线程
            self.check_thread = threading.Thread(target=self.check_and_trigger_tasks)
            self.check_thread.daemon = True
            self.check_thread.start()
            # 初始化完成
            logger.info("[SimpleTimeTask] initialized")

        except Exception as e:
            logger.error(f"[SimpleTimeTask] initialization error: {e}")
            raise "[SimpleTimeTask] init failed, ignore "

    def init_db_and_load_tasks(self):
        """ 初始化数据库，创建任务表并加载现有任务 """
        with self.db_lock:
            # 创建数据库连接
            with sqlite3.connect(self.DB_FILE_PATH) as conn:
                cursor = conn.cursor()

                # 检查表是否存在并获取元数据
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks';")
                table_exists = cursor.fetchone() is not None

                if table_exists:
                    # 表存在，检查字段兼容性
                    cursor.execute("PRAGMA table_info(tasks);")
                    columns = cursor.fetchall()
                    column_names = [column[1] for column in columns]  # 提取字段名

                    expected_columns = [
                        'id', 'time', 'frequency', 'content',
                        'target_type', 'user_id', 'user_name',
                        'user_group_name', 'group_title', 'is_processed'
                    ]

                    # 检查字段数量与名称是否兼容
                    if len(column_names) != len(expected_columns) or set(column_names) != set(expected_columns):
                        logger.warning("[SimpleTimeTask] Database schema is incompatible. Dropping and recreating the tasks table.")
                        cursor.execute("DROP TABLE tasks;")

                # 创建数据表（如果不存在）
                cursor.execute('''
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
                ''')

                # 从数据库中加载当前的任务
                cursor.execute('SELECT * FROM tasks')
                # 读取所有任务行
                rows = cursor.fetchall()
                logger.info(f"[SimpleTimeTask] Loaded tasks from database: {rows}")

                # 创建 Task 对象并添加到 self.tasks 列表
                for row in rows:
                    task = Task(
                        task_id=row[0],
                        time_value=row[1],
                        frequency=row[2],
                        content=row[3],
                        target_type=row[4],
                        user_id=row[5],
                        user_name=row[6],
                        user_group_name=row[7],
                        group_title=row[8],
                        is_processed=row[9]
                    )
                    # 添加 Task 实例到 self.tasks 列表
                    self.tasks.append(task)

    def find_user_name_by_user_id(self, msg, user_id):
        """查找指定 UserName 的昵称"""
        user_name = None
        try:
            # 获取成员列表
            members = msg['User']['MemberList']
            # 遍历成员列表
            for member in members:
                # 检查 UserName 是否匹配
                if member['UserName'] == user_id:
                    # 找到昵称
                    user_name =  member['NickName']
        except Exception as e:
            logger.error(f"[DarkRoom] 查找用户昵称失败: {e}")
        return user_name

    def get_group_id(self, group_title):
        # 获取群聊ID
        chatrooms = itchat.get_chatrooms()
        tempRoomId = None
        # 获取群聊
        for chat_room in chatrooms:
            # 根据群聊名称匹配群聊ID
            userName = chat_room["UserName"]
            NickName = chat_room["NickName"]
            if NickName == group_title:
                tempRoomId = userName
                break
        return tempRoomId

    def add_task(self, command_args, user_id, user_name, user_group_name):
        """ 添加任务 """
        # 初始化返回内容
        reply_str = None
        target_type = 0
        with self.db_lock:
            # 获取参数
            frequency = command_args[1]
            time_value = command_args[2]
            content = ' '.join(command_args[3:])

            # 检查频率和时间是否为空
            if len(frequency) < 1 or len(time_value) < 1 or len(content) < 1:
                reply_str = f"[SimpleTimeTask] 任务格式错误: {command_args}\n请使用 '/time 频率 时间 内容' 的格式。"
                logger.warning(reply_str)
                return reply_str

            logger.debug(f"[SimpleTimeTask] {frequency} {time_value} {content}")

            # 解析目标群
            group_title = None
            if command_args[-1].startswith('group['):
                # 获取群聊名称
                group_title = command_args[-1][6:-1]
                # 获取任务内容
                content = ' '.join(command_args[3:-1])

            # 生成任务ID
            task_id = self.generate_unique_id()

            # 处理时间字符串
            if frequency in ["今天", "明天"]:
                # 为一次性任务设置具体时分
                date_str = time.strftime("%Y-%m-%d") if frequency == "今天" else time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
                # 格式化为 年-月-日 时:分
                time_value = f"{date_str} {time_value}"
                frequency = "once"
            elif frequency == "工作日":
                frequency = "work_day"
            elif frequency == "每天":
                frequency = "every_day"

            # 检查任务时间的有效性
            if self.validate_time(frequency, time_value):
                if group_title:
                    target_type = 1
                # 创建任务
                new_task = Task(task_id, time_value, frequency, content, target_type, user_id, user_name, user_group_name, group_title, 0)
                # 将新任务添加到内存中
                self.tasks.append(new_task)
                # 将新任务更新到数据库
                self.update_task_in_db(new_task)
                # 格式化回复内容
                reply_str = f"[SimpleTimeTask] 😸 任务已添加: \n\n[{task_id}] {frequency} {time_value} {content} {'group[' + group_title + ']' if group_title else ''}"
            else:
                reply_str = "[SimpleTimeTask] 添加任务失败，时间格式不正确或已过期."

            # 打印任务列表
            logger.debug(f"[SimpleTimeTask] 任务列表: {self.tasks}")

        return reply_str

    def update_task_in_db(self, task: Task):
        """ 更新任务到数据库 """
        # 由于我们该方法是对任务的插入，因此可以简化锁的使用
        with sqlite3.connect(self.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            # is_processed 默认值设为 0（未处理）
            cursor.execute('''
                INSERT INTO tasks (id, time, frequency, content, target_type, user_id, user_name, user_group_name, group_title, is_processed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (task.task_id, task.time_value, task.frequency, task.content,
                task.target_type, task.user_id, task.user_name,
                task.user_group_name, task.group_title, task.is_processed))
            # 提交更改
            conn.commit()
            logger.info(f"[SimpleTimeTask] Task added to DB: {task.task_id}")

    def show_task_list(self):
        """ 显示所有任务 """
        # 遍历任务列表
        with self.db_lock:
            tasks_list = "[SimpleTimeTask] 😸 任务列表:\n\n"
            for task in self.tasks:
                tasks_list += f"💼[{task.user_name}|{task.task_id}] {task.frequency} {task.time_value} {task.content} {'group[' + task.group_title + ']' if task.target_type else ''}\n"
            return tasks_list
        tasks_list = ""

    def cancel_task(self, task_id):
        """ 取消任务 """
        try:
            with self.db_lock:
                # 检查任务列表是否为空
                if not self.tasks:
                    logger.warning(f"[SimpleTimeTask] No tasks to cancel.")
                    return "[SimpleTimeTask] 没有可取消的任务。"

                deleted = False
                new_tasks = []

                # 遍历当前任务，决定是否删除任务
                for task in self.tasks:
                    if task.task_id == task_id:
                        # 找到并标记为删除
                        deleted = True
                        logger.info(f"[SimpleTimeTask] Task cancelled: {task_id}")
                    else:
                        # 保留其他任务
                        new_tasks.append(task)

                # 更新内存中的任务列表
                self.tasks = new_tasks

                # 更新数据库
                if deleted:
                    self.remove_task_from_db(task_id)
                    return f"[SimpleTimeTask] 😸 任务 [{task_id}] 已取消。"
                else:
                    logger.warning(f"[SimpleTimeTask] Task ID [{task_id}] not found for cancellation.")
                    return f"[SimpleTimeTask] 未找到任务 [{task_id}]."

        except Exception as e:
            logger.error(f"[SimpleTimeTask] Error cancelling task: {e}")
            return "[SimpleTimeTask] 取消任务时发生错误，请稍后重试。"

    def remove_task_from_db(self, task_id):
        """ 从数据库中删除任务 """
        # 确保线程安全
        with sqlite3.connect(self.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
            conn.commit()
            logger.info(f"[SimpleTimeTask] Task removed from DB: {task_id}")

    def is_weekday(self):
        today = datetime.datetime.now()
        # weekday() 返回值：0 = 星期一, 1 = 星期二, ..., 6 = 星期日
        return today.weekday() < 5

    def check_and_trigger_tasks(self):
        """ 定时检查和触发任务 """
        while True:
            # 获取当前时间
            now = time.strftime("%H:%M")
            # 获取今天的日期
            today_date = time.strftime("%Y-%m-%d")

            # 每天重置未处理状态
            if now == "00:00":
                self.reset_processed_status()

            # 遍历副本以便在列表修改时不出错
            for task in self.tasks:
                # 处理时间格式
                try:
                    if task.frequency == "once":
                        # 对于 "once"，使用完整的年-月-日-时-分
                        task_date, task_time = task.time_value.split(' ')
                        if task_date != today_date:
                            # 只触发在当天
                            continue
                        if task_time != now:
                            # 只触发在当前时间
                            continue
                    elif task.frequency == "work_day":
                        # 对于 "work_day"，只在工作日触发，且只使用时-分
                        if not self.is_weekday():
                            # 不是工作日，跳过
                            continue
                        task_time = task.time_value
                        # 若时间不符合或已处理，则跳过
                        if task_time!= now or task.is_processed == 1:
                            continue
                    elif task.frequency == "every_day":
                        # 对于 "every_day"，每天触发，且只使用时-分
                        task_time = task.time_value
                        # 若时间不符合或已处理，则跳过
                        if task_time != now or task.is_processed == 1:
                            continue

                    # 任务触发后处理
                    if task.frequency == "once":
                        # 从内存中移除
                        self.tasks.remove(task)
                        # 从数据库中删除对应的条目
                        self.remove_task_from_db(task.task_id)
                    else:
                        # 将 is_processed 设置为 1
                        task.is_processed =  1
                        # 更新数据库中的状态
                        self.update_processed_status_in_db(task.task_id, 1)

                    # 触发任务
                    self.run_task_in_thread(task)

                except ValueError as e:
                    logger.error(f"[SimpleTimeTask] Time format error for task ID {task.task_id}: {e}")
                    # 删除报错的任务
                    try:
                        self.tasks.remove(task)
                        self.remove_task_from_db(task.task_id)
                    except Exception as e:
                        logger.error(f"[SimpleTimeTask] to delete this task {task.task_id}: {e}")
                except Exception as e:
                    logger.error(f"[SimpleTimeTask] An unexpected error occurred for task ID {task.task_id}: {e}")
                    # 删除报错的任务
                    try:
                        self.tasks.remove(task)
                        self.remove_task_from_db(task.task_id)
                    except Exception as e:
                        logger.error(f"[SimpleTimeTask] to delete this task {task.task_id}: {e}")

            time.sleep(5)  # 5秒检查一次

    def reset_processed_status(self):
        """ 重置所有任务的已处理状态 """
        with self.db_lock:
            for task in self.tasks:
                # 如果 is_processed 为 True
                if task.is_processed == 1:
                    # 重置为 False
                    task.is_processed = 0
                    # 更新数据库中的状态
                    with sqlite3.connect(self.DB_FILE_PATH) as conn:
                        cursor = conn.cursor()
                        cursor.execute('UPDATE tasks SET is_processed = ? WHERE id = ?', (0, task.task_id))
                        conn.commit()
                        logger.info(f"[SimpleTimeTask] Task status updated in DB: {task.task_id} to {0}")

    def update_processed_status_in_db(self, task_id, is_processed):
        """ 更新任务的处理状态到数据库 """
        with sqlite3.connect(self.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE tasks SET is_processed = ? WHERE id = ?', (is_processed, task_id))
            conn.commit()
            logger.info(f"[SimpleTimeTask] Task status updated in DB: {task_id} to {is_processed}")

    def generate_unique_id(self):
        """ 生成唯一任务ID """
        return ''.join(random.choices('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=10))

    def validate_time(self, frequency, time_value):
        """ 验证时间和频率 """
        if frequency not in ["once", "work_day", "every_day"]:
            return False
        # 初始化返回值
        ret = True
        # 获取当前时间
        current_time = time.strftime("%H:%M")

        if frequency == "once":
            # 如果是一次性任务，检查时间格式
            if time_value < f"{time.strftime('%Y-%m-%d')} {current_time}":
                # 今天的时间已过期
                ret = False
        elif frequency == "work_day":
            # 工作日时间检查
            ret = True
        elif frequency == "every_day":
            # 每天的任务可以在任何时间生效
            ret = True

        return ret

    def trigger_task(self, task: Task):
        """ 触发任务的实际逻辑 """
        try:
            # 初始化变量
            content = task.content
            receiver = None
            is_group = False
            is_group_str = "用户消息"
            if task.target_type == 1:
                is_group = True
                receiver = self.get_group_id(task.group_title)
                is_group_str = "群组消息"
            else:
                receiver = task.user_id

            logger.info(f"[SimpleTimeTask] 触发[{task.user_name}]的{is_group_str}: [{content}] to {receiver}")

            # 构造消息
            orgin_string = "id=0, create_time=0, ctype=TEXT, content=/time 每天 17:55 text, from_user_id=@, from_user_nickname=用户昵称, to_user_id==, to_user_nickname=, other_user_id=@123, other_user_nickname=用户昵称, is_group=False, is_at=False, actual_user_id=None, actual_user_nickname=None, at_list=None"
            pattern = r'(\w+)\s*=\s*([^,]+)'
            matches = re.findall(pattern, orgin_string)
            content_dict = {match[0]: match[1] for match in matches}
            content_dict["content"] = content
            content_dict["receiver"] = receiver
            content_dict["session_id"] = receiver
            content_dict["isgroup"] = is_group
            content_dict["ActualUserName"] = task.user_name
            content_dict["from_user_nickname"] = task.user_name
            content_dict["from_user_id"] = task.user_id
            content_dict["User"] = {
                'MemberList': [{'UserName': task.user_id, 'NickName': task.user_name}]
            }

            # 构建上下文
            msg: ChatMessage = ChatMessage(content_dict)
            for key, value in content_dict.items():
                if hasattr(msg, key):
                    setattr(msg, key, value)
            msg.is_group = is_group
            content_dict["msg"] = msg
            context = Context(ContextType.TEXT, content, content_dict)

            # reply默认值
            reply_text = f"[SimpleTimeTask]\n--定时提醒任务--\n{content}"
            replyType = ReplyType.TEXT

            # 以下部分保持不变
            if "GPT" in content:
                content = content.replace("GPT", "")
                reply: Reply = Bridge().fetch_reply_content(content, context)

                # 检查reply是否有效
                if reply and reply.type:
                    # 替换reply类型和内容
                    reply_text = reply.content
                    replyType = reply.type
            else:
                e_context = None
                # 初始化插件上下文
                channel = WechatChannel()
                channel.channel_type = "wx"
                content_dict["content"] = content
                context.__setitem__("content", content)
                logger.info(f"[SimpleTimeTask] content: {content}")
                try:
                    # 获取插件回复
                    e_context = PluginManager().emit_event(
                        EventContext(Event.ON_HANDLE_CONTEXT, {"channel": channel, "context": context, "reply": Reply()})
                    )
                except Exception as e:
                    logger.info(f"路由插件异常！将使用原消息回复。错误信息：{e}")

                # 如果插件回复为空，则使用原消息回复
                if e_context and e_context["reply"]:
                    reply = e_context["reply"]
                    # 检查reply是否有效
                    if reply and reply.type:
                        # 替换reply类型和内容
                        reply_text = reply.content
                        replyType = reply.type

            # 构建回复
            reply = Reply()
            reply.type = replyType
            reply.content = reply_text
            self.replay_use_custom(reply, context)

        except Exception as e:
            logger.error(f"[SimpleTimeTask] 发送消息失败: {e}")

    def run_task_in_thread(self, task: Task):
        """ 在新线程中运行任务 """
        logger.info(f"[SimpleTimeTask] 开始运行任务 {task.task_id}")
        # 控制线程的事件
        task_thread = threading.Thread(target=self.run_with_timeout, args=(task,))
        task_thread.start()
        # 设置超时为60秒
        task_thread.join(timeout=60)

        if task_thread.is_alive():
            logger.warning(f"[SimpleTimeTask] 任务 {task.task_id} 超时结束")
            # 结束线程
            task_thread.join()

    def run_with_timeout(self, task: Task):
        """ 运行任务并捕获异常 """
        try:
            self.trigger_task(task)
        except Exception as e:
            logger.error(f"[SimpleTimeTask] 运行任务时发生异常: {e}")

    def replay_use_custom(self, reply, context : Context, retry_cnt=0):
        try:
            # 发送消息
            channel_name = RobotConfig.conf().get("channel_type", "wx")
            channel = channel_factory.create_channel(channel_name)
            channel.send(reply, context)

            #释放
            channel = None
            gc.collect()

        except Exception as e:
            if retry_cnt < 2:
                # 重试（最多三次）
                time.sleep(3 + 3 * retry_cnt)
                logger.warning(f"[SimpleTimeTask] 发送消息失败，正在重试: {e}")
                self.replay_use_custom(reply, context, retry_cnt + 1)
            else:
                logger.error(f"[SimpleTimeTask] 发送消息失败，重试次数达到上限: {e}")

    def detect_time_command(self, text):
        # 判断输入是否为空
        if not text:
            return None

        # 查找/time在文本中的位置
        time_index = text.find('/time')

        # 如果找到，就返回包含/time之后的文本
        if time_index != -1:
            result = text[time_index:]
            # 包含/time及后面的内容
            return result
        else:
            # 如果没有找到，返回None
            return None

    def on_handle_context(self, e_context: EventContext):
        """ 处理用户指令 """
        # 检查消息类型
        if e_context["context"].type not in [ContextType.TEXT]:
            return

        # 初始化变量
        user_id = None
        user_name = None
        user_group_name = None
        # 获取用户ID
        msg = e_context['context']['msg']
        # 检查是否为群消息
        if msg.is_group:
            # 群消息，获取真实ID
            user_id = msg._rawmsg['ActualUserName']
        else:
            # 私聊消息，获取用户ID
            user_id = msg.from_user_id

        # 获取当前时间（以毫秒为单位）
        current_time = time.monotonic() * 1000  # 转换为毫秒
        # 防抖动检查
        last_time = self.user_last_processed_time.get(user_id, 0)
        # 防抖动间隔为100毫秒
        if current_time - last_time < 100:
            # 如果在100毫秒内重复触发，不做处理
            logger.debug(f"[SimpleTimeTask] Ignored duplicate command from {user_id}.")
            return
        # 更新用户最后处理时间
        self.user_last_processed_time[user_id] = current_time

        # 获取用户指令
        command = self.detect_time_command(msg.content.strip())
        logger.debug(f"[SimpleTimeTask] Command received: {command}")

        # 检查指令是否有效
        if command is not None:
            # 初始化回复字符串
            reply_str = ''
            # 检查是否为群消息
            if msg.is_group:
                # 获取群昵称
                user_name = self.find_user_name_by_user_id(msg._rawmsg, user_id)
                user_group_name = msg.actual_user_nickname
            else:
                # 获取用户昵称
                user_name = msg.from_user_nickname
            logger.info(f"[SimpleTimeTask] 收到来自[{user_name}|{user_group_name}|{user_id}]的指令: {command}")

            # 解析指令
            command_args = command.split(' ')
            if command_args[1] == '任务列表':
                # 获取任务列表
                reply_str = self.show_task_list()
            elif command_args[1] == '取消任务':
                # 取消任务
                if len(command_args) != 3:
                    reply_str = "[SimpleTimeTask] 请输入有效任务ID"
                else:
                    reply_str = self.cancel_task(command_args[2])
            elif command_args[1] in ["今天", "明天", "工作日", "每天"]:
                # 添加任务
                if len(command_args) < 4:
                    reply_str = f"[SimpleTimeTask] 任务格式错误: {command_args}\n请使用 '/time 频率 时间 内容' 的格式。"
                    logger.warning(reply_str)
                else:
                    reply_str = self.add_task(command_args, user_id, user_name, user_group_name)

            if reply_str is not None:
                # 创建回复对象
                reply = Reply()
                reply.type = ReplyType.TEXT
                reply.content = reply_str
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return

    def get_help_text(self, **kwargs):
        """获取帮助文本"""
        help_text = "- [任务列表]：/time 任务列表\n- [取消任务]：/time 取消任务 任务ID\n- [添加任务]：/time <freq> <time> <GPT> <content> <group>\n\n   示例：/time 今天 17:00 提醒喝水\n   示例：/time 今天 17:00 GPT 提醒喝水\n   示例：/time 今天 17:00 提醒喝水 group[群标题]\n   示例：/time 今天 17:00 GPT 提醒喝水 group[群标题]"
        return help_text
