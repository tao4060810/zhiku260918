"""单进程有界限流；计数满时拒绝新键，不能通过随机用户名驱逐有效限制。"""
from threading import Lock
from time import monotonic
from typing import Hashable

_buckets: dict[Hashable, tuple[int, float]] = {}
_lock = Lock()
MAX_BUCKETS = 10000


class RateLimited(Exception):
    """携带剩余等待时间的限流异常，由 Web 层转换成 HTTP 429。"""
    def __init__(self, retry_after):
        """
        记录客户端至少需要等待的秒数
        :param retry_after: 距离当前限流窗口结束的秒数
        """
        self.retry_after = max(1, int(retry_after))


def check_rate(key, limit, seconds):
    """
    按固定时间窗口累计请求次数，超限时抛出 RateLimited
    :param key: 限流维度，如接口名与 IP 或用户名组成的元组
    :param limit: 窗口内允许的最大请求次数
    :param seconds: 限流窗口持续秒数
    """
    now = monotonic()
    with _lock:
        # 1. 清理已结束的窗口，释放内存中的计数项
        for old in [k for k, (_, end) in _buckets.items() if end <= now]:
            del _buckets[old]
        # 2. 已有键沿用原窗口，新键从本次请求开始计时
        count, end = _buckets.get(key, (0, now + seconds))
        # 3. 达到次数或总键数上限时拒绝请求，避免随机键挤掉已有计数
        if count >= limit or (key not in _buckets and len(_buckets) >= MAX_BUCKETS):
            raise RateLimited(end - now)
        _buckets[key] = (count + 1, end)
