"""project.store — where tasks live. In memory, nothing else."""

from project import config

_TASKS = []


def add_task(title):
    """Append a task and return it. Raises ValueError past MAX_TASKS."""
    if len(_TASKS) >= config.MAX_TASKS:
        raise ValueError("too many tasks")
    task = {"id": len(_TASKS) + 1, "title": title}
    _TASKS.append(task)
    return task


def list_tasks():
    """Every task, oldest first."""
    return list(_TASKS)
