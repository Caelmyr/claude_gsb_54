"""实时排行榜 API。"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, get_current_user
from backend.storage import read_json
from backend.judge.ranking import get_leaderboard, contest_status, _sort_key
from backend.api.contests import _valid_problems
from backend.utils import user_key, frozen_now

leaderboard_bp = Blueprint("leaderboard", __name__)


def _load(contest_id):
    return read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))


@leaderboard_bp.get("/leaderboard/<contest_id>")
@require_auth
def leaderboard(contest_id):
    contest = _load(contest_id)
    if not contest:
        return err("竞赛不存在", 404)
    as_admin = request.user.get("role") == "admin"
    if not contest.get("visble", True) and not as_admin:
        return err("竞赛不存在", 404)
    data = get_leaderboard(contest, as_admin=as_admin)
    # 与评分模式一致的名次排序（ACM：解题数降序/罚时升序；IOI：总分降序/用时升序）
    data["rows"] = sorted(data.get("rows", []),
                          key=lambda r: _sort_key(r, contest.get("mode", "acm")))
    data["contest_status"] = contest_status(contest)
    data["frozen_now"] = frozen_now(contest)
    data["freeze_time"] = contest.get("freeze_time")
    data["freeze_enabled"] = contest.get("freeze_enabled", False)
    data["mode"] = contest.get("mode", "acm")
    data["contest_title"] = contest.get("title", "")
    data["contest_problems"] = _valid_problems(contest)
    data["me"] = user_key(request.user)
    return ok(data)
