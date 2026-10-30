"""数据库键与片库成员共用的纯名称规则，不包含数据库或网络依赖。"""


def normalize_library_name(value: str) -> str:
    """只折叠空白和大小写，不做模糊拼写或标点猜测。"""
    return " ".join(str(value or "").strip().split()).casefold()
