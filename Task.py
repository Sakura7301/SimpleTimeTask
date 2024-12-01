class Task:
    def __init__(self, task_id=None, time_value="", frequency="", content="", target_type=0, user_id="", user_name="", user_group_name="", group_title="", is_processed=0):
        self.task_id = task_id
        self.time_value = time_value
        self.frequency = frequency
        self.content = content
        self.target_type = target_type
        self.user_id = user_id
        self.user_name = user_name
        self.user_group_name = user_group_name
        self.group_title = group_title
        self.is_processed = is_processed
