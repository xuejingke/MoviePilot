import re
import traceback
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import ThreadPool
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap

from app import schemas
from app.core.config import settings
from app.core.event import EventManager, eventmanager, Event
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils


class AutoSignIn(_PluginBase):
    # 插件名称
    plugin_name = "站点自动签到"
    # 插件描述
    plugin_desc = "自动模拟登录站点并签到。"
    # 插件图标
    plugin_icon = "signin.png"
    # 主题色
    plugin_color = "#4179F4"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "autosignin_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    sites: SitesHelper = None
    # 事件管理器
    event: EventManager = None
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    # 加载的模块
    _site_schema: list = []

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _queue_cnt: int = 5
    _sign_sites: list = []
    _retry_keyword = None
    _clean: bool = False
    _start_time: int = None
    _end_time: int = None

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.event = EventManager()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._queue_cnt = config.get("queue_cnt") or 5
            self._sign_sites = config.get("sign_sites")
            self._retry_keyword = config.get("retry_keyword")
            self._clean = config.get("clean")

        # 加载模块
        if self._enabled or self._onlyonce:

            self._site_schema = ModuleHelper.load('app.plugins.autosignin.sites',
                                                  filter_func=lambda _, obj: hasattr(obj, 'match'))

            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._cron:
                try:
                    if self._cron.strip().count(" ") == 4:
                        self._scheduler.add_job(func=self.sign_in,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="站点自动签到")
                        logger.info(f"站点自动签到服务启动，执行周期 {self._cron}")
                    else:
                        # 2.3/9-23
                        crons = self._cron.strip().split("/")
                        if len(crons) == 2:
                            # 2.3
                            self._cron = crons[0]
                            # 9-23
                            times = crons[1].split("-")
                            if len(times) == 2:
                                # 9
                                self._start_time = int(times[0])
                                # 23
                                self._end_time = int(times[1])
                        if self._start_time and self._end_time:
                            self._scheduler.add_job(func=self.sign_in,
                                                    trigger="interval",
                                                    hours=float(self._cron.strip()),
                                                    name="站点自动签到")
                            logger.info(f"站点自动签到服务启动，执行周期 {self._cron}")
                        else:
                            logger.error("站点自动签到服务启动失败，周期格式错误")
                            # 推送实时消息
                            self.systemmessage.put(f"执行周期配置错误")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")
            else:
                # 随机时间
                triggers = TimerUtils.random_scheduler(num_executions=2,
                                                       begin_hour=9,
                                                       end_hour=23,
                                                       max_interval=12 * 60,
                                                       min_interval=6 * 60)
                for trigger in triggers:
                    self._scheduler.add_job(self.sign_in, "cron",
                                            hour=trigger.hour, minute=trigger.minute,
                                            name="站点自动签到")

            if self._onlyonce:
                logger.info(f"站点自动签到服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sign_in, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点自动签到")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        # 保存配置
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "queue_cnt": self._queue_cnt,
                "sign_sites": self._sign_sites,
                "retry_keyword": self._retry_keyword,
                "clean": self._clean,
            }
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/site_signin",
            "event": EventType.SiteSignin,
            "desc": "站点签到",
            "data": {}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [{
            "path": "/signin_by_domain",
            "endpoint": self.signin_by_domain,
            "methods": ["GET"],
            "summary": "站点签到",
            "description": "使用站点域名签到站点",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项
        site_options = [{"title": site.get("name"), "value": site.get("id")}
                        for site in self.sites.get_indexers()]
        return [
                   {
                       'component': 'VForm',
                       'content': [
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 3
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'enabled',
                                                   'label': '启用插件',
                                               }
                                           }
                                       ]
                                   },
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 3
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'notify',
                                                   'label': '发送通知',
                                               }
                                           }
                                       ]
                                   },
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 3
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'onlyonce',
                                                   'label': '立即运行一次',
                                               }
                                           }
                                       ]
                                   },
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 3
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'clean',
                                                   'label': '清理本日已签到',
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 4
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'cron',
                                                   'label': '执行周期',
                                                   'placeholder': '5位cron表达式，留空自动'
                                               }
                                           }
                                       ]
                                   },
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 4
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'queue_cnt',
                                                   'label': '队列数量'
                                               }
                                           }
                                       ]
                                   },
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 4
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'retry_keyword',
                                                   'label': '重试关键词',
                                                   'placeholder': '重新签到关键词，支持正则表达式；每天首次全签，后续如果设置了重试词则只签到命中重试词的站点，否则全签。'
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'content': [
                                           {
                                               'component': 'VSelect',
                                               'props': {
                                                   'chips': True,
                                                   'multiple': True,
                                                   'model': 'sign_sites',
                                                   'label': '签到站点',
                                                   'items': site_options
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                       },
                                       'content': [
                                           {
                                               'component': 'VAlert',
                                               'props': {
                                                   'text': '签到周期支持：'
                                                           '1.五位cro表达式、'
                                                           '2.配置间隔，单位小时，比如2.3/9-23（9-23点之间每隔2.3小时执行一次）、'
                                                           '3.周期不填默认9-23点随机执行2次。'
                                                           '每天首次签到全量签到，其余执行命中重试关键词的站点。'
                                               }
                                           }
                                       ]
                                   }
                               ]
                           }
                       ]
                   }
               ], {
                   "enabled": False,
                   "notify": True,
                   "cron": "",
                   "onlyonce": False,
                   "clean": False,
                   "queue_cnt": 5,
                   "sign_sites": [],
                   "retry_keyword": "错误|失败"
               }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 最近两天的日期数组
        date_list = [(datetime.now() - timedelta(days=i)).date() for i in range(2)]
        # 最近一天的签到数据
        current_day = ""
        sign_data = []
        for day in date_list:
            current_day = f"{day.month}月{day.day}日"
            sign_data = self.get_data(current_day)
            if sign_data:
                break
        if sign_data:
            contents = [
                {
                    'component': 'tr',
                    'props': {
                        'class': 'text-sm'
                    },
                    'content': [
                        {
                            'component': 'td',
                            'props': {
                                'class': 'whitespace-nowrap break-keep'
                            },
                            'text': current_day
                        },
                        {
                            'component': 'td',
                            'text': data.get("site")
                        },
                        {
                            'component': 'td',
                            'text': data.get("status")
                        }
                    ]
                } for data in sign_data
            ]
        else:
            contents = [
                {
                    'component': 'tr',
                    'props': {
                        'class': 'text-sm'
                    },
                    'content': [
                        {
                            'component': 'td',
                            'props': {
                                'colspan': 3,
                                'class': 'text-center'
                            },
                            'text': '暂无数据'
                        }
                    ]
                }
            ]
        return [
            {
                'component': 'VTable',
                'props': {
                    'hover': True
                },
                'content': [
                    {
                        'component': 'thead',
                        'content': [
                            {
                                'component': 'th',
                                'props': {
                                    'class': 'text-start ps-4'
                                },
                                'text': '日期'
                            },
                            {
                                'component': 'th',
                                'props': {
                                    'class': 'text-start ps-4'
                                },
                                'text': '站点'
                            },
                            {
                                'component': 'th',
                                'props': {
                                    'class': 'text-start ps-4'
                                },
                                'text': '状态'
                            }
                        ]
                    },
                    {
                        'component': 'tbody',
                        'content': contents
                    }
                ]
            }
        ]

    @eventmanager.register(EventType.SiteSignin)
    def sign_in(self, event: Event = None):
        """
        自动签到
        """
        # 日期
        today = datetime.today()
        if self._start_time and self._end_time:
            if int(datetime.today().hour) < self._start_time or int(datetime.today().hour) > self._end_time:
                logger.error(
                    f"当前时间 {int(datetime.today().hour)} 不在 {self._start_time}-{self._end_time} 范围内，暂不签到")
                return
        if event:
            logger.info("收到命令，开始站点签到 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始站点签到 ...",
                              userid=event.event_data.get("user"))

        yesterday = today - timedelta(days=1)
        yesterday_str = yesterday.strftime('%Y-%m-%d')
        # 删除昨天历史
        self.del_data(key=yesterday_str)

        # 查看今天有没有签到历史
        today = today.strftime('%Y-%m-%d')
        today_history = self.get_data(key=today)

        # 查询签到站点
        sign_sites = [site for site in self.sites.get_indexers() if not site.get("public")]
        # 过滤掉没有选中的站点
        if self._sign_sites:
            sign_sites = [site for site in sign_sites if site.get("id") in self._sign_sites]

        # 今日没数据
        if not today_history or self._clean:
            logger.info(f"今日 {today} 未签到，开始签到已选站点")
            # 过滤删除的站点
            self._sign_sites = [site.get("id") for site in sign_sites if site]
            if self._clean:
                # 关闭开关
                self._clean = False
        else:
            # 今天已签到需要重签站点
            retry_sites = today_history.get("retry")
            # 今天已签到站点
            already_sign_sites = today_history.get("sign")

            # 今日未签站点
            no_sign_sites = [site for site in sign_sites if
                             site.get("id") not in already_sign_sites or site.get("id") in retry_sites]

            if not no_sign_sites:
                logger.info(f"今日 {today} 已签到，无重新签到站点，本次任务结束")
                return

            # 签到站点 = 需要重签+今日未签
            sign_sites = no_sign_sites
            logger.info(f"今日 {today} 已签到，开始重签重试站点、特殊站点、未签站点")

        if not sign_sites:
            logger.info("没有需要签到的站点")
            return

        # 执行签到
        logger.info("开始执行签到任务 ...")
        with ThreadPool(min(len(sign_sites), int(self._queue_cnt))) as p:
            status = p.map(self.signin_site, sign_sites)

        if status:
            logger.info("站点签到任务完成！")
            # 获取今天的日期
            key = f"{datetime.now().month}月{datetime.now().day}日"
            # 保存数据
            self.save_data(key, [{
                "site": s[0],
                "status": s[1]
            } for s in status])

            # 命中重试词的站点id
            retry_sites = []
            # 命中重试词的站点签到msg
            retry_msg = []
            # 登录成功
            login_success_msg = []
            # 签到成功
            sign_success_msg = []
            # 已签到
            already_sign_msg = []
            # 仿真签到成功
            fz_sign_msg = []
            # 失败｜错误
            failed_msg = []

            sites = {site.get('name'): site.get("id") for site in self.sites.get_indexers() if not site.get("public")}
            for s in status:
                site_name = s[0]
                site_id = None
                if site_name:
                    site_id = sites.get(site_name)
                # 记录本次命中重试关键词的站点
                if self._retry_keyword:
                    if site_id:
                        match = re.search(self._retry_keyword, s[1])
                        if match:
                            logger.debug(f"站点 {site_name} 命中重试关键词 {self._retry_keyword}")
                            retry_sites.append(site_id)
                            # 命中的站点
                            retry_msg.append(s)
                            continue

                if "登录成功" in s:
                    login_success_msg.append(s)
                elif "仿真签到成功" in s:
                    fz_sign_msg.append(s)
                    continue
                elif "签到成功" in s:
                    sign_success_msg.append(s)
                elif '已签到' in s:
                    already_sign_msg.append(s)
                else:
                    failed_msg.append(s)

            if not self._retry_keyword:
                # 没设置重试关键词则重试已选站点
                retry_sites = self._sign_sites
            logger.debug(f"下次签到重试站点 {retry_sites}")

            # 存入历史
            self.save_data(key=today,
                           value={
                               "sign": self._sign_sites,
                               "retry": retry_sites
                           })

            # 发送通知
            if self._notify:
                # 签到详细信息 登录成功、签到成功、已签到、仿真签到成功、失败--命中重试
                signin_message = login_success_msg + sign_success_msg + already_sign_msg + fz_sign_msg + failed_msg
                if len(retry_msg) > 0:
                    signin_message += retry_msg

                self.post_message(title="站点自动签到",
                                  mtype=NotificationType.SiteMessage,
                                  text=f"全部签到数量: {len(list(self._sign_sites))} \n"
                                       f"本次签到数量: {len(sign_sites)} \n"
                                       f"下次签到数量: {len(retry_sites) if self._retry_keyword else 0} \n"
                                       f"{signin_message}"
                                  )
            if event:
                self.post_message(channel=event.event_data.get("channel"),
                                  title="站点签到完成！", userid=event.event_data.get("user"))
        else:
            logger.error("站点签到任务失败！")
            if event:
                self.post_message(channel=event.event_data.get("channel"),
                                  title="站点签到任务失败！", userid=event.event_data.get("user"))
        # 保存配置
        self.__update_config()

    def __build_class(self, url) -> Any:
        for site_schema in self._site_schema:
            try:
                if site_schema.match(url):
                    return site_schema
            except Exception as e:
                logger.error("站点模块加载失败：%s" % str(e))
        return None

    def signin_by_domain(self, url: str) -> schemas.Response:
        """
        签到一个站点，可由API调用
        """
        domain = StringUtils.get_url_domain(url)
        site_info = self.sites.get_indexer(domain)
        if not site_info:
            return schemas.Response(
                success=True,
                message=f"站点【{url}】不存在"
            )
        else:
            return schemas.Response(
                success=True,
                message=self.signin_site(site_info)
            )

    def signin_site(self, site_info: CommentedMap) -> Tuple[str, str]:
        """
        签到一个站点
        """
        site_module = self.__build_class(site_info.get("url"))
        if site_module and hasattr(site_module, "signin"):
            try:
                _, msg = site_module().signin(site_info)
                # 特殊站点直接返回签到信息，防止仿真签到、模拟登陆有歧义
                return site_info.get("name"), msg or ""
            except Exception as e:
                traceback.print_exc()
                return site_info.get("name"), f"签到失败：{str(e)}"
        else:
            return site_info.get("name"), self.__signin_base(site_info)

    @staticmethod
    def __signin_base(site_info: CommentedMap) -> str:
        """
        通用签到处理
        :param site_info: 站点信息
        :return: 签到结果信息
        """
        if not site_info:
            return ""
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        render = site_info.get("render")
        proxies = settings.PROXY if site_info.get("proxy") else None
        proxy_server = settings.PROXY_SERVER if site_info.get("proxy") else None
        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法签到")
            return ""
        # 模拟登录
        try:
            # 访问链接
            checkin_url = site_url
            if site_url.find("attendance.php") == -1:
                # 拼登签到地址
                checkin_url = urljoin(site_url, "attendance.php")
            logger.info(f"开始站点签到：{site}，地址：{checkin_url}...")
            if render:
                page_source = PlaywrightHelper().get_page_source(url=checkin_url,
                                                                 cookies=site_cookie,
                                                                 ua=ua,
                                                                 proxies=proxy_server)
                if not SiteUtils.is_logged_in(page_source):
                    if under_challenge(page_source):
                        return f"无法通过Cloudflare！"
                    return f"仿真登录失败，Cookie已失效！"
            else:
                res = RequestUtils(cookies=site_cookie,
                                   ua=ua,
                                   proxies=proxies
                                   ).get_res(url=checkin_url)
                if not res and site_url != checkin_url:
                    logger.info(f"开始站点模拟登录：{site}，地址：{site_url}...")
                    res = RequestUtils(cookies=site_cookie,
                                       ua=ua,
                                       proxies=proxies
                                       ).get_res(url=site_url)
                # 判断登录状态
                if res and res.status_code in [200, 500, 403]:
                    if not SiteUtils.is_logged_in(res.text):
                        if under_challenge(res.text):
                            msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                        elif res.status_code == 200:
                            msg = "Cookie已失效"
                        else:
                            msg = f"状态码：{res.status_code}"
                        logger.warn(f"{site} 签到失败，{msg}")
                        return f"签到失败，{msg}！"
                    else:
                        logger.info(f"{site} 签到成功")
                        return f"签到成功"
                elif res is not None:
                    logger.warn(f"{site} 签到失败，状态码：{res.status_code}")
                    return f"签到失败，状态码：{res.status_code}！"
                else:
                    logger.warn(f"{site} 签到失败，无法打开网站")
                    return f"签到失败，无法打开网站！"
        except Exception as e:
            logger.warn("%s 签到失败：%s" % (site, str(e)))
            traceback.print_exc()
            return f"签到失败：{str(e)}！"

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            sign_sites = config.get("sign_sites")
            if sign_sites:
                if isinstance(sign_sites, str):
                    sign_sites = [sign_sites]

                # 删除对应站点
                if site_id:
                    sign_sites = [site for site in sign_sites if int(site) != int(site_id)]
                else:
                    # 清空
                    sign_sites = []

                # 若无站点，则停止
                if len(sign_sites) == 0:
                    self._enabled = False

                # 保存配置
                self.update_config(
                    {
                        "enabled": self._enabled,
                        "notify": self._notify,
                        "cron": self._cron,
                        "onlyonce": self._onlyonce,
                        "queue_cnt": self._queue_cnt,
                        "sign_sites": sign_sites
                    }
                )
