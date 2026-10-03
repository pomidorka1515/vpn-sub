from typing import Literal

from ..common import BaseService, SharedCoreResources
from .user.common import CommonUserService
from .bandwidth import BandwidthService
from .panel import BG_POOL

class LeaderboardService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        user_svc: CommonUserService,
        bandwidth_svc: BandwidthService
    ) -> None:
        super().__init__(res)
        self.user_svc: CommonUserService = user_svc
        self.bandwidth_svc: BandwidthService = bandwidth_svc
    def leaderboard(
        self,
        category: Literal["total", "monthly", "wl_monthly"],
        *,
        top_n: int = 0,
        use_displaynames: bool = False,
        flip: bool = False,
    ) -> dict[str, int]:
        """
        Dict of top-n users by specified bandwidth type, in order.
        WARNING: polls all users info. this is blocking!

        Args:
            category: type of bandwidth to use.
            top_n: clamp the leaderboard to top-n users.
                If <= 0, returns a leaderboard with all users.
            use_displaynames: if True, uses display names instead of internal usernames.
                Useful when passing data into chart.leaderboard_chart().
            flip: if True, sorted in ascending order, otherwise descending.

        Returns:
            A dict: {"username": 1293720471, "second_place": 12493630},
            where key is the username and value is bandwidth in bytes.
        """
        raw: dict[str, int] = {}
        records = self.user_svc.list_user_states()
        users = [record["username"] for record in records]
        if use_displaynames:
            display_users = [str(record["displayname"]) for record in records]
        else:
            display_users = users
        # display_users is what we use in dict keys
        # populate the raw data
        match category:
            case 'total':
                # One batched read instead of a per-user panel poll.
                # Admin bot and the HTTP admin route share the background
                # pool with BWatch. A whitelist-only read is one panel and
                # stays inline; this path is the multi-panel total.
                totals = self.bandwidth_svc.all_traffic(pool=BG_POOL)
                for user, display in zip(users, display_users):
                    info = totals.get(user)
                    raw[display] = int(info.total) if info is not None else 0
            case 'monthly' | 'wl_monthly':
                for record, display in zip(records, display_users):
                    if category == "monthly":
                        limit = record["bw_limit_gb"]
                        used = record["bw_used"]
                    else:
                        limit = record["wl_limit_gb"]
                        used = record["wl_used"]
                    if limit == 0:
                        continue # skip users who dont have bandwidth
                    raw[display] = used

        sorted_items: list[tuple[str, int]] = sorted(raw.items(), key=lambda x: x[1], reverse=not flip)
        if top_n > 0:
            sorted_items = sorted_items[:top_n]
        return {k: v for k, v in sorted_items}
