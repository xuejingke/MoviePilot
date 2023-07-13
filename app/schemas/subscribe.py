from typing import Optional, List

from pydantic import BaseModel


class Subscribe(BaseModel):
    id: Optional[int] = None
    # 订阅名称
    name: Optional[str] = None
    # 订阅年份
    year: Optional[str] = None
    # 订阅类型 电影/电视剧
    type: Optional[str] = None
    # 搜索关键字
    keyword: Optional[str] = None
    tmdbid: Optional[int] = None
    doubanid: Optional[str] = None
    # 季号
    season: Optional[int] = None
    # 海报
    poster: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 评分
    vote: Optional[int] = 0
    # 描述
    description: Optional[str] = None
    # 过滤规则
    filter: Optional[str] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 总集数
    total_episode: Optional[int] = 0
    # 开始集数
    start_episode: Optional[int] = 0
    # 缺失集数
    lack_episode: Optional[int] = 0
    # 附加信息
    note: Optional[str] = None
    # 状态：N-新建， R-订阅中
    state: Optional[str] = None
    # 最后更新时间
    last_update: Optional[str] = None
    # 订阅用户
    username: Optional[str] = None
    # 订阅站点
    sites: Optional[List[int]] = None

    class Config:
        orm_mode = True
