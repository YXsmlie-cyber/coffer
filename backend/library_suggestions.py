"""未绑定片库目录的只读名称建议；不自动绑定，也不改变相似度规则。"""
import difflib

from sqlalchemy import select

from .database import Model
from .identity import normalize_library_name


async def suggestion_model_names(session) -> list[tuple[int, str]]:
    """通常只读取建议用的两个字段，避免构造全量模特 ORM 对象。"""
    predicate = Model.profile_type.in_(("model", "pornstar"))
    rows = (await session.execute(select(Model.id, Model.name).where(predicate))).all()
    names = [(row.id, row.name) for row in rows]
    if len({normalize_library_name(name) for _, name in names}) != len(names):
        # 旧实现对归一化重名使用“最后读取者覆盖”。窄投影可能让 SQLite
        # 选择另一覆盖索引，改变无 ORDER BY 的读取顺序；仅此时保留原查询。
        legacy = (await session.execute(select(Model).where(predicate))).scalars().all()
        return [(model.id, model.name) for model in legacy]
    return names


def build_binding_suggestions(
    folders: list[tuple[str, str]], models: list[tuple[int, str]],
) -> dict[str, list[dict]]:
    """整批纯计算，可在线程中运行；顺序、阈值与评分沿用原有 difflib。"""
    known_names = {
        normalize_library_name(name): (model_id, name)
        for model_id, name in models
    }
    possible_names = list(known_names)
    suggestions = {}
    for cid, normalized_name in folders:
        matches = difflib.get_close_matches(
            normalized_name, possible_names, n=5, cutoff=0.55)
        suggestions[cid] = [{
            "model_id": known_names[name][0],
            "name": known_names[name][1],
            "confidence": round(difflib.SequenceMatcher(
                None, normalized_name, name).ratio(), 4),
            "automatic": False,
        } for name in matches]
    return suggestions
