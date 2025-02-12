# encoding:utf-8
import re
import gc
import time
import random
import plugins
import sqlite3
import calendar
import shutil
import datetime
import threading
from plugins import *
from lib import itchat
from config import conf
import config as RobotConfig
from common.log import logger
from bridge.bridge import Bridge
from channel import channel_factory
from wcwidth import wcswidth, wcwidth
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
    version="1.0.1",
    author="Sakura7301",
)
class SimpleTimeTask(Plugin):
    def __init__(self):
        super().__init__()
        try:
            self.config = super().load_config()
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            self.chatrooms = {}
            # 获取协议类型
            self.channel_type = conf().get("channel_type")
            if self.channel_type == "gewechat":
                # 设置群映射关系
                self.get_group_map()
            # 线程名
            self.daemon_name = "SimpleTimeTask_daemon"
            # 定义数据库路径
            self.DB_FILE_PATH = "plugins/SimpleTimeTask/simple_time_task.db"
            # 创建数据库锁
            self.db_lock = threading.Lock()
            # 初始化数据库并加载任务到内存
            self.tasks = {}
            self.init_db_and_load_tasks()
            # 此值用于记录上一次重置任务状态的时间()初始化
            self.last_reset_task_date = "1970-01-01"
            # 防抖动字典
            self.user_last_processed_time = {}
            # 检查线程是否关闭
            self.check_daemon()
            # 启动任务检查线程
            self.check_thread = threading.Thread(target=self.check_and_trigger_tasks, name=self.daemon_name)
            self.check_thread.daemon = True
            self.check_thread.start()
            # 初始化完成
            logger.info("[SimpleTimeTask] initialized")

        except Exception as e:
            logger.error(f"[SimpleTimeTask] initialization error: {e}")
            raise "[SimpleTimeTask] init failed, ignore "

    def get_group_map(self):
        from lib.gewechat.client import GewechatClient
        try:
            self.gewe_base_url = conf().get("gewechat_base_url")
            self.gewe_token = conf().get("gewechat_token")
            self.gewe_app_id = conf().get("gewechat_app_id")
            self.gewe_client = GewechatClient(self.gewe_base_url, self.gewe_token)

            # 获取通讯录列表
            result = self.gewe_client.fetch_contacts_list(self.gewe_app_id)
            if result and result['ret'] == 200:
                chatrooms = result['data']['chatrooms']
                brief_info = self.gewe_client.get_brief_info(self.gewe_app_id, chatrooms)
                logger.info(f"[SimpleTimeTask] 群聊简要信息: \n{brief_info}")
                if brief_info and brief_info['ret'] == 200:
                    self.chatrooms = brief_info['data']
                else:
                        logger.error(f"[SimpleTimeTask] 获取群聊标题映射失败! group_id: {chatrooms}")
                logger.debug(f"[SimpleTimeTask] 群聊映射关系: \n{self.chatrooms}")
            else:
                error_info = None
                if result:
                    error_info = f"ret: {result['ret']} msg: {result['msg']}"
                logger.error(f"[SimpleTimeTask] 获取WX通讯录列表失败! {error_info}")
        except Exception as e:
            logger.error(f"[SimpleTimeTask] 设置群聊映射关系失败! {e}")

    def check_daemon(self):
        target_thread = None
        for thread in threading.enumerate():  # 获取所有活动线程
            if thread.name == self.daemon_name:
                # 找到同名线程
                target_thread = thread
                break
        # 回收线程
        if target_thread:
            # 关闭线程
            target_thread._stop()
        # 没有找到同名线程
        return None

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
                    # 添加 Task 实例到 self.tasks 字典，以 task_id 作为键
                    self.tasks[task.task_id] = task

    def pad_string(self, s: str, total_width: int) -> str:
        """
        根据显示宽度填充字符串，使其达到指定的总宽度。
        中英文混合时，中文字符占用两个宽度，英文字符占用一个宽度。
        """
        current_width = wcswidth(s)
        if current_width < total_width:
            # 计算需要填充的空格数
            padding = total_width - current_width
            return s + ' ' * padding
        return s

    def truncate_string(self, s: str, max_width: int, truncate_width: int) -> str:
        """
        截断字符串，使其在超过 max_width 时，显示前 truncate_width 个宽度的字符，
        并在末尾添加省略号 '...'
        """
        # 检查字符串宽度是否超过最大宽度
        if wcswidth(s) > max_width:
            # 计算需要截断的字符数
            truncated = ''
            current_width = 0
            # 遍历字符串的每个字符
            for char in s:
                # 计算当前字符的宽度
                char_width = wcwidth(char)
                # 如果当前宽度超过限制，截断并添加省略号
                if current_width + char_width > truncate_width:
                    break
                # 添加字符到截断后的字符串
                truncated += char
                current_width += char_width
            return truncated + '...'
        return s

    def print_tasks_info(self):
        """
        打印当前 self.tasks 中的所有任务信息，以整齐的表格形式，使用一次 logger 调用。
        """
        try:
            # 如果没有任务，记录相应日志并返回
            if not self.tasks:
                logger.info("[SimpleTimeTask] 当前没有任务。")
                return

            # 定义表头
            headers = [
                "task_id", "time", "frequency", "content",
                "type", "your_name",
                "group_name", "group_title", "executed"
            ]

            # 定义每个列的最大显示宽度
            max_widths = {
                "task_id": 10,
                "time": 5,
                "frequency": 25,         # 增大频率列宽，确保完整打印
                "content": 20,
                "type": 4,
                "your_name": 10,
                "group_name": 10,
                "group_title": 14,
                "executed": 8
            }

            # 收集所有任务的数据，并应用截断
            tasks_data = []
            for task in self.tasks.values():
                # 处理任务ID，如果超过最大宽度，截断并添加省略号
                task_id = self.truncate_string(task.task_id, max_widths["task_id"], max_widths["task_id"] - 3) if wcswidth(task.task_id) > max_widths["task_id"] else self.pad_string(task.task_id, max_widths["task_id"])

                # 处理时间，按原样打印（假设时间格式固定，不需要截断）
                time_value = self.pad_string(task.time_value, max_widths["time"])

                # 频率部分完整打印，不进行截断
                frequency = self.pad_string(task.frequency, max_widths["frequency"])

                # 处理内容，按要求进行截断
                content = self.truncate_string(task.content, max_widths["content"], 17) if wcswidth(task.content) > max_widths["content"] else self.pad_string(task.content, max_widths["content"])

                # 目标类型，转换为中文描述
                target_type = self.pad_string("group" if task.target_type else "user", max_widths["type"])

                # 处理用户昵称，按原样或截断
                user_nickname = self.truncate_string(task.user_name, max_widths["your_name"], max_widths["your_name"] - 3) if wcswidth(task.user_name) > max_widths["your_name"] else self.pad_string(task.user_name, max_widths["your_name"])

                # 处理用户群昵称，按原样或截断
                if task.user_group_name:
                    user_group_nickname = self.truncate_string(task.user_group_name, max_widths["group_name"], max_widths["group_name"] - 3) if wcswidth(task.user_group_name) > max_widths["group_name"] else self.pad_string(task.user_group_name, max_widths["group_name"])
                else:
                    user_group_nickname = self.pad_string("None", max_widths["group_name"])

                # 处理群标题，按要求进行截断
                if task.group_title:
                    group_title = self.truncate_string(task.group_title, max_widths["group_title"], 11) if wcswidth(task.group_title) > max_widths["group_title"] else self.pad_string(task.group_title, max_widths["group_title"])
                else:
                    group_title = self.pad_string("None", max_widths["group_title"])

                # 处理是否已处理，转换为中文描述
                is_processed = self.pad_string("yes" if task.is_processed else "no", max_widths["executed"])

                # 构建任务行
                row = [
                    task_id,
                    time_value,
                    frequency,
                    content,
                    target_type,
                    user_nickname,
                    user_group_nickname,
                    group_title,
                    is_processed
                ]
                tasks_data.append(row)

            # 计算每列的实际宽度（取表头和数据中的最大值，不超过设定的最大宽度）
            actual_widths = []
            for idx, header in enumerate(headers):
                # 获取当前列所有数据的最大显示宽度
                max_data_width = max(wcswidth(str(row[idx])) for row in tasks_data) if tasks_data else wcswidth(header)
                # 计算实际宽度，不超过设定的最大宽度
                actual_width = min(max(wcswidth(header), max_data_width), max_widths[header])
                actual_widths.append(actual_width)

            # 构建分隔线，例如：+----------+-----+--------+
            separator = "+------------+-------+---------------------------+----------------------+------+------------+------------+----------------+----------+"

            # 构建表头行，例如：| task_id | time | frequency |
            header_row = "|" + "|".join(
                f" {self.pad_string(header, actual_widths[idx])} " for idx, header in enumerate(headers)
            ) + "|"

            # 构建所有数据行
            data_rows = []
            for row in tasks_data:
                formatted_row = "|" + "|".join(
                    f" {self.pad_string(str(item), actual_widths[idx])} " for idx, item in enumerate(row)
                ) + "|"
                data_rows.append(formatted_row)

            # 组合完整的表格
            table = "\n".join([
                separator,
                header_row,
                separator
            ] + data_rows + [
                separator
            ])

            # 使用一次 logger 调用打印所有任务信息
            logger.info(f"[SimpleTimeTask] 当前任务列表如下:\n{table}")
        except Exception as e:
            # 如果在打印过程中发生异常，记录错误日志
            logger.error(f"[SimpleTimeTask] 打印任务信息时发生错误: {e}")

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
        tempRoomId = None
        if self.channel_type == "gewechat":
            # 获取群聊
            for chat_room in self.chatrooms:
                # 根据群聊名称匹配群聊ID
                userName = chat_room["userName"]
                NickName = chat_room["nickName"]
                if NickName == group_title:
                    tempRoomId = userName
                    break
        else:
            # 获取群聊ID
            chatrooms = itchat.get_chatrooms()
            # 获取群聊
            for chat_room in chatrooms:
                # 根据群聊名称匹配群聊ID
                userName = chat_room["UserName"]
                NickName = chat_room["NickName"]
                if NickName == group_title:
                    tempRoomId = userName
                    break
        return tempRoomId

    def has_frequency_check_constraint(self):
        """
        检查 tasks 表的 frequency 字段是否有 CHECK 约束。

        返回:
            bool: 如果有 CHECK 约束则返回 True，否则返回 False。
        """
        try:
            conn = sqlite3.connect(self.DB_FILE_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT sql FROM sqlite_master
                WHERE type='table' AND name='tasks';
            """)
            result = cursor.fetchone()
            if result:
                create_table_sql = result[0]
                # 打印 CREATE TABLE 语句用于调试
                logger.debug(f"CREATE TABLE 语句: {create_table_sql}")

                # 查找所有 CHECK 约束
                checks = re.findall(r'CHECK\s*\((.*?)\)', create_table_sql, re.IGNORECASE | re.DOTALL)
                logger.debug(f"检测到的 CHECK 约束: {checks}")

                for check in checks:
                    if 'frequency' in check.lower():
                        logger.info("检测到涉及 'frequency' 字段的 CHECK 约束。")
                        return True
            return False
        except sqlite3.Error as e:
            logger.error(f"检查约束失败: {e}")
            return False

    def migrate_tasks_table(self):
        """
        迁移 tasks 表，移除 frequency 字段的 CHECK 约束。
        数据表路径为 self.DB_FILE_PATH。
        迁移成功后，删除备份文件。

        返回:
            bool: 迁移是否成功。
        """
        backup_path = self.DB_FILE_PATH + "_backup.db"

        try:
            # 备份数据库文件
            shutil.copyfile(self.DB_FILE_PATH, backup_path)
            logger.info(f"数据库已成功备份到 {backup_path}")
        except IOError as e:
            logger.error(f"备份数据库失败: {e}")
            return False

        try:
            # 连接到SQLite数据库
            conn = sqlite3.connect(self.DB_FILE_PATH)
            cursor = conn.cursor()

            # 开始事务
            cursor.execute("BEGIN TRANSACTION;")

            # 重命名现有的 tasks 表为 tasks_old
            cursor.execute("ALTER TABLE tasks RENAME TO tasks_old;")
            logger.info("表 'tasks' 已成功重命名为 'tasks_old'")

            # 创建新的 tasks 表，不包含 CHECK 约束
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    time TEXT NOT NULL,
                    frequency TEXT,
                    content TEXT NOT NULL,
                    target_type INTEGER DEFAULT 0,
                    user_id TEXT,
                    user_name TEXT,
                    user_group_name TEXT,
                    group_title TEXT,
                    is_processed INTEGER DEFAULT 0
                );
            ''')
            logger.info("新的表 'tasks' 已成功创建，不包含 CHECK 约束")

            # 复制数据从 tasks_old 到 新的 tasks 表
            cursor.execute('''
                INSERT INTO tasks (id, time, frequency, content, target_type, user_id, user_name, user_group_name, group_title, is_processed)
                SELECT id, time, frequency, content, target_type, user_id, user_name, user_group_name, group_title, is_processed
                FROM tasks_old;
            ''')
            logger.info("数据已成功从 'tasks_old' 复制到新的 'tasks' 表")

            # 删除旧的 tasks_old 表
            cursor.execute("DROP TABLE tasks_old;")
            logger.info("旧表 'tasks_old' 已成功删除")

            # 提交事务
            conn.commit()
            logger.info("数据库迁移已成功完成。")

            # 删除备份文件
            try:
                os.remove(backup_path)
                logger.info(f"备份文件 {backup_path} 已成功删除。")
            except OSError as e:
                logger.info(f"删除备份文件失败: {e}")

            return True

        except sqlite3.Error as e:
            logger.error(f"数据库迁移失败: {e}")
            if conn:
                conn.rollback()
                logger.info("事务已回滚。")
            return False

    def is_valid_monthly(self, frequency):
        """
        检查 monthly_x 是否合法。

        规则：
        1. 如果 x 小于等于当前月份的天数，返回 True。
        2. 如果 x 超过当前月份的天数且当前日期是本月的最后一天，返回 True。
        3. 其他情况返回 False。

        :param frequency: 字符串，例如 "monthly_30"
        :return: 布尔值
        """
        if not frequency.startswith("monthly_"):
            return False

        try:
            expected_day = int(frequency.split("_")[1])
        except (IndexError, ValueError):
            # 格式不正确或不是整数
            return False

        # 获取当前时间
        now = time.localtime()
        year = now.tm_year
        month = now.tm_mon
        current_day = now.tm_mday

        # 获取当前月份的总天数
        total_days = calendar.monthrange(year, month)[1]

        if expected_day < current_day:
            # 时间未到，无需触发
            return False
        elif expected_day == current_day:
            # 达到约定日期，返回True，准备触发
            logger.debug(f"[SimpleTimeTask] trigger month_task frequency: {frequency}")
            return True
        else:
            # 约定日期大于本月最后一天(如设定为31号，但是本月最多只到30号)
            if current_day == total_days:
                # 今天已经是本月的最后一天，触发。
                logger.debug(f"[SimpleTimeTask] expected day({expected_day}) is not in this month, today is the last day, now trigger it!")
                return True
            else:
                return False

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
            elif re.match(r"每周[一二三四五六日天]", frequency):
                # 处理每周x
                weekday_map = {
                    "一": "Monday",
                    "二": "Tuesday",
                    "三": "Wednesday",
                    "四": "Thursday",
                    "五": "Friday",
                    "六": "Saturday",
                    "日": "Sunday",
                    "天": "Sunday"
                }
                day = frequency[-1]
                english_day = weekday_map.get(day)
                if english_day:
                    frequency = f"weekly_{english_day}"
                else:
                    # 处理未知的星期
                    frequency = "undefined"
            elif re.match(r"每月([1-9]|[12][0-9]|3[01])号", frequency):
                    # 处理每月x号，确保x为1到31
                    day_of_month = re.findall(r"每月([1-9]|[12][0-9]|3[01])号", frequency)[0]
                    frequency = f"monthly_{day_of_month}"
            elif re.match(r"不含周[一二三四五六日天]", frequency):
                # 处理不含周x
                weekday_map = {
                    "一": "Monday",
                    "二": "Tuesday",
                    "三": "Wednesday",
                    "四": "Thursday",
                    "五": "Friday",
                    "六": "Saturday",
                    "日": "Sunday",
                    "天": "Sunday"
                }
                day = frequency[-1]
                english_day = weekday_map.get(day)
                if english_day:
                    frequency = f"excludeWeekday_{english_day}"
                else:
                    # 处理未知的星期
                    frequency = "undefined"
            else:
                # 处理其他未定义的频率
                frequency = "undefined"

            logger.debug(f"即将设置的频率为：{frequency}")

            # 检查任务时间的有效性
            if self.validate_time(frequency, time_value):
                if group_title:
                    target_type = 1
                # 创建任务
                new_task = Task(task_id, time_value, frequency, content, target_type, user_id, user_name, user_group_name, group_title, 0)

                allowed_frequencies = ('once', 'work_day', 'every_day')
                frequency_valid = new_task.frequency in allowed_frequencies

                # 检查是否有 CHECK 约束
                has_check = self.has_frequency_check_constraint()
                logger.debug(f"检查 'tasks' 表中 'frequency' 字段是否有 CHECK 约束: {'有' if has_check else '没有'}")

                # 决定是否需要迁移
                if not frequency_valid:
                    if has_check:
                        logger.info(f"检测到 frequency '{new_task.frequency}' 不在 {allowed_frequencies} 中，并且存在 CHECK 约束，开始迁移数据库以移除 CHECK 约束。")
                        migration_success = self.migrate_tasks_table()
                        if not migration_success:
                            logger.error("数据库迁移失败，无法添加任务。")
                            return
                        else:
                            logger.info("数据库迁移成功，已移除 CHECK 约束。")
                    else:
                        logger.debug(f"检测到 frequency '{new_task.frequency}' 不在 {allowed_frequencies} 中，但 'frequency' 字段没有 CHECK 约束，无需迁移。")

                # 将新任务添加到内存中
                self.tasks[new_task.task_id] = new_task
                # 将新任务更新到数据库
                self.update_task_in_db(new_task)
                # 格式化回复内容
                reply_str = f"[SimpleTimeTask] 😸 任务已添加: \n\n[{task_id}] {frequency} {time_value} {content} {'group[' + group_title + ']' if group_title else ''}"

                # 打印当前任务信息
                self.print_tasks_info()
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
        with self.db_lock:
            tasks_list = "[SimpleTimeTask] 😸 任务列表:\n\n"
            for task in self.tasks.values():
                group_info = f"group[{task.group_title}]" if task.target_type else ""
                tasks_list += f"💼[{task.user_name}|{task.task_id}] {task.frequency} {task.time_value} {task.content} {group_info}\n"
            return tasks_list

    def cancel_task(self, task_id: str) -> str:
        """取消任务"""
        try:
            with self.db_lock:
                if not self.tasks:
                    logger.warning("[SimpleTimeTask] 没有可取消的任务。")
                    return "[SimpleTimeTask] 没有可取消的任务。"

                # 尝试从字典中移除任务
                task = self.tasks.pop(task_id, None)
                if task:
                    logger.info(f"[SimpleTimeTask] 任务已取消: {task_id}")
                    # 从数据库中删除任务
                    self.remove_task_from_db(task_id)
                    # 打印当前任务信息
                    self.print_tasks_info()
                    return f"[SimpleTimeTask] 😸 任务 [{task_id}] 已取消。"
                else:
                    logger.warning(f"[SimpleTimeTask] 未找到任务 ID [{task_id}] 以供取消。")
                    return f"[SimpleTimeTask] 未找到任务 [{task_id}]。"

        except Exception as e:
            logger.error(f"[SimpleTimeTask] 取消任务时发生错误: {e}")
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

    def update_task_status(self, task_id, is_processed=1):
        """ 更新任务的处理状态到数据库 """
        try:
            # 获取任务
            task = self.tasks.get(task_id)
            # 更新任务状态
            task.is_processed = 1
            with sqlite3.connect(self.DB_FILE_PATH) as conn:
                # 连接数据库
                cursor = conn.cursor()
                # 更新任务状态
                cursor.execute('UPDATE tasks SET is_processed = ? WHERE id = ?', (is_processed, task_id))
                # 提交更改
                conn.commit()
            logger.info(f"[SimpleTimeTask] Task status updated in DB: {task_id} to {is_processed}")
        except Exception as e:
            logger.error(f"[SimpleTimeTask] update task status failed: {e}")

    def check_and_trigger_tasks(self):
        """定时检查和触发任务"""
        while True:
            try:
                once_tasks = []
                loop_tasks = []
                # 获取当前时间、日期和星期
                now = time.strftime("%H:%M")
                today_date = time.strftime("%Y-%m-%d")
                # e.g., "Monday"
                current_weekday = time.strftime("%A", time.localtime())

                logger.debug(f"[SimpleTimeTask] 正在检查任务, 当前时间: {today_date}-{now}, 最后重置时间: {self.last_reset_task_date}")

                # 每天重置未处理状态
                if now == "00:00" and today_date != self.last_reset_task_date:
                    self.reset_processed_status()
                    # 更新最后重置日期
                    self.last_reset_task_date = today_date
                    logger.info(f"[SimpleTimeTask] 已重置所有任务的处理状态。记录最后重置日期为 {self.last_reset_task_date}。")

                # 使用 list() 避免在遍历过程中修改字典
                tasks_copy = list(self.tasks.values())

                # 创建任务副本以避免在遍历时修改列表
                for task in tasks_copy:
                    # 检查任务是否应该被触发
                    if self.should_trigger(task, now, today_date, current_weekday):
                        # 处理任务
                        self.process_task(task.task_id)
                        if task.frequency == "once":
                            once_tasks.append(task.task_id)
                        else:
                            loop_tasks.append(task.task_id)

                # 删除一次性任务
                for task_id in once_tasks:
                    # 删除对应ID的任务缓存
                    self.del_task_from_id(task_id)
                    # 从数据库中删除任务
                    self.remove_task_from_db(task_id)

                # 更新任务状态
                for task_id in loop_tasks:
                    self.update_task_status(task_id)

            except Exception as e:
                logger.error(f"[SimpleTimeTask] An unexpected error occurred: {e}")
            # 每5秒检查一次
            time.sleep(5)

    def remove_task(self, task_id):
        """从任务列表和数据库中移除任务"""
        try:
            # 从任务列表和数据库中移除任务
            self.del_task_from_id(task_id)
            self.remove_task_from_db(task_id)
        except Exception as e:
            logger.error(f"[SimpleTimeTask] Failed to remove task ID {task_id}: {e}")

    def should_trigger(self, task, now, today_date, current_weekday):
        """判断任务是否应该被触发"""
        frequency = task.frequency
        task_time = task.time_value

        # 一次性任务
        if frequency == "once":
            try:
                task_date, task_time = task_time.split(' ')
            except ValueError:
                logger.error(f"[SimpleTimeTask] Invalid time format for task ID {task.task_id}")
                self.remove_task(task.task_id)
                return False
            if task_date != today_date or task_time != now:
                return False
        # 工作日任务
        elif frequency == "work_day":
            if not self.is_weekday() or task_time != now or task.is_processed == 1:
                return False
        # 每天任务
        elif frequency == "every_day":
            if task_time != now or task.is_processed == 1:
                return False
        # 每周任务
        elif frequency.startswith("weekly_"):
            try:
                _, weekday = frequency.split("_")
            except ValueError:
                logger.error(f"[SimpleTimeTask] Invalid weekly frequency format for task ID {task.task_id}")
                self.remove_task(task.task_id)
                return False
            if current_weekday != weekday or task_time != now or task.is_processed == 1:
                return False
        # 每周除星期x外任务
        elif frequency.startswith("excludeWeekday_"):
            try:
                _, excluded_weekday = frequency.split("_")
            except ValueError:
                logger.error(f"[SimpleTimeTask] Invalid excludeWeekday frequency format for task ID {task.task_id}")
                self.remove_task(task.task_id)
                return False
            if current_weekday == excluded_weekday or task_time != now or task.is_processed == 1:
                return False
        # 每月x号任务
        elif frequency.startswith("monthly_"):
            if not self.is_valid_monthly(frequency) or task_time != now or task.is_processed == 1:
                return False
        # 未知频率
        else:
            logger.warning(f"[SimpleTimeTask] Unknown frequency '{frequency}' for task ID {task.task_id}")
            return False

        return True

    def get_task(self, task_id):
        """获取任务"""
        return self.tasks.get(task_id)

    def del_task_from_id(self, task_id: str) -> bool:
        """删除任务并返回是否成功"""
        # 从内存中删除任务
        self.tasks.pop(task_id, None)
        # 打印当前任务信息
        self.print_tasks_info()

    def process_task(self, task_id):
        """处理并触发任务"""
        try:
            # 获取任务
            task = self.get_task(task_id)
            if task is None:
                # 任务不存在
                logger.error(f"[SimpleTimeTask] Task ID {task_id} not found.")
            else:
                # 运行任务
                self.run_task_in_thread(task)
        except Exception as e:
            logger.error(f"[SimpleTimeTask] Failed to process task ID {task_id}: {e}")
            self.remove_task(task_id)

    def reset_processed_status(self):
        """ 重置所有任务的已处理状态 """
        try:
            with self.db_lock:
                for task in self.tasks.values():
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
        except Exception as e:
            logger.error(f"[SimpleTimeTask] Failed to reset processed status: {e}")

    def generate_unique_id(self):
        """ 生成唯一任务ID """
        return ''.join(random.choices('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=10))

    def validate_time(self, frequency, time_value):
        """ 验证时间和频率 """
        if frequency not in [ "once", "work_day", "every_day", "weekly_Monday", "weekly_Tuesday", "weekly_Wednesday", "weekly_Thursday", "weekly_Friday", "weekly_Saturday", "weekly_Sunday", "monthly_1", "monthly_2", "monthly_3", "monthly_4", "monthly_5", "monthly_6", "monthly_7", "monthly_8", "monthly_9", "monthly_10", "monthly_11", "monthly_12", "monthly_13", "monthly_14", "monthly_15", "monthly_16", "monthly_17", "monthly_18", "monthly_19", "monthly_20", "monthly_21", "monthly_22", "monthly_23", "monthly_24", "monthly_25", "monthly_26", "monthly_27", "monthly_28", "monthly_29", "monthly_30", "monthly_31", "excludeWeekday_Monday", "excludeWeekday_Tuesday", "excludeWeekday_Wednesday", "excludeWeekday_Thursday", "excludeWeekday_Friday", "excludeWeekday_Saturday", "excludeWeekday_Sunday"]:
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
                if receiver is None:
                    # 未获取到群id，跳过此次任务处理
                    return
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
        try:
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
        except Exception as e:
            logger.error(f"[SimpleTimeTask] 运行任务时发生异常: {e}")

    def run_with_timeout(self, task: Task):
        """ 运行任务并捕获异常 """
        try:
            self.trigger_task(task)
        except Exception as e:
            logger.error(f"[SimpleTimeTask] 触发任务时发生异常: {e}")

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
        if self.channel_type == "gewechat":
            # gewe协议无需区分真实ID
            user_id = msg.actual_user_id
        else:
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
            if self.channel_type == "gewechat":
                # gewe协议获取群名
                user_name = msg.actual_user_nickname
                if msg.is_group:
                    user_group_name = msg.other_user_nickname
            else:
                # 检查是否为群消息
                if msg.is_group:
                        # itchat协议获取群名
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
            else:
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
        help_text = "- [任务列表]：/time 任务列表\n- [取消任务]：/time 取消任务 任务ID\n- [添加任务]：/time <freq> <time> <GPT> <content> <group>\n\n示例：\n    /time 今天 17:00 提醒喝水\n    /time 今天 17:00 GPT 提醒喝水\n    /time 每周日 08:00 GPT 提醒我逛超市\n    /time 不含周日 08:55 摸鱼\n    /time 每月10号 17:00 GPT 提醒我存钱\n    /time 今天 17:00 提醒喝水\n    /time 今天 17:00 GPT 提醒喝水 group[群标题]\n\n注意：设定每月固定日期触发时，如果本月没有指定的日期，任务会默认在当月的最后一天触发。"
        return help_text
