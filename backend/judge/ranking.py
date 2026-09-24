"""实时排行榜：增量更新 + 封榜（Scoreboard Freeze）。

存储（对应「成绩按竞赛/用户分片」）：
  data/scores/{contest_id}/{user_id}.json   每个用户一份成绩分片（权威数据）
  data/scores/{contest_id}/ranking.json     聚合榜单（增量维护、用于快速读取）

评分模式：
  - acm：按「解题数降序、罚时升序」排名；罚时 = 首次 AC 时刻(秒) + 错误次数 * 罚时
  - ioi：按「总分降序、总用时升序」排名；每题取历史最高分（部分分）

封榜：当 contest.freeze_time 到达后，公开榜单冻结为封榜时刻的快照；
      内部成绩继续更新，仅管理员可查看实时榜单。
"""
import copy
import os

from backend import config
from backend.storage import read_json, locked_update, list_files
from backend.utils import now_iso, now_ts, parse_time

RANKING_FILE = "ranking.json"


def _problem_exists(problem_id):
    """题目文件是否仍存在（题目被删除后，其在竞赛/榜单中的引用应失效）。"""
    if not problem_id:
        return False
    return os.path.exists(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))


def _contest_problem_ids(contest):
    """竞赛当前有效题目 id 集合；题目文件已删除的条目不计入。"""
    ids = set()
    for item in contest.get("problems", []) if contest else []:
        pid = item.get("problem_id") if isinstance(item, dict) else item
        if pid and _problem_exists(pid):
            ids.add(pid)
    return ids


def _scrub_row(row, valid_ids):
    """剔除榜单行中已不属于竞赛（题目已删除）的列，并重算汇总指标。

    成绩分片里可能残留被删除题目的 solved/score/penalty，
    若不过滤会导致解题数、总分、罚时与实际题目数对不上。
    返回新行，不修改入参。
    """
    row = copy.deepcopy(row)
    probs = row.get("problems", {}) or {}
    kept = {pid: p for pid, p in probs.items() if pid in valid_ids}
    solved = sum(1 for p in kept.values() if p.get("solved"))
    score = sum(p.get("score", 0) for p in kept.values())
    penalty = sum(p.get("penalty", 0) for p in kept.values())
    total_time_ms = sum(p.get("time_ms", 0) for p in kept.values())
    row["problems"] = kept
    row["solved"] = solved
    row["score"] = score
    row["penalty"] = penalty
    row["total_time_ms"] = total_time_ms
    return row


def scrub_rows(rows, contest):
    """按竞赛当前有效题目过滤榜单行，重算指标、排序与名次（读取时兜底）。"""
    valid_ids = _contest_problem_ids(contest)
    mode = contest.get("mode", "acm")
    out = [_scrub_row(r, valid_ids) for r in rows]
    out.sort(key=lambda r: _sort_key(r, mode))
    for i, r in enumerate(out):
        r["rank"] = i + 1
    return out


def contest_status(contest, ts=None):
    """返回竞赛状态：upcoming | running | ended。"""
    ts = ts or now_ts()
    start = parse_time(contest.get("start_time"))
    end = parse_time(contest.get("end_time"))
    if start is None or ts < start:
        return "upcoming"
    if end is not None and ts > end:
        return "ended"
    return "running"


def contest_elapsed(contest, ts=None):
    """竞赛已进行秒数（未开始返回 0）。"""
    ts = ts or now_ts()
    start = parse_time(contest.get("start_time"))
    if start is None or ts <= start:
        return 0
    return int(ts - start) + 28800


def _score_dir(contest_id):
    return os.path.join(config.SCORES_DIR, contest_id)


def _user_path(contest_id, user_id):
    return os.path.join(_score_dir(contest_id), f"{user_id}.json")


def _ranking_path(contest_id):
    return os.path.join(_score_dir(contest_id), RANKING_FILE)


def empty_user_record(contest_id, user_id, username, nickname):
    return {
        "contest_id": contest_id,
        "user_id": user_id,
        "username": username,
        "nickname": nickname or username,
        "solved": 0,
        "score": 0,
        "penalty": 0,
        "total_time_ms": 0,
        "problems": {},
    }


def _summarize(record, mode, penalty_seconds):
    """由用户成绩分片计算榜单摘要行。"""
    solved = 0
    score = 0
    penalty = 0
    total_time_ms = 0
    for p in record.get("problems", {}).values():
        if p.get("solved"):
            solved += 1
        score += p.get("score", 0)
        penalty += p.get("penalty", 0)
        total_time_ms += p.get("time_ms", 0)
    return {
        "user_id": record["user_id"],
        "username": record.get("username", ""),
        "nickname": record.get("nickname", record.get("username", "")),
        "solved": solved,
        "score": score,
        "penalty": penalty,
        "total_time_ms": total_time_ms,
        "problems": record.get("problems", {}),
    }


def _sort_key(row, mode):
    if mode == "acm":
        # 解题数降序，罚时升序，用时升序
        return (-row["solved"], row["penalty"], row["user_id"])
    # ioi：总分降序，用时升序
    return (-row["score"], row["total_time_ms"], row["user_id"])


def _rebuild_ranking(contest_id, mode, penalty_seconds, valid_ids=None):
    """重建聚合榜单（扫描该竞赛全部分片并排序）。

    valid_ids 为竞赛当前有效的题目 id 集合；分片里残留的已删除题目列
    会在重建时剔除并参与重算，保证榜单与竞赛题目一致。
    """
    if valid_ids is None:
        contest = read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))
        valid_ids = _contest_problem_ids(contest) if contest else None
    d = _score_dir(contest_id)
    rows = []
    for name in list_files(d):
        if name == "ranking":
            continue
        rec = read_json(os.path.join(d, name + ".json"))
        if rec:
            if valid_ids is None:
                rows.append(_summarize(rec, mode, penalty_seconds))
            else:
                rows.append(_scrub_row(_summarize(rec, mode, penalty_seconds), valid_ids))
    rows.sort(key=lambda r: _sort_key(r, mode))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    data = {
        "contest_id": contest_id,
        "mode": mode,
        "updated_at": now_iso(),
        "frozen_at": None,
        "frozen_snapshot": None,
        "rows": rows,
    }
    existing = read_json(_ranking_path(contest_id))
    if existing and existing.get("frozen_snapshot") is not None:
        # 封榜快照同样剔除已删除的题目列（删除发生在封榜期也能修正列与名次）
        contest = read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))
        ids = valid_ids if valid_ids is not None else _contest_problem_ids(contest)
        snap_mode = (contest or {}).get("mode", mode)
        snap = [_scrub_row(r, ids) for r in existing["frozen_snapshot"]]
        snap.sort(key=lambda r: _sort_key(r, snap_mode))
        for i, r in enumerate(snap):
            r["rank"] = i + 1
        data["frozen_snapshot"] = snap
        data["frozen_at"] = existing.get("frozen_at")
    return data


def is_frozen(contest, ts=None):
    """当前是否处于封榜状态。"""
    if not contest.get("freeze_enabled"):
        return False
    ts = ts or now_ts()
    freeze = parse_time(contest.get("freeze_time"))
    end = parse_time(contest.get("end_time"))
    if freeze is None:
        return False
    if ts < freeze:
        return False
    # 结束后解冻（所有人可见最终结果）
    if end is not None and ts > end:
        return False
    return True


def record_submission(contest, user, problem_id, result):
    """在评测完成后增量更新该用户的成绩分片与聚合榜单。

    result 由评测引擎给出，包含 status / score / time_ms / memory_kb 等。
    该函数在评测线程中调用，通过 storage 的文件级锁保证并发安全。
    """
    mode = contest.get("mode", "acm")
    penalty_seconds = int(config.DEFAULT_SETTINGS["ranking"]["penalty_seconds"])
    contest_id = contest["id"]
    user_id = user["id"]

    def _update(rec):
        if rec is None:
            rec = empty_user_record(
                contest_id, user_id, user.get("username", ""), user.get("nickname", "")
            )
        probs = rec.setdefault("problems", {})
        p = probs.setdefault(problem_id, {
            "solved": False, "attempts": 0, "first_solve_time": None,
            "score": 0, "time_ms": 0, "memory_kb": 0, "penalty": 0,
        })
        p["attempts"] += 1
        p["time_ms"] = max(p["time_ms"], result.get("time_ms", 0))
        p["memory_kb"] = max(p["memory_kb"], result.get("memory_kb", 0))

        accepted = result.get("status") == "AC"
        if accepted:
            p["solved"] = True
            if p["first_solve_time"] is None:
                elapsed = contest_elapsed(contest)
                p["first_solve_time"] = now_iso()
                if mode == "acm":
                    wrong_before = p["attempts"] - 1
                    p["penalty"] = elapsed + wrong_before * penalty_seconds
        if mode == "ioi":
            p["score"] = max(p["score"], result.get("score", 0))
        elif mode == "acm":
            p["score"] = 1 if p["solved"] else 0
        return rec

    user_path = _user_path(contest_id, user_id)
    record = locked_update(user_path, _update, default=None)

    # 增量更新聚合榜单：重新扫描并排序（分数变化才触发）
    _maybe_freeze_snapshot(contest)
    ranking = _rebuild_ranking(contest_id, mode, penalty_seconds,
                               valid_ids=_contest_problem_ids(contest))
    locked_update(_ranking_path(contest_id), lambda _d: ranking, default=ranking)
    return record


def _maybe_freeze_snapshot(contest):
    """封榜时刻到达时，捕获当前榜单作为冻结快照（只捕获一次）。"""
    if is_frozen(contest):
        return
    path = _ranking_path(contest["id"])
    existing = read_json(path)
    if existing is None or existing.get("frozen_snapshot") is not None:
        return
    rows = existing.get("rows", [])
    # 冻结快照深拷贝（避免后续内部分片变动污染）
    snapshot = copy.deepcopy(rows)
    for i, r in enumerate(snapshot):
        r["rank"] = i + 1
    existing["frozen_snapshot"] = snapshot
    existing["frozen_at"] = now_iso()
    locked_update(path, lambda _d: existing, default=existing)


def get_leaderboard(contest, as_admin=False):
    """获取榜单。封榜期间非管理员看到冻结快照。

    读取时按竞赛当前有效题目（题目文件仍存在）兜底过滤一次，
    防止历史脏数据中残留已删除题目的列与分数。
    """
    path = _ranking_path(contest["id"])
    data = read_json(path)
    if data is None:
        return {"contest_id": contest["id"], "rows": [], "frozen": False,
                "frozen_at": None, "updated_at": None}
    frozen = is_frozen(contest)
    rows = data.get("rows", [])
    if frozen and not as_admin:
        snap = data.get("frozen_snapshot")
        rows = snap if snap is not None else []
    rows = scrub_rows(rows, contest)
    return {
        "contest_id": contest["id"],
        "mode": contest.get("mode", "acm"),
        "rows": rows,
        "frozen": frozen,
        "frozen_at": data.get("frozen_at"),
        "updated_at": data.get("updated_at"),
    }


def get_user_record(contest_id, user_id):
    """读取单个用户在竞赛中的成绩分片。"""
    return read_json(_user_path(contest_id, user_id))


def reset_contest_scores(contest_id):
    """清空某竞赛的全部成绩（用于重判/清空榜单）。"""
    import shutil
    shutil.rmtree(_score_dir(contest_id), ignore_errors=True)
    os.makedirs(_score_dir(contest_id), exist_ok=True)


def remove_problem_from_scores(contest_id, problem_id):
    """从某竞赛的全部用户成绩分片中剔除指定题目，并重建聚合榜单。

    题目被管理员删除时调用：否则 solved/score/penalty 仍会计入该题，
    榜单上也会残留一列无法再提交的题目。
    """
    contest = read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))
    if contest is None:
        return
    mode = contest.get("mode", "acm")
    penalty_seconds = int(config.DEFAULT_SETTINGS["ranking"]["penalty_seconds"])
    d = _score_dir(contest_id)

    def _strip(rec):
        probs = rec.get("problems", {})
        if problem_id not in probs:
            return rec
        probs.pop(problem_id, None)
        rec["solved"] = sum(1 for p in probs.values() if p.get("solved"))
        rec["score"] = sum(p.get("score", 0) for p in probs.values())
        rec["penalty"] = sum(p.get("penalty", 0) for p in probs.values())
        rec["total_time_ms"] = sum(p.get("time_ms", 0) for p in probs.values())
        return rec

    changed = False
    for name in list_files(d):
        if name == "ranking":
            continue
        path = os.path.join(d, name + ".json")
        rec = read_json(path)
        if rec is None or problem_id not in rec.get("problems", {}):
            continue
        locked_update(path, _strip, default=None)
        changed = True

    # 无论分片是否变化都重建一次，清掉聚合榜单/冻结快照里可能残留的列
    ranking = _rebuild_ranking(contest_id, mode, penalty_seconds,
                               valid_ids=_contest_problem_ids(contest))
    locked_update(_ranking_path(contest_id), lambda _d: ranking, default=ranking)
    return changed
